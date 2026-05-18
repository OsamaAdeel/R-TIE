"""W88 — Tests for the named regulatory computation pre-router.

Pre-W88 every DATA_QUERY query naming a Basel-defined regulatory
computation (BIA, CET1, CAR, ...) was sent to the LLM SQL generator
with a catalog dominated by OFSMDM staging tables. The LLM fabricated
SQL on ``ABL_OPS_RISK_DATA`` regardless of what computation the user
named — wrong table, wrong formula, wrong schema, and the existing
trust gates (W57 / W86) only catch the result-shape symptoms, not the
routing decision.

W88 introduces a deterministic pre-router that recognises the named
computation in the raw query and short-circuits LLM generation with
canonical SQL against the OFSERM fact table. These tests pin:

- the registry shape (6 anchor + 3 decline items, no dup names)
- the detection surface (positive matches per computation, mixed-case
  / hyphen / underscore variants, embedding in longer queries)
- negative cases (other query types, empty input, generic data queries)
- SKEY resolution (lazy lookup, module-global cache, decline on
  lookup failure — never ship a guessed SKEY)
- plan-building (SQL shape per anchor family, params binding)
- decline payload (pure decline vs skey-unresolved fallback)

These tests do NOT exercise the data_query.py wiring or run any SQL
against Oracle — that's the integration canary surface
([tests/integration/test_live_stream.py](../../integration/test_live_stream.py)
W88 canaries).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.agents.computation_router import (
    W88_NAMED_COMPUTATIONS,
    W88AnchorMatch,
    W88ComputationDefinition,
    build_anchor_plan,
    build_decline_payload,
    detect_named_computation,
    reset_skey_cache,
    resolve_skey_for_anchor,
)


@pytest.fixture(autouse=True)
def _reset_skey_cache_between_tests():
    """Module-global SKEY cache must not leak across tests."""
    reset_skey_cache()
    yield
    reset_skey_cache()


# ---------------------------------------------------------------------------
# Registry integrity — pin shape so a downgrade is loud
# ---------------------------------------------------------------------------

class TestRegistryIntegrity:
    def test_v1_has_six_anchor_and_three_decline(self):
        """Diagnostic Section 5 recommended v1 = 6 anchor + 3 decline.
        If a future ticket promotes a decline-arm item (e.g. TSA) to
        anchor, this test fires — the change needs an audit.
        """
        anchors = [d for d in W88_NAMED_COMPUTATIONS if d.arm == "anchor"]
        declines = [d for d in W88_NAMED_COMPUTATIONS if d.arm == "decline"]
        assert len(anchors) == 6, (
            f"Expected 6 anchor items, got {len(anchors)}: "
            f"{[d.name for d in anchors]}"
        )
        assert len(declines) == 3, (
            f"Expected 3 decline items, got {len(declines)}: "
            f"{[d.name for d in declines]}"
        )

    def test_no_duplicate_names(self):
        """Each computation's `name` must be unique — names are used
        as anchor identifiers in telemetry and canary assertions.
        """
        names = [d.name for d in W88_NAMED_COMPUTATIONS]
        assert len(names) == len(set(names)), (
            f"Duplicate names in registry: {names}"
        )

    def test_anchor_arm_definitions_are_complete(self):
        """Anchor entries must have schema, table, result_column, and
        a filter declaration. Missing fields would crash plan-building.
        """
        for defn in W88_NAMED_COMPUTATIONS:
            if defn.arm == "anchor":
                assert defn.target_schema, f"{defn.name} missing target_schema"
                assert defn.target_table, f"{defn.name} missing target_table"
                assert defn.result_column, f"{defn.name} missing result_column"
                assert defn.filter_kind in ("method_skey", "cap_code"), (
                    f"{defn.name} has invalid filter_kind {defn.filter_kind!r}"
                )
                assert defn.filter_code, f"{defn.name} missing filter_code"

    def test_decline_arm_definitions_have_reasons(self):
        """Decline entries must have a decline_reason — that's the
        user-facing message. The alternative is optional.
        """
        for defn in W88_NAMED_COMPUTATIONS:
            if defn.arm == "decline":
                assert defn.decline_reason, f"{defn.name} missing decline_reason"


# ---------------------------------------------------------------------------
# detect_named_computation — positive cases
# ---------------------------------------------------------------------------

class TestPositiveMatches:
    """One test per registered named computation. If a future ticket
    re-names a pattern or drops one, the matching case fails before
    the canary catches it at integration time.
    """

    @pytest.mark.parametrize("query,expected_name", [
        # BIA — long name, abbreviation, short variant
        ("What is the operational risk capital charge under Basic Indicator Approach?", "BIA"),
        ("Calculate BIA for the latest run", "BIA"),
        ("basic-indicator approach result", "BIA"),
        # Credit RWA aggregate
        ("What is the total Credit Risk RWA?", "CREDIT_RWA_AGG"),
        ("Show me the RWA for credit risk", "CREDIT_RWA_AGG"),
        ("CAP169 value", "CREDIT_RWA_AGG"),
        # Market RWA aggregate
        ("Total Market Risk RWA on 2025-12-31", "MARKET_RWA_AGG"),
        ("RWA for market risk", "MARKET_RWA_AGG"),
        ("CAP090 amount", "MARKET_RWA_AGG"),
        # CET1
        ("What is the CET1 ratio?", "CET1"),
        ("Show Common Equity Tier 1 ratio", "CET1"),
        ("CAP960", "CET1"),
        # Tier 1
        ("Tier 1 capital ratio for ABL", "TIER1"),
        ("What's the T1 ratio?", "TIER1"),
        ("CAP214 value", "TIER1"),
        # CAR
        ("Total Capital Ratio on 2025-12-31", "CAR"),
        ("Capital Adequacy Ratio", "CAR"),
        ("CAP192", "CAR"),
        # Leverage (decline)
        ("Compute the leverage ratio", "LEVERAGE_RATIO"),
        ("CAP843 value", "LEVERAGE_RATIO"),
        # LCR (decline)
        ("Liquidity Coverage Ratio for 2025", "LCR"),
        ("What is the LCR?", "LCR"),
        # NSFR (decline)
        ("Net Stable Funding Ratio result", "NSFR"),
        ("Show NSFR", "NSFR"),
    ])
    def test_detects_named_computation(self, query, expected_name):
        match = detect_named_computation(query, "DATA_QUERY")
        assert match is not None, f"No match for {query!r}"
        assert match.definition.name == expected_name, (
            f"Expected {expected_name} for {query!r}, "
            f"got {match.definition.name}"
        )


# ---------------------------------------------------------------------------
# detect_named_computation — negative cases
# ---------------------------------------------------------------------------

class TestNegativeMatches:
    @pytest.mark.parametrize("query,query_type", [
        ("How is N_EOP_BAL calculated?", "VARIABLE_TRACE"),
        ("What does FN_LOAD_OPS_RISK_DATA do?", "FUNCTION_LOGIC"),
        ("Compute CET1 ratio", "FUNCTION_LOGIC"),  # right keyword, wrong type
        ("Trace BIA through the chain", "VARIABLE_TRACE"),  # right keyword, wrong type
    ])
    def test_non_data_query_type_returns_none(self, query, query_type):
        """W88 should only fire on DATA_QUERY; other types have their
        own anchor paths (W76, BI routing). 'How is BIA calculated?'
        is FUNCTION_LOGIC — user wants logic, not a number.
        """
        assert detect_named_computation(query, query_type) is None

    @pytest.mark.parametrize("query", [
        "",
        "   ",
        "\n\t  \n",
    ])
    def test_empty_query_returns_none(self, query):
        assert detect_named_computation(query, "DATA_QUERY") is None

    def test_none_query_returns_none(self):
        assert detect_named_computation(None, "DATA_QUERY") is None

    def test_non_string_query_returns_none(self):
        """Defensive — state.get('raw_query') might be a dict shape
        in some legacy code paths. Don't crash on the type mismatch.
        """
        assert detect_named_computation(123, "DATA_QUERY") is None
        assert detect_named_computation({"q": "BIA"}, "DATA_QUERY") is None

    @pytest.mark.parametrize("query", [
        "What's the sum of N_EOP_BAL for ABL?",
        "How many rows in FCT_LOAN_ASSET?",
        "List all entities in 2025",
        "Show transactions over 1 million",
        "What columns are on STG_OPS_RISK_DATA?",
    ])
    def test_generic_data_query_returns_none(self, query):
        """Existing DATA_QUERY canaries (N_EOP_BAL, ABL transactions,
        etc.) must NOT match W88 — pre-router is a no-op for them.
        Regression-protective: any false positive here means an
        existing canary will start short-circuiting through W88.
        """
        assert detect_named_computation(query, "DATA_QUERY") is None


# ---------------------------------------------------------------------------
# Edge cases — variant spellings, embedded in longer queries
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_case_insensitive_match(self):
        """Patterns compile with re.IGNORECASE — uppercase / lowercase /
        mixed should all match.
        """
        for q in ("bia", "BIA", "Bia", "bIa"):
            assert (
                detect_named_computation(f"compute {q} charge", "DATA_QUERY")
                is not None
            ), f"Case variant {q!r} missed"

    def test_hyphen_underscore_separator_variants(self):
        """Patterns accept space, hyphen, or underscore between words."""
        for q in (
            "common equity tier 1 ratio",
            "common-equity-tier-1 ratio",
            "common_equity_tier_1 ratio",
        ):
            match = detect_named_computation(q, "DATA_QUERY")
            assert match is not None, f"Separator variant {q!r} missed"
            assert match.definition.name == "CET1"

    def test_match_within_longer_question(self):
        """The pattern is `search`, not `fullmatch` — the named
        computation can appear anywhere in the query.
        """
        q = "Hi, can you tell me what the CET1 ratio is for ABL on 2025-12-31, please?"
        match = detect_named_computation(q, "DATA_QUERY")
        assert match is not None
        assert match.definition.name == "CET1"

    def test_registry_order_determinism(self):
        """When a query could match multiple patterns, the first-declared
        wins. Pin this so the registry order has predictable semantics.
        BIA's `\\bBIA\\b` is declared after the long-name pattern; both
        match `"Basic Indicator Approach (BIA)"` but `Basic Indicator
        Approach` is checked first because the long-name patterns
        appear first in the registry.
        """
        match = detect_named_computation(
            "Basic Indicator Approach (BIA) charge", "DATA_QUERY"
        )
        assert match is not None
        assert match.definition.name == "BIA"


# ---------------------------------------------------------------------------
# SKEY resolution — async, mocked schema_tools
# ---------------------------------------------------------------------------

@pytest.fixture
def bia_anchor():
    bia_def = next(d for d in W88_NAMED_COMPUTATIONS if d.name == "BIA")
    return W88AnchorMatch(definition=bia_def, matched_pattern=r"\bBIA\b")


@pytest.fixture
def cet1_anchor():
    cet1_def = next(d for d in W88_NAMED_COMPUTATIONS if d.name == "CET1")
    return W88AnchorMatch(definition=cet1_def, matched_pattern=r"\bCET[\s\-_]*1\b")


class TestSkeyResolution:
    async def test_method_skey_lookup_caches_success(self, bia_anchor):
        """First call probes Oracle; second call returns cached value
        without re-probing. The cache is the module-global; reset
        between tests via the autouse fixture.
        """
        schema_tools = AsyncMock()
        schema_tools.execute_raw = AsyncMock(return_value=[(115,)])

        skey1 = await resolve_skey_for_anchor(bia_anchor, schema_tools)
        skey2 = await resolve_skey_for_anchor(bia_anchor, schema_tools)

        assert skey1 == 115
        assert skey2 == 115
        schema_tools.execute_raw.assert_called_once()  # cached on 2nd call

    async def test_method_skey_lookup_caches_failure(self, bia_anchor):
        """Lookup failure also caches — None is sticky. A retry on a
        misconfigured catalog would otherwise hammer Oracle on every
        query naming the failing computation.
        """
        schema_tools = AsyncMock()
        schema_tools.execute_raw = AsyncMock(side_effect=RuntimeError("oracle down"))

        skey1 = await resolve_skey_for_anchor(bia_anchor, schema_tools)
        skey2 = await resolve_skey_for_anchor(bia_anchor, schema_tools)

        assert skey1 is None
        assert skey2 is None
        schema_tools.execute_raw.assert_called_once()

    async def test_method_skey_lookup_returns_none_on_empty_result(self, bia_anchor):
        """DIM_BASEL_METHODOLOGY exists but the code we asked for isn't
        in there — OFSAA upgrade scenario. Don't guess; return None
        and let the caller decline.
        """
        schema_tools = AsyncMock()
        schema_tools.execute_raw = AsyncMock(return_value=[])

        skey = await resolve_skey_for_anchor(bia_anchor, schema_tools)
        assert skey is None

    async def test_method_skey_lookup_returns_none_when_skey_is_null(self, bia_anchor):
        """Row returned but value is NULL — defensive against DIM
        seed-data anomalies. Treat as not-found.
        """
        schema_tools = AsyncMock()
        schema_tools.execute_raw = AsyncMock(return_value=[(None,)])

        skey = await resolve_skey_for_anchor(bia_anchor, schema_tools)
        assert skey is None

    async def test_cap_code_anchor_does_not_probe(self, cet1_anchor):
        """cap_code anchors don't need a SKEY lookup — the CAP code is
        the filter. resolve_skey_for_anchor must NOT call Oracle for
        these.
        """
        schema_tools = AsyncMock()
        schema_tools.execute_raw = AsyncMock(return_value=[(42,)])

        skey = await resolve_skey_for_anchor(cet1_anchor, schema_tools)
        assert skey is None
        schema_tools.execute_raw.assert_not_called()


# ---------------------------------------------------------------------------
# build_anchor_plan — SQL shape per anchor family
# ---------------------------------------------------------------------------

class TestBuildAnchorPlan:
    def test_method_skey_plan_has_skey_param(self, bia_anchor):
        plan = build_anchor_plan(bia_anchor, skey=115)
        assert plan is not None
        assert plan["params"] == {"w88_skey": 115}
        assert ":w88_skey" in plan["sql"]
        assert "FCT_OPS_RISK_DATA" in plan["sql"]
        assert "N_BASEL_METHOD_SKEY" in plan["sql"]
        assert plan["query_kind"] == "AGGREGATE"
        assert plan["select_columns"] == ["BIA"]

    def test_method_skey_plan_carries_anchor_metadata(self, bia_anchor):
        plan = build_anchor_plan(bia_anchor, skey=115)
        assert "w88_anchor" in plan
        anchor_meta = plan["w88_anchor"]
        assert anchor_meta["name"] == "BIA"
        assert anchor_meta["target_table"] == "OFSERM.FCT_OPS_RISK_DATA"
        assert "N_BASEL_METHOD_SKEY=115" in anchor_meta["filter"]
        assert "ORBIA" in anchor_meta["filter"]

    def test_method_skey_plan_returns_none_when_skey_unresolved(self, bia_anchor):
        """Critical guarantee — never ship a guessed SKEY. When skey is
        None, build_anchor_plan returns None so the caller decline-falls.
        """
        assert build_anchor_plan(bia_anchor, skey=None) is None

    def test_cap_code_plan_has_cap_code_param(self, cet1_anchor):
        plan = build_anchor_plan(cet1_anchor, skey=None)  # skey ignored
        assert plan is not None
        assert plan["params"] == {"w88_cap_code": "CAP960"}
        assert ":w88_cap_code" in plan["sql"]
        assert "FCT_STANDARD_ACCT_HEAD" in plan["sql"]
        assert "DIM_STANDARD_ACCT_HEAD" in plan["sql"]
        assert "V_STD_ACCT_HEAD_ID" in plan["sql"]

    def test_cap_code_plan_uses_latest_run_dense_rank(self, cet1_anchor):
        """Capital ratios at FCT_STANDARD_ACCT_HEAD have multiple rows
        per CAP-code across runs / dates. The plan must scope to the
        latest (date, run) — pinning DENSE_RANK keeps the diagnostic's
        run-defaulting strategy in code.
        """
        plan = build_anchor_plan(cet1_anchor, skey=None)
        assert "DENSE_RANK" in plan["sql"]
        assert "N_MIS_DATE_SKEY DESC" in plan["sql"]
        assert "N_RUN_SKEY DESC" in plan["sql"]


# ---------------------------------------------------------------------------
# build_decline_payload — pure decline vs SKEY-unresolved fallback
# ---------------------------------------------------------------------------

@pytest.fixture
def lcr_anchor():
    lcr_def = next(d for d in W88_NAMED_COMPUTATIONS if d.name == "LCR")
    return W88AnchorMatch(definition=lcr_def, matched_pattern=r"\bLCR\b")


class TestBuildDeclinePayload:
    def test_pure_decline_returns_decline_reason(self, lcr_anchor):
        """LCR has no fact table in this OFSAA module. Reason from
        the registry; alternative is None.

        Note: ``badge`` is intentionally NOT set here — main.py's
        SSE wrapper at src/main.py:2061-2071 derives the badge from
        ``status``. status="unsupported" maps to REJECTED, which is
        what the integration canary asserts.
        """
        payload = build_decline_payload(
            anchor=lcr_anchor,
            user_query="What's the LCR?",
            correlation_id="corr-1",
        )
        assert payload["status"] == "unsupported"
        assert payload["sql"] is None
        assert "badge" not in payload, (
            "badge should be derived by main.py, not stamped here"
        )
        assert "OFSAA module" in payload["summary"]
        assert "Liquidity Coverage Ratio" in payload["explanation"]
        w88_meta = payload["w88_decline"]
        assert w88_meta["name"] == "LCR"
        assert w88_meta["skey_unresolved"] is False

    def test_leverage_decline_includes_alternative(self):
        """Leverage ratio decline points the user at the Tier 1 ratio
        which IS computed. The alternative line must surface in the
        explanation.
        """
        lev_def = next(
            d for d in W88_NAMED_COMPUTATIONS if d.name == "LEVERAGE_RATIO"
        )
        anchor = W88AnchorMatch(
            definition=lev_def,
            matched_pattern=r"\bleverage[\s\-_]+ratio\b",
        )
        payload = build_decline_payload(
            anchor=anchor,
            user_query="What is the leverage ratio?",
            correlation_id="corr-2",
        )
        assert "Tier 1" in payload["explanation"]
        assert "Alternative" in payload["explanation"]

    def test_skey_unresolved_fallback_explains_operational_cause(self, bia_anchor):
        """When an anchor-arm computation fails its SKEY lookup, the
        decline must surface the operational cause (DIM lookup failure)
        rather than implying the computation isn't defined.
        """
        payload = build_decline_payload(
            anchor=bia_anchor,
            user_query="What's the BIA charge?",
            correlation_id="corr-3",
            skey_unresolved=True,
        )
        assert payload["w88_decline"]["skey_unresolved"] is True
        assert "DIM_BASEL_METHODOLOGY" in payload["summary"]
        assert "ORBIA" in payload["summary"]
        assert "will not guess" in payload["summary"]

    def test_decline_payload_shape_matches_unsupported_contract(self, lcr_anchor):
        """The decline payload must satisfy the keys DataQueryAgent's
        existing 'unsupported' result emits. Missing keys here would
        break the frontend's response renderer.
        """
        payload = build_decline_payload(
            anchor=lcr_anchor,
            user_query="LCR",
            correlation_id="corr-4",
        )
        required_keys = {
            "status", "query_kind", "sql", "count_sql", "params",
            "rows", "columns", "row_count", "summary", "explanation",
            "sanity_warnings", "verification_sql", "correlation_id",
        }
        missing = required_keys - set(payload.keys())
        assert not missing, f"Missing keys: {missing}"
