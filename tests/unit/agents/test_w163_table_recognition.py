"""W163: W57 table-vs-function recognition.

W57's per-claim-binding check flags a cited name that survives the
function-name filters but is absent from the retrieved function set as
``GROUNDING-HIGH: cited function ... not in retrieved sources``. Some such
names are real SOURCE TABLES the explainer legitimately cites — e.g.
``ABL_OPS_RISK_DATA``, the source table read by FN_LOAD_OPS_RISK_DATA. The
``ABL_`` prefix was missing from the table-prefix filter, so the table was
mis-flagged as a fabricated function and forced C01 UNVERIFIED (surfaced once
W160 cleared the masking W108-TRUNCATED downgrade).

The fix recognizes real tables by IDENTITY (``schemas_for_table`` — does any
indexed function reference the name as target/source table?), NOT by prefix:
``ABL_`` names both real tables AND real functions, so a prefix allow-list
would wave a fabricated ``ABL_``-prefixed function through W57.

Two-sided discriminator:
  - FIXED: the real table ABL_OPS_RISK_DATA cited in the captured C01 heading
    framing no longer trips GROUNDING-HIGH (identity lookup -> ["OFSERM"]).
  - STILL CAUGHT: a fabricated function name (ABL_-prefixed AND non-ABL) is in
    no graph as a table (identity -> []), so it still fires GROUNDING-HIGH.

The heading framing below is the REAL trigger captured from a live C01
``/v1/stream`` run (scratch/w163_c01_markdown.md), not an assumed one — the
prose ``...from the `ABL_OPS_RISK_DATA` table`` form is correctly skipped by
the prose regex; the ``### Step 4: ... ABL_OPS_RISK_DATA`` heading is what
fires (W78a heading Pattern A).
"""

import pytest

import src.parsing.schema_discovery as schema_discovery
from src.agents.logic_explainer import (
    _w57_check_per_claim_binding,
    _w57_cited_name_is_known_table,
)

# Sentinel: the helper only checks ``redis_client is None``; the real client is
# never touched because schemas_for_table is monkeypatched.
_FAKE_REDIS = object()


def _src(line_count, text="dummy"):
    return [{"line": i, "text": text} for i in range(1, line_count + 1)]


def _patch_tables(monkeypatch, table_set):
    """Make schemas_for_table report membership against *table_set* (upper)."""
    upper = {t.upper() for t in table_set}

    def fake(name, redis_client, schemas=None):
        return ["OFSERM"] if (name or "").upper() in upper else []

    monkeypatch.setattr(schema_discovery, "schemas_for_table", fake)


# The captured C01 framing (real trigger): a `### Step N: ... <TABLE>` heading.
_C01_STEP4_HEADING = (
    "### Step 4: Data Insertion from ABL_OPS_RISK_DATA (Lines 203-222)\n"
    "The function inserts data into `STG_OPS_RISK_DATA` from the "
    "`ABL_OPS_RISK_DATA` table. It selects various columns."
)


# ===========================================================================
# FIXED: real table no longer mis-flagged
# ===========================================================================

def test_real_table_in_heading_not_flagged(monkeypatch):
    """ABL_OPS_RISK_DATA is a real table -> identity lookup clears it."""
    _patch_tables(monkeypatch, {"ABL_OPS_RISK_DATA"})
    multi = {"FN_LOAD_OPS_RISK_DATA": {"source_code": _src(376)}}
    warnings = _w57_check_per_claim_binding(
        _C01_STEP4_HEADING, multi, ["FN_LOAD_OPS_RISK_DATA"],
        redis_client=_FAKE_REDIS,
    )
    assert not any("ABL_OPS_RISK_DATA" in w for w in warnings), warnings
    assert not any("GROUNDING-HIGH" in w for w in warnings), warnings


def test_real_table_in_prose_function_framing_not_flagged(monkeypatch):
    """Even if the model mis-frames a real table as 'the function `T`',
    identity recognizes it as a table and clears it."""
    _patch_tables(monkeypatch, {"ABL_OPS_RISK_DATA"})
    md = "The function `ABL_OPS_RISK_DATA` provides the source rows."
    multi = {"FN_LOAD_OPS_RISK_DATA": {"source_code": _src(376)}}
    warnings = _w57_check_per_claim_binding(
        md, multi, ["FN_LOAD_OPS_RISK_DATA"], redis_client=_FAKE_REDIS,
    )
    assert warnings == [], warnings


# ===========================================================================
# STILL CAUGHT: fabricated functions keep firing (guard integrity)
# ===========================================================================

def test_fabricated_abl_function_still_flagged(monkeypatch):
    """A fabricated ABL_-prefixed function is in no graph as a table -> fires.

    This is the crux: a naive 'ABL_ = table, skip' would silence this. Identity
    lookup keeps the catch — schemas_for_table('ABL_NONEXISTENT_FN') -> [].
    """
    _patch_tables(monkeypatch, {"ABL_OPS_RISK_DATA"})  # only the real table
    md = "The function `ABL_NONEXISTENT_FN` orchestrates the whole pipeline."
    multi = {"FN_LOAD_OPS_RISK_DATA": {"source_code": _src(376)}}
    warnings = _w57_check_per_claim_binding(
        md, multi, ["FN_LOAD_OPS_RISK_DATA"], redis_client=_FAKE_REDIS,
    )
    assert any(
        "ABL_NONEXISTENT_FN" in w and "GROUNDING-HIGH" in w for w in warnings
    ), warnings


def test_fabricated_non_abl_function_still_flagged(monkeypatch):
    """A non-ABL fabricated function must also still fire."""
    _patch_tables(monkeypatch, {"ABL_OPS_RISK_DATA"})
    md = "The function `FAKE_FABRICATED_FN` computes the final figure."
    multi = {"FN_LOAD_OPS_RISK_DATA": {"source_code": _src(376)}}
    warnings = _w57_check_per_claim_binding(
        md, multi, ["FN_LOAD_OPS_RISK_DATA"], redis_client=_FAKE_REDIS,
    )
    assert any(
        "FAKE_FABRICATED_FN" in w and "GROUNDING-HIGH" in w for w in warnings
    ), warnings


def test_fabricated_function_in_heading_still_flagged(monkeypatch):
    """Mirror the real trigger path: a fabricated name in a `### Step` heading
    still fires (only real tables are spared)."""
    _patch_tables(monkeypatch, {"ABL_OPS_RISK_DATA"})
    md = "### Step 9: Call to ABL_NONEXISTENT_FN (Lines 400-410)\nIt runs last."
    multi = {"FN_LOAD_OPS_RISK_DATA": {"source_code": _src(376)}}
    warnings = _w57_check_per_claim_binding(
        md, multi, ["FN_LOAD_OPS_RISK_DATA"], redis_client=_FAKE_REDIS,
    )
    assert any(
        "ABL_NONEXISTENT_FN" in w and "GROUNDING-HIGH" in w for w in warnings
    ), warnings


# ===========================================================================
# Fallback: no redis_client -> pre-W163 behavior preserved (no new hole)
# ===========================================================================

def test_no_redis_client_preserves_pre_w163_flag():
    """With redis_client=None (unit / pre-startup), the identity guard is inert
    and the real table is flagged exactly as before — the cure only applies on
    the live path, opening no offline hole."""
    md = _C01_STEP4_HEADING
    multi = {"FN_LOAD_OPS_RISK_DATA": {"source_code": _src(376)}}
    warnings = _w57_check_per_claim_binding(
        md, multi, ["FN_LOAD_OPS_RISK_DATA"],  # redis_client defaults to None
    )
    assert any(
        "ABL_OPS_RISK_DATA" in w and "GROUNDING-HIGH" in w for w in warnings
    ), warnings


# ===========================================================================
# Helper unit: identity guard semantics
# ===========================================================================

def test_helper_none_redis_returns_false():
    assert _w57_cited_name_is_known_table("ABL_OPS_RISK_DATA", None) is False


def test_helper_true_for_known_table(monkeypatch):
    _patch_tables(monkeypatch, {"ABL_OPS_RISK_DATA"})
    assert _w57_cited_name_is_known_table(
        "ABL_OPS_RISK_DATA", _FAKE_REDIS
    ) is True


def test_helper_false_for_unknown_name(monkeypatch):
    _patch_tables(monkeypatch, {"ABL_OPS_RISK_DATA"})
    assert _w57_cited_name_is_known_table(
        "ABL_NONEXISTENT_FN", _FAKE_REDIS
    ) is False


def test_helper_caches_result(monkeypatch):
    """The per-call cache memoizes so repeated names cost one scan."""
    calls = {"n": 0}

    def fake(name, redis_client, schemas=None):
        calls["n"] += 1
        return ["OFSERM"]

    monkeypatch.setattr(schema_discovery, "schemas_for_table", fake)
    cache: dict = {}
    a = _w57_cited_name_is_known_table("ABL_OPS_RISK_DATA", _FAKE_REDIS, cache)
    b = _w57_cited_name_is_known_table("ABL_OPS_RISK_DATA", _FAKE_REDIS, cache)
    assert a is True and b is True
    assert calls["n"] == 1
