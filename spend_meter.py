"""Local JSONL spend ledger. Standard library only.

An exclusive lock covers each read-check-append. The lock is released
before the caller makes an API call. The sidecar is ``{ledger}.lock``
(one byte): POSIX ``fcntl.flock(LOCK_EX)``, Windows ``msvcrt.locking``
of that byte with ``LK_LOCK``.
"""

import contextlib
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from config import (
    BUDGET_USD,
    CHARS_PER_TOKEN,
    DEFAULT_MAX_TOKENS,
    MAX_PROMPT_TOKENS,
    MODEL_PRICES,
)

_MILLION = Decimal(1_000_000)
_ROW_KEYS = (
    "timestamp",
    "kind",
    "reservation_id",
    "model",
    "prompt_tokens",
    "cached_prompt_tokens",
    "completion_tokens",
    "usd",
)


class SpendRefused(Exception):
    """The meter refused the call. No ledger row is written and no API call runs."""


def format_usd(value: Decimal) -> str:
    """USD as a plain decimal string. JSON never stores a float."""
    return format(value, "f")


class SpendMeter:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    @staticmethod
    def estimate_prompt_tokens(text: str) -> int:
        """Ceil of len(text) / CHARS_PER_TOKEN, via integer arithmetic."""
        return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN

    @staticmethod
    def worst_case_usd(model_id: str, prompt_tokens: int) -> Decimal:
        """Full prompt price plus DEFAULT_MAX_TOKENS at the completion price.

        No cached-token discount. Raises KeyError when model_id has no price.
        """
        price = MODEL_PRICES[model_id]
        return (
            Decimal(prompt_tokens) * price["prompt"]
            + Decimal(DEFAULT_MAX_TOKENS) * price["completion"]
        ) / _MILLION

    def reserve(self, model_id: str, prompt_tokens: int) -> str:
        """Lock, check, append a reserved row, fsync, unlock, return the id.

        SpendRefused writes nothing. A lock error propagates and the caller
        must not make the API call.
        """
        with self._locked():
            if model_id not in MODEL_PRICES:
                raise SpendRefused(f"no price for model {model_id!r}")
            if prompt_tokens >= MAX_PROMPT_TOKENS:
                raise SpendRefused(
                    f"estimated prompt tokens {prompt_tokens} reach the "
                    f"{MAX_PROMPT_TOKENS} token cap"
                )
            worst = self.worst_case_usd(model_id, prompt_tokens)
            projected = _committed(self._read_rows()) + worst
            if projected > BUDGET_USD:
                raise SpendRefused(
                    f"projected cost {format_usd(projected)} USD exceeds "
                    f"budget {format_usd(BUDGET_USD)} USD"
                )
            reservation_id = str(uuid.uuid4())
            self._append_row(
                _row(
                    kind="reserved",
                    reservation_id=reservation_id,
                    model=model_id,
                    prompt_tokens=prompt_tokens,
                    cached_prompt_tokens=0,
                    completion_tokens=DEFAULT_MAX_TOKENS,
                    usd=worst,
                )
            )
            return reservation_id

    def settle(self, reservation_id: str, usage: object | None = None) -> None:
        """Append one settled row for this id. A second settle appends nothing.

        Usage is priced only when cached tokens are in ``0 .. prompt_tokens``.
        Otherwise the reserved token counts and worst-case USD are copied.
        ``cost_in_usd_ticks`` and ``reasoning_tokens`` are ignored.
        """
        with self._locked():
            rows = self._read_rows()
            if any(
                row["kind"] == "settled" and row["reservation_id"] == reservation_id
                for row in rows
            ):
                return
            reserved = next(
                row
                for row in rows
                if row["kind"] == "reserved" and row["reservation_id"] == reservation_id
            )
            priced = _price_usage(usage, MODEL_PRICES.get(reserved["model"]))
            if priced is None:
                self._append_row(
                    _row(
                        kind="settled",
                        reservation_id=reservation_id,
                        model=reserved["model"],
                        prompt_tokens=reserved["prompt_tokens"],
                        cached_prompt_tokens=reserved["cached_prompt_tokens"],
                        completion_tokens=reserved["completion_tokens"],
                        usd=Decimal(reserved["usd"]),
                    )
                )
                return
            prompt_tokens, cached_tokens, completion_tokens, usd = priced
            self._append_row(
                _row(
                    kind="settled",
                    reservation_id=reservation_id,
                    model=reserved["model"],
                    prompt_tokens=prompt_tokens,
                    cached_prompt_tokens=cached_tokens,
                    completion_tokens=completion_tokens,
                    usd=usd,
                )
            )

    @contextlib.contextmanager
    def _locked(self):
        handle = self._acquire()
        try:
            yield
        finally:
            self._release(handle)

    def _acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(str(self.path) + ".lock")
        _ensure_lock_byte(lock_path)
        handle = open(lock_path, "r+b")
        try:
            _lock(handle)
        except OSError:
            handle.close()
            raise
        return handle

    def _release(self, handle) -> None:
        try:
            _unlock(handle)
        finally:
            handle.close()

    def _read_rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        text = self.path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def _append_row(self, row: dict) -> None:
        line = json.dumps(row) + "\n"
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())


def _row(
    *,
    kind: str,
    reservation_id: str,
    model: str,
    prompt_tokens: int,
    cached_prompt_tokens: int,
    completion_tokens: int,
    usd: Decimal,
) -> dict:
    row = {
        "timestamp": datetime.now(UTC).isoformat(),
        "kind": kind,
        "reservation_id": reservation_id,
        "model": model,
        "prompt_tokens": int(prompt_tokens),
        "cached_prompt_tokens": int(cached_prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "usd": format_usd(usd),
    }
    if tuple(row) != _ROW_KEYS:
        raise RuntimeError("ledger row keys drifted")
    return row


def _committed(rows: list[dict]) -> Decimal:
    """Each reservation once: settled USD if present, else reserved worst case."""
    totals: dict[str, Decimal] = {}
    settled: set[str] = set()
    for row in rows:
        reservation_id = row["reservation_id"]
        if row["kind"] == "settled":
            totals[reservation_id] = Decimal(row["usd"])
            settled.add(reservation_id)
        elif row["kind"] == "reserved" and reservation_id not in settled:
            totals.setdefault(reservation_id, Decimal(row["usd"]))
    return sum(totals.values(), Decimal(0))


def _price_usage(usage: object | None, price: dict[str, Decimal] | None):
    """(prompt, cached, completion, usd) or None when the call must stay worst case."""
    if usage is None or price is None:
        return None
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    if prompt is None or completion is None:
        return None
    cached = _cached_tokens(usage)
    prompt_i = int(prompt)
    cached_i = int(cached)
    completion_i = int(completion)
    if not 0 <= cached_i <= prompt_i:
        return None
    usd = (
        (Decimal(prompt_i) - Decimal(cached_i)) * price["prompt"]
        + Decimal(cached_i) * price["cached_prompt"]
        + Decimal(completion_i) * price["completion"]
    ) / _MILLION
    return prompt_i, cached_i, completion_i, usd


def _cached_tokens(usage: object) -> int:
    """xAI ``cached_prompt_text_tokens``, else OpenAI ``prompt_tokens_details``.

    Missing details count as 0. This does not read ``cost_in_usd_ticks`` or
    ``reasoning_tokens``.
    """
    if hasattr(usage, "cached_prompt_text_tokens"):
        value = usage.cached_prompt_text_tokens
        return 0 if value is None else int(value)
    details = getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return 0
    value = getattr(details, "cached_tokens", None)
    return 0 if value is None else int(value)


def _ensure_lock_byte(lock_path: Path) -> None:
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(str(lock_path), flags, 0o600)
    try:
        if os.lseek(fd, 0, os.SEEK_END) < 1:
            os.write(fd, b"\0")
            os.fsync(fd)
    finally:
        os.close(fd)


def _lock(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
