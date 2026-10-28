"""Spawn targets for the spend-meter process tests.

Pytest does not collect this module. It must not import the assistant or pytest:
Windows spawn re-imports the target module in the child.
"""

import time
from pathlib import Path

from spend_meter import SpendMeter, SpendRefused


def try_reserve(ledger_path, model_id, prompt_tokens, barrier, queue):
    """Reserve once after the barrier. Report ok, refused, or error."""
    try:
        barrier.wait(30)
    except Exception as exc:
        queue.put(("error", f"barrier {type(exc).__name__}: {exc}"))
        return
    try:
        reservation_id = SpendMeter(ledger_path).reserve(model_id, prompt_tokens)
    except SpendRefused:
        queue.put(("refused", ""))
        return
    except Exception as exc:
        queue.put(("error", f"{type(exc).__name__}: {exc}"))
        return
    queue.put(("ok", reservation_id))


def reserve_then_wait(ledger_path, model_id, prompt_tokens, ready_file):
    """Reserve, fsync (inside reserve), signal, then wait to be killed.

    Does not settle. The parent kills this process after the signal file appears.
    """
    reservation_id = SpendMeter(ledger_path).reserve(model_id, prompt_tokens)
    Path(ready_file).write_text(reservation_id, encoding="utf-8", newline="\n")
    time.sleep(120)
