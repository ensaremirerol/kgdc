"""Progress lines on stderr. Muted with progress.enabled = False (CLI: --quiet)."""
from __future__ import annotations

import sys
import threading
import time

enabled = True
_t0 = time.monotonic()
_lock = threading.Lock()


def log(msg: str) -> None:
    if not enabled:
        return
    with _lock:
        print(f"[{time.monotonic() - _t0:6.1f}s] {msg}", file=sys.stderr, flush=True)


def stage(title: str) -> None:
    """A header between the pipeline's steps, so a run reads as the five steps of the paper."""
    if not enabled:
        return
    with _lock:
        print(f"\n== {title} ==", file=sys.stderr, flush=True)
