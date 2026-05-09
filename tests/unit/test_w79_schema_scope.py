"""W79 — user-driven schema selection.

The /v1/stream endpoint accepts a `schema_scope` field that the UI
dropdown drives. ALL fan-out runs per-schema top-K retrieval (NOT
global top-K, which would let one schema's mediocre matches crowd out
another schema's good ones); a specific schema name scopes retrieval
to that schema only and overrides the LLM-inferred schema_name.

These tests pin:
  * QueryRequest accepts schema_scope and defaults to "ALL"
  * _normalize_schema_scope maps inbound values to canonical tokens
  * _run_scoped_vector_search dispatches ALL fan-out vs scoped mode
  * _run_scope_mismatch_precheck (D2 cross-scope detection) returns
    a structured DECLINED payload when the named function lives in
    another schema, and None otherwise
  * _build_scope_mismatch_response shape matches the W37 family
"""

from __future__ import annotations

import pytest

from src import main as main_mod
from src.main import (
    QueryRequest,
    _SCHEMA_SCOPE_ALL,
    _build_scope_mismatch_response,
    _normalize_schema_scope,
    _run_bi_scope_mismatch_precheck,
    _run_scope_mismatch_precheck,
    _run_scoped_vector_search,
)


# ---------------------------------------------------------------------------
# QueryRequest schema_scope plumbing
# ---------------------------------------------------------------------------


def test_query_request_defaults_schema_scope_to_all():
    """A request body without schema_scope must default to "ALL" — the
    conservative scope that fans out across every discovered schema."""
    req = QueryRequest(
        query="how is CAP973 calculated?",
        session_id="s1",
        engineer_id="eng",
    )
    assert req.schema_scope == "ALL"


def test_query_request_accepts_explicit_schema_scope():
    """Explicit schema selections must round-trip verbatim — the
    backend doesn't coerce here, that's _normalize_schema_scope's job."""
    req = QueryRequest(
        query="x",
        session_id="s1",
        engineer_id="eng",
        schema_scope="OFSERM",
    )
    assert req.schema_scope == "OFSERM"


# ---------------------------------------------------------------------------
# _normalize_schema_scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ALL", "ALL"),
        ("all", "ALL"),         # case-insensitive intake
        ("All", "ALL"),
        ("OFSMDM", "OFSMDM"),
        ("ofsmdm", "OFSMDM"),   # case-insensitive intake → upper canonical
        ("OFSERM", "OFSERM"),
        ("FSDM", "FSDM"),
        ("FSAPPS", "FSAPPS"),
        ("  OFSERM  ", "OFSERM"),  # whitespace tolerated
    ],
)
def test_normalize_schema_scope_canonicalizes_known_values(raw, expected):
    assert _normalize_schema_scope(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "UNKNOWN", "OFSAA", "ofserm-x"])
def test_normalize_schema_scope_falls_back_to_all_on_unknown(raw):
    """Empty / unrecognized values must degrade to the safe default
    ("ALL") rather than 400ing — a malformed scope from a stale UI
    build should still produce an answer, not a hard error."""
    assert _normalize_schema_scope(raw) == _SCHEMA_SCOPE_ALL


# ---------------------------------------------------------------------------
# _run_scoped_vector_search — ALL fan-out vs scoped mode
# ---------------------------------------------------------------------------


class _FakeVectorStore:
    """Records every search() invocation and returns canned results.

    The W79 fan-out path issues one search per discovered schema. The
    test asserts both that the fan-out happened (calls record matches
    the expected schemas) and that each call's schema_filter was the
    expected per-schema TAG value.
    """

    def __init__(self, hits_by_schema: dict[str, list[dict]] | None = None):
        self.hits_by_schema = hits_by_schema or {}
        self.calls: list[dict] = []

    async def search(
        self,
        *,
        query_embedding,
        top_k,
        module_filter=None,
        schema_filter=None,
    ):
        self.calls.append(
            {
                "top_k": top_k,
                "schema_filter": schema_filter,
                "module_filter": module_filter,
            }
        )
        # ALL fan-out: schema_filter is one of the discovered schemas;
        # scoped mode: schema_filter is the user's choice; legacy
        # (no _graph_redis): schema_filter is None — unfiltered.
        if schema_filter is None:
            return [hit for hits in self.hits_by_schema.values() for hit in hits]
        return list(self.hits_by_schema.get(schema_filter, []))


async def test_scoped_vector_search_fans_out_all_mode(monkeypatch):
    """ALL must call vector_store.search once per discovered schema
    with a per-schema TAG filter, not a single global KNN."""
    fake_vs = _FakeVectorStore(
        hits_by_schema={
            "OFSMDM": [
                {"function_name": "FN_LOAD_OPS_RISK_DATA", "schema": "OFSMDM", "score": 0.42}
            ],
            "OFSERM": [
                {"function_name": "CS_Goodwill_Calculation", "schema": "OFSERM", "score": 0.38}
            ],
        }
    )
    monkeypatch.setattr(main_mod, "_vector_store", fake_vs)
    monkeypatch.setattr(main_mod, "_graph_redis", object())  # non-None
    monkeypatch.setattr(
        main_mod, "discovered_schemas", lambda _redis: ["OFSMDM", "OFSERM"]
    )

    results, contributed = await _run_scoped_vector_search(
        query_embedding=[0.0] * 4,
        schema_scope="ALL",
        top_k=5,
    )

    assert [c["schema_filter"] for c in fake_vs.calls] == ["OFSMDM", "OFSERM"], (
        "ALL must issue one search per discovered schema with a "
        "per-schema TAG filter — got " + repr(fake_vs.calls)
    )
    # Aggregated hits preserve per-schema attribution.
    schemas_in_results = {h["schema"] for h in results}
    assert schemas_in_results == {"OFSMDM", "OFSERM"}
    assert contributed == ["OFSMDM", "OFSERM"]


async def test_scoped_vector_search_skips_empty_schemas_in_contributed(monkeypatch):
    """Schemas that returned zero hits must not appear in the
    schemas_searched list — otherwise the UI chip would falsely
    advertise OFSMDM contribution for a query that only OFSERM
    answered."""
    fake_vs = _FakeVectorStore(
        hits_by_schema={
            "OFSMDM": [],  # empty
            "OFSERM": [
                {"function_name": "CS_Goodwill_Calculation", "schema": "OFSERM", "score": 0.4}
            ],
        }
    )
    monkeypatch.setattr(main_mod, "_vector_store", fake_vs)
    monkeypatch.setattr(main_mod, "_graph_redis", object())
    monkeypatch.setattr(
        main_mod, "discovered_schemas", lambda _redis: ["OFSMDM", "OFSERM"]
    )

    results, contributed = await _run_scoped_vector_search(
        query_embedding=[0.0] * 4,
        schema_scope="ALL",
        top_k=5,
    )

    assert contributed == ["OFSERM"]
    assert all(h["schema"] == "OFSERM" for h in results)


async def test_scoped_vector_search_specific_schema_passes_filter(monkeypatch):
    """Scoped mode must pass schema_filter through to the vector
    store — that's the TAG pre-filter the KNN runs against."""
    fake_vs = _FakeVectorStore(
        hits_by_schema={
            "OFSMDM": [
                {"function_name": "FN_LOAD_OPS_RISK_DATA", "schema": "OFSMDM", "score": 0.42}
            ],
        }
    )
    monkeypatch.setattr(main_mod, "_vector_store", fake_vs)
    monkeypatch.setattr(main_mod, "_graph_redis", object())

    results, contributed = await _run_scoped_vector_search(
        query_embedding=[0.0] * 4,
        schema_scope="OFSMDM",
        top_k=5,
    )

    assert len(fake_vs.calls) == 1
    assert fake_vs.calls[0]["schema_filter"] == "OFSMDM"
    assert contributed == ["OFSMDM"]
    assert results[0]["schema"] == "OFSMDM"


async def test_scoped_vector_search_specific_schema_empty_contributed_on_miss(monkeypatch):
    """Scoped mode with no matches must return an empty contributed
    list — the meta event then correctly tells the UI nothing was
    found, instead of falsely advertising the scoped schema as a
    contributor."""
    fake_vs = _FakeVectorStore(hits_by_schema={"OFSMDM": []})
    monkeypatch.setattr(main_mod, "_vector_store", fake_vs)
    monkeypatch.setattr(main_mod, "_graph_redis", object())

    results, contributed = await _run_scoped_vector_search(
        query_embedding=[0.0] * 4,
        schema_scope="OFSMDM",
        top_k=5,
    )
    assert results == []
    assert contributed == []


# ---------------------------------------------------------------------------
# _build_scope_mismatch_response — payload shape
# ---------------------------------------------------------------------------


def test_build_scope_mismatch_response_shape():
    """The W79 cross-scope DECLINED payload must mirror the W37
    function_not_found shape so the frontend's DECLINED rendering path
    handles both without new branching."""
    payload = _build_scope_mismatch_response(
        requested_function="CS_Goodwill_Calculation",
        scoped_schema="OFSMDM",
        other_schemas=["OFSERM"],
        correlation_id="cid-123",
    )
    assert payload["type"] == "scope_mismatch"
    assert payload["status"] == "declined"
    assert payload["badge"] == "DECLINED"
    assert payload["validated"] is False
    assert payload["confidence"] == 0.0
    assert payload["requested_function"] == "CS_Goodwill_Calculation"
    assert payload["requested_schema"] == "OFSMDM"
    assert payload["available_schemas"] == ["OFSERM"]
    assert payload["schema_scope"] == "OFSMDM"
    assert payload["schema_searched"] == []
    # Warnings ride the existing W57/W46 channel so TrustBanner renders
    # this as a SCOPE_MISMATCH severity-typed flag.
    assert any(w.startswith("SCOPE_MISMATCH:") for w in payload["warnings"])
    # Body must point at the schema dropdown action.
    body = payload["explanation"]["markdown"]
    assert "OFSMDM" in body
    assert "OFSERM" in body
    assert "schema scope" in body.lower() or "all schemas" in body.lower()


# ---------------------------------------------------------------------------
# _run_scope_mismatch_precheck — D2 detection
# ---------------------------------------------------------------------------


def test_scope_mismatch_precheck_returns_none_for_all_mode(monkeypatch):
    """ALL mode never produces a scope mismatch — the user hasn't
    constrained anything."""
    monkeypatch.setattr(main_mod, "_graph_redis", object())
    out = _run_scope_mismatch_precheck(
        "How is CS_Goodwill_Calculation calculated?",
        schema_scope="ALL",
        correlation_id="cid",
    )
    assert out is None


def test_scope_mismatch_precheck_returns_none_when_function_in_scope(monkeypatch):
    """Function exists in the user's scoped schema → no mismatch.
    The normal pipeline runs unchanged."""
    monkeypatch.setattr(main_mod, "_graph_redis", object())
    monkeypatch.setattr(
        main_mod,
        "function_exists_in_graph",
        lambda fn, redis, schemas=None: schemas == ["OFSMDM"],
    )

    out = _run_scope_mismatch_precheck(
        "How does FN_LOAD_OPS_RISK_DATA work?",
        schema_scope="OFSMDM",
        correlation_id="cid",
    )
    assert out is None


def test_scope_mismatch_precheck_returns_none_when_function_nowhere(monkeypatch):
    """Function exists in NO schema → return None so the W37
    function-precheck owns the response (its 'not found' framing is
    more accurate than 'wrong scope')."""
    monkeypatch.setattr(main_mod, "_graph_redis", object())
    monkeypatch.setattr(
        main_mod, "function_exists_in_graph", lambda fn, redis, schemas=None: False
    )
    monkeypatch.setattr(
        main_mod, "discovered_schemas", lambda _redis: ["OFSMDM", "OFSERM"]
    )

    out = _run_scope_mismatch_precheck(
        "How does FN_TOTALLY_FAKE_FUNCTION work?",
        schema_scope="OFSMDM",
        correlation_id="cid",
    )
    assert out is None


def test_scope_mismatch_precheck_fires_when_function_in_other_schema(monkeypatch):
    """The interesting case: user scoped to OFSMDM, but the named
    function only exists in OFSERM. We must return a structured
    'wrong scope' payload pointing at OFSERM as the right scope."""
    monkeypatch.setattr(main_mod, "_graph_redis", object())

    def _exists(fn, redis, schemas=None):
        # Function lives in OFSERM only.
        if schemas is None:
            return True
        return "OFSERM" in schemas

    monkeypatch.setattr(main_mod, "function_exists_in_graph", _exists)
    monkeypatch.setattr(
        main_mod, "discovered_schemas", lambda _redis: ["OFSMDM", "OFSERM", "FSDM"]
    )

    out = _run_scope_mismatch_precheck(
        "How is CS_Goodwill_Calculation calculated?",
        schema_scope="OFSMDM",
        correlation_id="cid",
    )
    assert out is not None
    assert out["type"] == "scope_mismatch"
    assert out["badge"] == "DECLINED"
    assert out["requested_schema"] == "OFSMDM"
    assert "OFSERM" in out["available_schemas"]
    # No false positives on FSDM — it doesn't host the function.
    assert "FSDM" not in out["available_schemas"]


def test_scope_mismatch_precheck_returns_none_without_named_function(monkeypatch):
    """A query without any function-shaped identifier (e.g. a column
    trace or a bare CAP-code) doesn't trigger scope_mismatch — the
    W79 detection is anchored on a named PL/SQL function token.
    Column / BI routing has its own multi-schema handling."""
    monkeypatch.setattr(main_mod, "_graph_redis", object())
    out = _run_scope_mismatch_precheck(
        "what is the total N_EOP_BAL on 2025-12-31?",
        schema_scope="OFSMDM",
        correlation_id="cid",
    )
    assert out is None


# ---------------------------------------------------------------------------
# _run_bi_scope_mismatch_precheck — D2 detection for CAP-code queries
# ---------------------------------------------------------------------------


def _make_bi_state(query: str, query_type: str = "COLUMN_LOGIC", **extra):
    """Compact LogicState fixture for BI precheck tests."""
    state = {
        "raw_query": query,
        "query_type": query_type,
        "target_variable": extra.pop("target_variable", ""),
    }
    state.update(extra)
    return state


def test_bi_scope_mismatch_returns_none_for_all_mode(monkeypatch):
    monkeypatch.setattr(main_mod, "_graph_redis", object())
    out = _run_bi_scope_mismatch_precheck(
        _make_bi_state("How is CAP973 calculated?"),
        schema_scope="ALL",
        correlation_id="cid",
    )
    assert out is None


def test_bi_scope_mismatch_returns_none_when_identifier_in_scope(monkeypatch):
    """CAP-code resolves under the user's chosen scope → BI runs as
    normal; precheck returns None."""
    monkeypatch.setattr(main_mod, "_graph_redis", object())
    monkeypatch.setattr(
        main_mod, "extract_function_candidates", lambda q: []
    )
    monkeypatch.setattr(
        main_mod, "detect_business_identifiers", lambda q, p: ["CAP973"]
    )
    monkeypatch.setattr(
        main_mod,
        "resolve_bi_to_function",
        lambda ident, redis, schemas=None: (
            {"function": "CS_X", "schema": schemas[0]}
            if schemas == ["OFSERM"]
            else None
        ),
    )

    out = _run_bi_scope_mismatch_precheck(
        _make_bi_state("How is CAP973 calculated?"),
        schema_scope="OFSERM",
        correlation_id="cid",
    )
    assert out is None


def test_bi_scope_mismatch_fires_when_identifier_only_in_other_schema(monkeypatch):
    """CAP-code lives in OFSERM only, user scoped to OFSMDM → return
    a structured scope-mismatch payload pointing at OFSERM."""
    monkeypatch.setattr(main_mod, "_graph_redis", object())
    monkeypatch.setattr(
        main_mod, "extract_function_candidates", lambda q: []
    )
    monkeypatch.setattr(
        main_mod, "detect_business_identifiers", lambda q, p: ["CAP973"]
    )

    def _resolve(ident, redis, schemas=None):
        if schemas == ["OFSERM"]:
            return {"function": "CS_Goodwill_Calculation", "schema": "OFSERM"}
        return None

    monkeypatch.setattr(main_mod, "resolve_bi_to_function", _resolve)
    monkeypatch.setattr(
        main_mod, "discovered_schemas", lambda _redis: ["OFSMDM", "OFSERM"]
    )

    out = _run_bi_scope_mismatch_precheck(
        _make_bi_state("How is CAP973 calculated?"),
        schema_scope="OFSMDM",
        correlation_id="cid",
    )
    assert out is not None
    assert out["type"] == "scope_mismatch"
    assert out["badge"] == "DECLINED"
    assert out["requested_function"] == "CAP973"
    assert out["available_schemas"] == ["OFSERM"]
    assert out["requested_schema"] == "OFSMDM"


def test_bi_scope_mismatch_returns_none_when_identifier_nowhere(monkeypatch):
    """CAP-code matched the BI pattern but resolves nowhere — let the
    normal flow run (it'll produce a W45 / empty-retrieval response)."""
    monkeypatch.setattr(main_mod, "_graph_redis", object())
    monkeypatch.setattr(
        main_mod, "extract_function_candidates", lambda q: []
    )
    monkeypatch.setattr(
        main_mod, "detect_business_identifiers", lambda q, p: ["CAP999"]
    )
    monkeypatch.setattr(
        main_mod, "resolve_bi_to_function", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        main_mod, "discovered_schemas", lambda _redis: ["OFSMDM", "OFSERM"]
    )

    out = _run_bi_scope_mismatch_precheck(
        _make_bi_state("How is CAP999 calculated?"),
        schema_scope="OFSMDM",
        correlation_id="cid",
    )
    assert out is None


def test_bi_scope_mismatch_explicit_function_override_wins(monkeypatch):
    """When the user names a function that lives under the chosen
    scope, that function wins over any CAP-code in the same query —
    matches the explicit-function override BI itself uses."""
    monkeypatch.setattr(main_mod, "_graph_redis", object())
    monkeypatch.setattr(
        main_mod,
        "extract_function_candidates",
        lambda q: ["FN_LOAD_OPS_RISK_DATA"],
    )
    monkeypatch.setattr(
        main_mod,
        "function_exists_in_graph",
        lambda fn, redis, schemas=None: schemas == ["OFSMDM"],
    )

    out = _run_bi_scope_mismatch_precheck(
        _make_bi_state(
            "How does FN_LOAD_OPS_RISK_DATA compute CAP973?"
        ),
        schema_scope="OFSMDM",
        correlation_id="cid",
    )
    assert out is None


def test_bi_scope_mismatch_variable_trace_inspects_target_variable(monkeypatch):
    """VARIABLE_TRACE queries put the BI identifier in target_variable
    rather than raw_query (matches BI's own gating). The precheck
    should look there instead of the prose."""
    monkeypatch.setattr(main_mod, "_graph_redis", object())
    monkeypatch.setattr(
        main_mod, "extract_function_candidates", lambda q: []
    )

    seen = {}

    def _detect(haystack, _patterns):
        seen["haystack"] = haystack
        return ["CAP973"] if haystack == "CAP973" else []

    monkeypatch.setattr(main_mod, "detect_business_identifiers", _detect)

    def _resolve(ident, redis, schemas=None):
        if schemas == ["OFSERM"]:
            return {"function": "CS_X", "schema": "OFSERM"}
        return None

    monkeypatch.setattr(main_mod, "resolve_bi_to_function", _resolve)
    monkeypatch.setattr(
        main_mod, "discovered_schemas", lambda _redis: ["OFSMDM", "OFSERM"]
    )

    out = _run_bi_scope_mismatch_precheck(
        _make_bi_state(
            "what writes this metric?",
            query_type="VARIABLE_TRACE",
            target_variable="CAP973",
        ),
        schema_scope="OFSMDM",
        correlation_id="cid",
    )
    assert out is not None
    assert seen["haystack"] == "CAP973"
