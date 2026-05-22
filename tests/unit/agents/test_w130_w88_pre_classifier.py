"""W130 — Tests for pre-classifier W88 detection wiring.

W130 moves W88 detection upstream of both W87 and the LLM classifier so
the static decline registry (LCR / NSFR / Leverage Ratio) stops being
shadowed by the unrecognized-term gate or the DATA_QUERY MIS-date
clarification. The pipeline-level wiring lives in :mod:`src.main` (event
stream pre-LLM block + MIS-date gate relaxation) and is exercised by
canaries; these unit tests pin the detection surface and the
cross-fix non-interference contract:

- `detect_named_computation` matches F1 / F2 / F3 queries (the
  decline-arm shadowing cases that motivated W130).
- The C14 forecasting boundary is preserved — "next quarter" must not
  shift to W88 routing.
- The existing anchor-arm surface (CET1 / BIA / CAR) still matches —
  W130 does not narrow or widen W88 patterns; it only changes WHEN
  detection runs.
- W127 calendar stopwords and W121-broad-2 priority-1 synthesis
  rejection remain unaffected by W130. They fire in W87
  (post-classifier); W130 fires before. They share no code path but
  share a test file's worth of regression risk.
"""

from __future__ import annotations

import pytest

from src.agents.computation_router import (
    W88_NAMED_COMPUTATIONS,
    detect_named_computation,
)
from src.agents.orchestrator import _extract_unrecognized_term


# ---------------------------------------------------------------------------
# Baseline contract — F1/F2/F3 reach W88 patterns when called as W130 will
# call them (with query_type="DATA_QUERY" stamped as a literal, since the
# pre-classifier hook hasn't seen the LLM's classification yet)
# ---------------------------------------------------------------------------

class TestW130DeclineArmDetection:
    """The three decline-arm computations LCR / NSFR / Leverage Ratio
    must match when W130 calls `detect_named_computation` pre-classifier.
    These are the F1 / F2 / F3 baseline failure cases.
    """

    def test_f1_lcr_query_matches_decline_arm(self):
        match = detect_named_computation(
            raw_query="How is LCR computed?",
            query_type="DATA_QUERY",
        )
        assert match is not None
        assert match.definition.name == "LCR"
        assert match.definition.arm == "decline"

    def test_f1_lowercase_lcr_matches(self):
        """Patterns are case-insensitive per IGNORECASE flag."""
        match = detect_named_computation(
            raw_query="how is lcr computed?",
            query_type="DATA_QUERY",
        )
        assert match is not None
        assert match.definition.name == "LCR"

    def test_f1_liquidity_coverage_ratio_long_form_matches(self):
        match = detect_named_computation(
            raw_query="What's the Liquidity Coverage Ratio?",
            query_type="DATA_QUERY",
        )
        assert match is not None
        assert match.definition.name == "LCR"

    def test_f2_nsfr_query_matches_decline_arm(self):
        match = detect_named_computation(
            raw_query="How does the bank compute NSFR?",
            query_type="DATA_QUERY",
        )
        assert match is not None
        assert match.definition.name == "NSFR"
        assert match.definition.arm == "decline"

    def test_f2_net_stable_funding_ratio_long_form_matches(self):
        match = detect_named_computation(
            raw_query="What's the Net Stable Funding Ratio?",
            query_type="DATA_QUERY",
        )
        assert match is not None
        assert match.definition.name == "NSFR"

    def test_f3_leverage_ratio_query_matches_decline_arm(self):
        match = detect_named_computation(
            raw_query="What's the Leverage Ratio for this run?",
            query_type="DATA_QUERY",
        )
        assert match is not None
        assert match.definition.name == "LEVERAGE_RATIO"
        assert match.definition.arm == "decline"

    def test_f3_decline_includes_cap214_alternative_suggestion(self):
        """The Leverage Ratio decline carries an alternative-metric
        suggestion (CAP214 = Tier 1 Capital Ratio). W130's win is
        surfacing this text to the user — verify it's present in the
        registry so the decline payload includes it."""
        match = detect_named_computation(
            raw_query="What's the Leverage Ratio for this run?",
            query_type="DATA_QUERY",
        )
        assert match is not None
        assert match.definition.decline_alternative is not None
        assert "CAP214" in match.definition.decline_alternative


# ---------------------------------------------------------------------------
# C14 boundary — UNSUPPORTED forecasting classifier MUST NOT shift to W88
# ---------------------------------------------------------------------------

class TestW130BoundaryC14ForecastingPreserved:
    def test_c14_next_quarter_does_not_match_w88(self):
        """C14's forecasting query ('Which accounts are likely to fail
        next quarter?') must continue to route via UNSUPPORTED. W88
        patterns are deliberately narrow — no quarter / time-period
        pattern overlap with "next quarter" prose.
        """
        match = detect_named_computation(
            raw_query="Which accounts are likely to fail next quarter?",
            query_type="DATA_QUERY",
        )
        assert match is None

    def test_c14_variants_do_not_match_w88(self):
        """Defensive — common phrasings of the C14 forecasting intent
        all stay out of W88."""
        forecasting_queries = [
            "Predict next quarter's losses",
            "Which loans will default this quarter?",
            "Forecast Tier 1 capital next year",
        ]
        for q in forecasting_queries:
            match = detect_named_computation(
                raw_query=q, query_type="DATA_QUERY",
            )
            # "Forecast Tier 1 capital next year" intentionally contains
            # the "tier 1" token — the W88 TIER1 pattern requires
            # "tier 1 capital ratio" or "T1 capital ratio" or "CAP214",
            # not bare "tier 1 capital". Confirm narrowness.
            if match is not None:
                # If any future widening makes this match, we want a loud
                # failure pinning the regression.
                pytest.fail(
                    f"W88 incorrectly matched forecasting query: {q!r} "
                    f"→ {match.definition.name}"
                )


# ---------------------------------------------------------------------------
# Anchor-arm regression — existing positive surface preserved
# ---------------------------------------------------------------------------

class TestW130AnchorArmRegression:
    """Per W130 contract: patterns unchanged, only call timing changes.
    The 6 anchor-arm computations must continue to match exactly as
    they did pre-W130.
    """

    def test_cet1_anchor_arm_matches_with_mis_date(self):
        """Case 1 from Toheed's three-case matrix: anchor arm WITH MIS
        date — should behave identically pre- and post-W130 because the
        MIS-date relaxation doesn't matter when a date is present."""
        match = detect_named_computation(
            raw_query="What is the CET1 ratio on 2025-12-31?",
            query_type="DATA_QUERY",
        )
        assert match is not None
        assert match.definition.name == "CET1"
        assert match.definition.arm == "anchor"
        assert match.definition.filter_code == "CAP960"

    def test_cet1_anchor_arm_matches_without_mis_date(self):
        """Case 2 from Toheed's three-case matrix: anchor arm WITHOUT
        MIS date. This is the documented W88b case that W130 closes —
        pre-W130 the MIS-date gate fires a clarification before W88
        runs."""
        match = detect_named_computation(
            raw_query="What is the CET1 ratio?",
            query_type="DATA_QUERY",
        )
        assert match is not None
        assert match.definition.name == "CET1"
        assert match.definition.arm == "anchor"

    def test_bia_anchor_arm_matches(self):
        match = detect_named_computation(
            raw_query="What is the BIA capital charge?",
            query_type="DATA_QUERY",
        )
        assert match is not None
        assert match.definition.name == "BIA"
        assert match.definition.arm == "anchor"

    def test_car_anchor_arm_matches(self):
        match = detect_named_computation(
            raw_query="What is the Capital Adequacy Ratio?",
            query_type="DATA_QUERY",
        )
        assert match is not None
        assert match.definition.name == "CAR"
        assert match.definition.arm == "anchor"

    def test_credit_rwa_anchor_arm_matches(self):
        match = detect_named_computation(
            raw_query="What is the Credit RWA?",
            query_type="DATA_QUERY",
        )
        assert match is not None
        assert match.definition.name == "CREDIT_RWA_AGG"
        assert match.definition.arm == "anchor"


# ---------------------------------------------------------------------------
# Cross-fix non-interference — W127 and W121-broad-2 surfaces unchanged
# ---------------------------------------------------------------------------

class TestW130DoesNotRegressPriorFixes:
    """W130 fires pre-classifier in main.py; W127 calendar stopwords and
    W121-broad-2 priority-1 synthesis-rejection both fire inside W87
    (post-classifier in `_extract_unrecognized_term`). The code paths
    don't intersect, but pin the surfaces so a future refactor can't
    silently break them.
    """

    def test_w127_december_stopword_unaffected(self):
        """C3 baseline pin (W127): December must remain a stopword,
        filtered from W87 priority-4 extraction."""
        result = _extract_unrecognized_term(
            "Where is the December-only execution gate set?", "",
        )
        assert result is None or result.upper() != "DECEMBER"

    def test_w127_friday_stopword_unaffected(self):
        result = _extract_unrecognized_term(
            "Which functions ran on Friday?", "",
        )
        assert result is None or result.upper() != "FRIDAY"

    def test_w121b2_lve_cap_synthesis_rejection_unaffected(self):
        """A4 baseline pin (W121-broad-2): synthesized LVE_CAP must
        still be rejected by priority 1, falling through to LVE via
        priority 4."""
        result = _extract_unrecognized_term(
            "What's the LVE cap?", "LVE_CAP",
        )
        assert result != "LVE_CAP"
        assert result == "LVE" or result is None

    def test_w121b2_rrp_eligibility_synthesis_rejection_unaffected(self):
        """B1 baseline pin (W121-broad-2): RRP_ELIGIBILITY rejected."""
        result = _extract_unrecognized_term(
            "What enforces RRP eligibility?", "RRP_ELIGIBILITY",
        )
        assert result != "RRP_ELIGIBILITY"
        assert result == "RRP" or result is None

    def test_w121b2_verbatim_underscored_target_still_accepted(self):
        """N_EOP_BAL verbatim in query must remain extractable via
        priority 1 (rule (b) trivially false → ACCEPT)."""
        result = _extract_unrecognized_term(
            "What is the total N_EOP_BAL for 2025-12-31?", "N_EOP_BAL",
        )
        assert result == "N_EOP_BAL"


# ---------------------------------------------------------------------------
# Registry shape — pin so future widening / narrowing is loud
# ---------------------------------------------------------------------------

class TestW130RegistryShapeUnchanged:
    """W130 modifies WHEN W88 fires, not WHAT it fires on. Pin the
    registry shape so a future refactor that widens or narrows patterns
    surfaces as a loud test failure rather than a silent canary shift.
    """

    def test_w88_registry_size_unchanged(self):
        """6 anchor + 3 decline = 9 items, per W88 diagnostic Section 5."""
        anchors = [d for d in W88_NAMED_COMPUTATIONS if d.arm == "anchor"]
        declines = [d for d in W88_NAMED_COMPUTATIONS if d.arm == "decline"]
        assert len(anchors) == 6
        assert len(declines) == 3

    def test_w88_decline_names_unchanged(self):
        """LCR / NSFR / LEVERAGE_RATIO are W130's contract surface."""
        decline_names = {
            d.name for d in W88_NAMED_COMPUTATIONS if d.arm == "decline"
        }
        assert decline_names == {"LCR", "NSFR", "LEVERAGE_RATIO"}
