"""W86 — live integration canaries A / B / C.

pytest-collected (`test_*` prefix), in contrast with
``test_live_stream.py`` which is a manual smoke harness registered via
``@test()`` and run via its own ``main()``.

Requires a running RTIE backend on http://localhost:8000 + Redis + Oracle.
Each canary POSTs to ``/v1/stream``, captures the verbatim ``done`` event
payload, writes it to ``scratch/w86_canary_<a|b|c>.json`` (so the
reviewer can inspect the trust-contract surface verbatim regardless of
pytest's stdout capture mode), then asserts the W86-relevant fields.

Run: ``pytest tests/integration/test_w86_canaries.py -v``
Filter: ``pytest tests/integration/ -k "w86" -v``

Each canary maps to:
    * A — Stakeholder Q1 (BIA op risk at 31-Dec-2026; aggregate-of-nulls)
    * B — Stakeholder Q5 (NPLs on 31-dec-2025; row-list all-null metric)
    * C — No-false-positive guard (standard N_EOP_BAL canary, must
      stay VERIFIED)
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tests.integration.test_live_stream import run_query


SCRATCH_DIR = Path(__file__).resolve().parents[2] / "scratch"
BACKEND_URL = "http://localhost:8000"


def _backend_up() -> bool:
    try:
        with httpx.Client(timeout=2.0) as c:
            r = c.get(f"{BACKEND_URL}/health")
            return r.status_code < 500
    except Exception:
        return False


@pytest.fixture(scope="module")
def require_backend():
    if not _backend_up():
        pytest.skip(
            "RTIE backend not reachable at "
            f"{BACKEND_URL} — start with `python run.py` from RTIE/."
        )


def _persist_payload(name: str, done: dict) -> Path:
    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    path = SCRATCH_DIR / f"w86_canary_{name}.json"
    path.write_text(json.dumps(done, default=str, indent=2), encoding="utf-8")
    return path


def _w86_warning(warnings: list) -> str | None:
    for w in warnings or []:
        if isinstance(w, str) and w.startswith("suspicious_metric_all_null:"):
            return w
    return None


# ---------------------------------------------------------------------
# Canary A — Q1 reproduction (BIA at future date, aggregate-of-nulls)
# ---------------------------------------------------------------------

def test_w86_canary_a_bia_future_date(require_backend):
    """Stakeholder Q1: BIA values for 31-Dec-2026 → aggregate returns
    one row with every metric NULL. Was VERIFIED pre-W86, must now be
    UNVERIFIED with a suspicious_metric_all_null warning."""
    result = run_query(
        "What is the operational risk values based on basic indicator "
        "approach on 31-Dec-2026?"
    )
    done = result["done"] or {}
    path = _persist_payload("a_bia_future_date", done)
    print(f"\n[W86-A] verbatim done payload written to: {path}")
    print(json.dumps(done, default=str, indent=2))

    warnings = done.get("sanity_warnings") or []
    failures: list[str] = []
    if done.get("type") != "data_query":
        failures.append(f"type={done.get('type')!r}, expected 'data_query'")
    if done.get("badge") != "UNVERIFIED":
        failures.append(f"badge={done.get('badge')!r}, expected 'UNVERIFIED'")
    if not done.get("suspicious"):
        failures.append(f"suspicious={done.get('suspicious')!r}, expected truthy")
    if not _w86_warning(warnings):
        failures.append(
            "no 'suspicious_metric_all_null' entry in sanity_warnings; "
            f"got: {warnings!r}"
        )

    assert not failures, (
        "W86-A failed checks:\n  - "
        + "\n  - ".join(failures)
        + f"\n\nVerbatim done payload at: {path}"
    )


# ---------------------------------------------------------------------
# Canary B — Q5 reproduction (row-list all-null metric column)
# ---------------------------------------------------------------------

def test_w86_canary_b_npls_all_null(require_backend):
    """Stakeholder Q5: row-list returns N rows with N_EOP_BAL_NPL NULL
    on every row. Was VERIFIED pre-W86, must now be UNVERIFIED with a
    suspicious_metric_all_null warning."""
    result = run_query("What are my npls on 31-dec-2025?")
    done = result["done"] or {}
    path = _persist_payload("b_npls_all_null", done)
    print(f"\n[W86-B] verbatim done payload written to: {path}")
    print(json.dumps(done, default=str, indent=2))

    warnings = done.get("sanity_warnings") or []
    failures: list[str] = []
    if done.get("type") != "data_query":
        failures.append(f"type={done.get('type')!r}, expected 'data_query'")
    if done.get("badge") != "UNVERIFIED":
        failures.append(f"badge={done.get('badge')!r}, expected 'UNVERIFIED'")
    if not done.get("suspicious"):
        failures.append(f"suspicious={done.get('suspicious')!r}, expected truthy")
    if not _w86_warning(warnings):
        failures.append(
            "no 'suspicious_metric_all_null' entry in sanity_warnings; "
            f"got: {warnings!r}"
        )

    assert not failures, (
        "W86-B failed checks:\n  - "
        + "\n  - ".join(failures)
        + f"\n\nVerbatim done payload at: {path}"
    )


# ---------------------------------------------------------------------
# Canary C — no-false-positive guard on standard aggregate
# ---------------------------------------------------------------------

def test_w86_canary_c_no_false_positive(require_backend):
    """Standard N_EOP_BAL canary — non-null aggregate must remain
    VERIFIED with no suspicious_metric_all_null warning. Pre-Phase-1
    baseline sum is -24,179,237,139.63 but the assertion only checks
    badge / suspicious / warnings (the LLM may shape SQL differently
    across runs)."""
    result = run_query(
        "What is the total N_EOP_BAL for V_LV_CODE='ABL' on 2025-12-31?"
    )
    done = result["done"] or {}
    path = _persist_payload("c_no_false_positive", done)
    print(f"\n[W86-C] verbatim done payload written to: {path}")
    print(json.dumps(done, default=str, indent=2))

    warnings = done.get("sanity_warnings") or []
    failures: list[str] = []
    if done.get("type") != "data_query":
        failures.append(f"type={done.get('type')!r}, expected 'data_query'")
    if done.get("badge") != "VERIFIED":
        failures.append(f"badge={done.get('badge')!r}, expected 'VERIFIED'")
    if done.get("suspicious"):
        failures.append(
            f"suspicious={done.get('suspicious')!r}, expected falsy"
        )
    if _w86_warning(warnings):
        failures.append(
            "unexpected 'suspicious_metric_all_null' fire on standard "
            f"aggregate; warnings={warnings!r}"
        )

    assert not failures, (
        "W86-C failed checks:\n  - "
        + "\n  - ".join(failures)
        + f"\n\nVerbatim done payload at: {path}"
    )
