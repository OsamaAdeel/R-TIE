"""Per-stage timing for the request pipeline.

`stage_timer` is a sync context manager that logs the elapsed wall-clock
time between enter and exit. It works for both sync and async call
sites because Python's `with` block doesn't await — the wrapped block
may itself `await` freely.

`mark_event` emits a single-point log line (no duration). Useful for
things like "first token reached the SSE generator" where the interesting
number is the absolute wall-clock timestamp relative to request start.

Both helpers are intentionally minimal:

* Use `time.perf_counter()` for monotonic, sub-millisecond accuracy.
* Pull the correlation ID from the request-scoped ContextVar when the
  caller doesn't pass one, so every line is joinable to a request.
* Never raise on logging failure — a telemetry bug must never change
  request behavior.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Optional

from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id

# Route stage-timing lines into the existing app.log rotating handler
# so grep for [STAGE_TIMING] picks them up alongside normal app logs.
logger = get_logger("rtie.stage_timer", concern="app")


@contextmanager
def stage_timer(
    stage_name: str,
    correlation_id: Optional[str] = None,
    **extra,
):
    """Log the elapsed time for the wrapped block.

    Emits a single INFO line of the form:

        [STAGE_TIMING] correlation_id=<cid> stage=<name> elapsed_ms=<x.x> [<k=v> ...]

    The block always runs to completion — the timer does not swallow
    exceptions and does not affect control flow. If an exception is
    raised inside the block, the log line is still emitted (with an
    `exc=1` marker), then the exception propagates.
    """
    cid = correlation_id or _safe_correlation_id()
    start = time.perf_counter()
    exc_flag = 0
    try:
        yield
    except BaseException:
        exc_flag = 1
        raise
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        extras = _format_extras(extra)
        suffix = f" exc=1{extras}" if exc_flag else extras
        try:
            logger.info(
                "[STAGE_TIMING] correlation_id=%s stage=%s elapsed_ms=%.1f%s",
                cid,
                stage_name,
                elapsed_ms,
                suffix,
            )
        except Exception:
            # Never let a telemetry failure kill a request.
            pass


def mark_event(event_name: str, correlation_id: Optional[str] = None, **extra) -> None:
    """Log a single-point timing event with the current monotonic clock.

    Useful for "first token" kinds of markers where the duration of
    interest is computed in the analysis (relative to another mark) and
    not by wrapping a code block.
    """
    cid = correlation_id or _safe_correlation_id()
    extras = _format_extras(extra)
    try:
        logger.info(
            "[STAGE_TIMING] correlation_id=%s event=%s monotonic_ms=%.1f%s",
            cid,
            event_name,
            time.perf_counter() * 1000.0,
            extras,
        )
    except Exception:
        pass


def _safe_correlation_id() -> str:
    """Return the request-scoped correlation ID or '-' if unset.

    `get_correlation_id()` raises when called outside a request scope
    (e.g., startup). We don't want instrumentation to fail in those
    contexts, so swallow everything.
    """
    try:
        cid = get_correlation_id()
    except Exception:
        return "-"
    return cid or "-"


def _format_extras(extra: dict) -> str:
    if not extra:
        return ""
    parts = []
    for k, v in extra.items():
        parts.append(f"{k}={v}")
    return " " + " ".join(parts)
