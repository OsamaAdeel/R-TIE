"""Diagnostic telemetry helpers for RTIE.

Minimal, dependency-free wrappers used to attribute per-stage latency
inside the `/v1/stream` request pipeline. Log-only; no state mutation.
"""

from src.telemetry.stage_timer import stage_timer, mark_event

__all__ = ["stage_timer", "mark_event"]
