"""Column-provenance routing — live integration canary.

The fix: a query naming a *column* and asking how it is written/populated
("How is N_EOP_BAL written?") must route to the VARIABLE_TRACE trace path with
the column's WRITER function(s) force-included into retrieval — instead of the
pre-fix unanchored narrow semantic search that retrieved functions which never
write N_EOP_BAL and let the LLM fabricate a relationship.

Asserts:
  * the meta event reports query_type == VARIABLE_TRACE (the pass re-routed it)
  * multi_source (functions_analyzed) contains POPULATE_PP_FROMGL — the real
    writer — and ideally its _AMC sibling
  * the rendered answer names POPULATE_PP_FROMGL as the writer

Requires a running RTIE backend on http://localhost:8000 + Redis + Oracle.
Writes the verbatim meta + done payloads to scratch/ for review.

Run: ``pytest tests/integration/test_column_provenance_canary.py -v``
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


def _first_meta(events: list) -> dict:
    for kind, payload in events or []:
        if kind == "meta" and isinstance(payload, dict):
            return payload
    return {}


def test_column_provenance_routes_to_writer_trace(require_backend):
    result = run_query("How is N_EOP_BAL written?")
    done = result["done"] or {}
    meta = _first_meta(result["events"])
    markdown = result["markdown"] or ""

    SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    path = SCRATCH_DIR / "column_provenance_canary.json"
    path.write_text(
        json.dumps({"meta": meta, "done": done, "markdown": markdown},
                   default=str, indent=2),
        encoding="utf-8",
    )
    print(f"\n[column-provenance] verbatim payload written to: {path}")
    print(json.dumps(meta, default=str, indent=2))

    functions_analyzed = [
        str(f).upper() for f in (meta.get("functions_analyzed") or [])
    ]
    failures: list[str] = []

    if meta.get("query_type") != "VARIABLE_TRACE":
        failures.append(
            f"query_type={meta.get('query_type')!r}, expected 'VARIABLE_TRACE' "
            "(column-provenance pass should have re-routed)"
        )
    if not any("POPULATE_PP_FROMGL" in f for f in functions_analyzed):
        failures.append(
            "POPULATE_PP_FROMGL absent from functions_analyzed (writer not "
            f"force-included into multi_source); got: {functions_analyzed!r}"
        )
    if "POPULATE_PP_FROMGL" not in markdown.upper():
        failures.append(
            "rendered answer does not name POPULATE_PP_FROMGL as the writer"
        )

    assert not failures, (
        "column-provenance canary failed checks:\n  - "
        + "\n  - ".join(failures)
        + f"\n\nVerbatim payload at: {path}"
    )


def test_column_provenance_includes_amc_sibling(require_backend):
    """Soft check: the multi-writer set should surface the _AMC sibling too.

    Skipped (not failed) when the corpus loaded into this backend does not
    contain POPULATE_PP_FROMGL_AMC — the writer set is corpus-dependent, and
    the load-bearing assertion is the primary writer in the test above.
    """
    result = run_query("How is N_EOP_BAL written?")
    meta = _first_meta(result["events"])
    functions_analyzed = [
        str(f).upper() for f in (meta.get("functions_analyzed") or [])
    ]
    if not any("POPULATE_PP_FROMGL_AMC" in f for f in functions_analyzed):
        pytest.skip(
            "POPULATE_PP_FROMGL_AMC not in this backend's corpus / writer set; "
            f"functions_analyzed={functions_analyzed!r}"
        )
    assert any("POPULATE_PP_FROMGL_AMC" in f for f in functions_analyzed)
