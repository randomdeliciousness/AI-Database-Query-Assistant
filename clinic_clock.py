"""Simulated clock for the vet-clinic generator.

The clock is the only source of filler timestamps. It does not read the
wall clock. Lead time does not move it.
"""

import datetime

EPOCH = datetime.datetime(2024, 1, 2, 14, 0, tzinfo=datetime.UTC)


class SimulatedClock:
    def __init__(self, start: datetime.datetime | None = None) -> None:
        if start is None:
            start = EPOCH
        if start.tzinfo is None or start.utcoffset() != datetime.timedelta(0):
            raise ValueError("start must be timezone-aware UTC")
        self._now = start

    @property
    def now(self) -> datetime.datetime:
        return self._now

    def advance(self, seconds: int) -> datetime.datetime:
        if isinstance(seconds, bool) or not isinstance(seconds, int):
            raise TypeError("seconds must be an int")
        if seconds < 0:
            raise ValueError("seconds must be non-negative")
        self._now = self._now + datetime.timedelta(seconds=seconds)
        return self._now

    def iso(self) -> str:
        """UTC, second resolution, suffix Z. Example: 2024-01-02T14:00:00Z."""
        return self._now.strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def parse_iso(text: str) -> datetime.datetime:
        """Accept only the iso() form. Return timezone-aware UTC."""
        if not isinstance(text, str) or len(text) != 20 or not text.endswith("Z"):
            raise ValueError("timestamp must be YYYY-MM-DDTHH:MM:SSZ")
        try:
            parsed = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise ValueError("timestamp must be YYYY-MM-DDTHH:MM:SSZ") from exc
        return parsed.replace(tzinfo=datetime.UTC)
