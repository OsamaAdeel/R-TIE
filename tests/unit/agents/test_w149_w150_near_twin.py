"""W149 + W150 — sentinel-collision fix and near-twin disambiguation gate.

W149: the cascade L5 (semantic_top1) must select on a GENUINE cosine distance
([0, 2]); a W80c expansion-only candidate carries the out-of-band
``graph_rerank.NO_VECTOR_SCORE`` (9.0) sentinel and must never win L5 — and when
EVERY candidate is a sentinel, L5 returns None (no anchor) rather than anchoring
on a graph-expansion neighbour.

W150: :func:`detect_near_twin_ambiguity` hedges instead of confidently anchoring
when (a) the anchor came from the described-not-named L5 path
(``source == "semantic_top1"``), (b) the two closest genuine cosine candidates
form a near-twin stem cohort, and (c) their margin is < 0.05. It must NEVER fire
on a NAMED path (W76 / classifier / BI / raw_query_scan) — that is the whole
"never hedge a named query" guarantee.
"""

import pytest

from src.agents.anchor_resolution import (
    determine_primary_anchor,
    detect_near_twin_ambiguity,
    _w150_near_twin,
    _W150_MARGIN_MAX,
)
from src.agents.graph_rerank import NO_VECTOR_SCORE


def _ms(*pairs):
    """Build a multi_source dict from (fn, score) pairs."""
    return {fn: {"score": score} for fn, score in pairs}


# --------------------------------------------------------------------------- #
# W149 — L5 sentinel guard
# --------------------------------------------------------------------------- #

def test_w149_l5_excludes_expansion_sentinel():
    """A 9.0 sentinel must not beat a genuine cosine hit in L5 min-score."""
    state = {
        "raw_query": "how does the deduction proration work",
        "object_name": "",
        "multi_source": _ms(
            ("CS_EXPANSION_NEIGHBOUR", NO_VECTOR_SCORE),  # sentinel, MUST lose
            ("CS_GENUINE_HIT", 0.42),                     # real cosine, MUST win
        ),
    }
    anchor = determine_primary_anchor(state)
    assert anchor is not None
    assert anchor["function"] == "CS_GENUINE_HIT"
    assert anchor["source"] == "semantic_top1"
    assert anchor["confidence"] == "low"


def test_w149_l5_returns_none_when_only_sentinels():
    """When every candidate is an expansion sentinel, no semantic_top1 anchor."""
    state = {
        "raw_query": "how does the routine work",
        "object_name": "",
        "multi_source": _ms(
            ("CS_EXPANSION_A", NO_VECTOR_SCORE),
            ("CS_EXPANSION_B", NO_VECTOR_SCORE),
        ),
    }
    assert determine_primary_anchor(state) is None


def test_w149_sentinel_is_out_of_cosine_band():
    """Guard against the constant drifting back into the [0, 2] cosine band."""
    assert NO_VECTOR_SCORE > 2.0


# --------------------------------------------------------------------------- #
# W150 — near-twin cohort predicate
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("a,b", [
    ("CS_THRESHOLD_TREATMENT_AMOUNT_ABOVE_AGGREGATE_THRESHOLD_CALCULATION",
     "CS_THRESHOLD_TREATMENT_AMOUNT_BELOW_AGGREGATE_THRESHOLD_CALCULATION"),
    ("CS_INSIGNIFICANT_INVST_IN_NON_REGULATORY_CONSL_ENTITY_DEDUCTION_AMOUNT",
     "CS_INSIGNIFICANT_INVST_IN_NON_REGULATORY_CONSL_ENTITY_DEDUCTION_PERCENTAGE"),
    ("CS_GOODWILL_NET_OF_DTL_CALCULATION",
     "CS_GOODWILL_AND_OTHER_INTANGIBLE_ASSETS_NET_OF_DTL_CALCULATION"),
])
def test_w150_predicate_true_for_same_family(a, b):
    assert _w150_near_twin(a, b) is True


@pytest.mark.parametrize("a,b", [
    ("FN_LOAD_OPS_RISK_DATA", "EXCHANGE_RATE_DATA_POPULATION"),   # distinct
    ("AMORTIZATION_SCHEDULE_POP", "INDEX_COMPOSITION_POP"),       # distinct
    ("", "CS_GOODWILL_CALCULATION"),                              # empty
])
def test_w150_predicate_false_for_distinct(a, b):
    assert _w150_near_twin(a, b) is False


# --------------------------------------------------------------------------- #
# W150 — gate firing / gating
# --------------------------------------------------------------------------- #

_SEMANTIC = {"function": "X", "source": "semantic_top1", "confidence": "low"}
_TWIN_MS = _ms(
    ("CS_THRESHOLD_TREATMENT_AMOUNT_ABOVE_AGGREGATE_THRESHOLD_CALCULATION", 0.48),
    ("CS_THRESHOLD_TREATMENT_AMOUNT_BELOW_AGGREGATE_THRESHOLD_CALCULATION", 0.49),
)


def test_w150_fires_on_twin_cohort_small_margin():
    hedge = detect_near_twin_ambiguity({"multi_source": _TWIN_MS}, _SEMANTIC)
    assert hedge is not None
    assert hedge["top1"] == \
        "CS_THRESHOLD_TREATMENT_AMOUNT_ABOVE_AGGREGATE_THRESHOLD_CALCULATION"
    assert hedge["margin"] < _W150_MARGIN_MAX
    # siblings are the top-N cohort members, closest first
    assert hedge["siblings"][0] == hedge["top1"]
    assert len(hedge["siblings"]) == 2


@pytest.mark.parametrize("source", [
    "w76_prefix", "classifier_object", "bi_routing", "raw_query_scan",
])
def test_w150_never_fires_on_named_paths(source):
    """The 'never hedge a named query' guarantee."""
    anchor = {"function": "X", "source": source, "confidence": "high"}
    assert detect_near_twin_ambiguity({"multi_source": _TWIN_MS}, anchor) is None


def test_w150_does_not_fire_on_large_margin():
    ms = _ms(
        ("CS_THRESHOLD_TREATMENT_AMOUNT_ABOVE_AGGREGATE_THRESHOLD_CALCULATION", 0.30),
        ("CS_THRESHOLD_TREATMENT_AMOUNT_BELOW_AGGREGATE_THRESHOLD_CALCULATION", 0.49),
    )
    assert detect_near_twin_ambiguity({"multi_source": ms}, _SEMANTIC) is None


def test_w150_does_not_fire_on_distinct_top2():
    ms = _ms(
        ("FN_LOAD_OPS_RISK_DATA", 0.31),
        ("EXCHANGE_RATE_DATA_POPULATION", 0.33),
    )
    assert detect_near_twin_ambiguity({"multi_source": ms}, _SEMANTIC) is None


def test_w150_does_not_fire_on_sentinel_only():
    ms = _ms(("A_FOO", NO_VECTOR_SCORE), ("A_BAR", NO_VECTOR_SCORE))
    assert detect_near_twin_ambiguity({"multi_source": ms}, _SEMANTIC) is None


def test_w150_does_not_fire_on_none_anchor():
    assert detect_near_twin_ambiguity({"multi_source": _TWIN_MS}, None) is None


def test_w150_excludes_sentinel_from_cohort_ranking():
    """A 9.0 sentinel must not be ranked top-1/top-2 inside the gate either."""
    ms = _ms(
        ("CS_EXPANSION_NEIGHBOUR", NO_VECTOR_SCORE),  # excluded
        ("CS_THRESHOLD_TREATMENT_AMOUNT_ABOVE_AGGREGATE_THRESHOLD_CALCULATION", 0.48),
        ("CS_THRESHOLD_TREATMENT_AMOUNT_BELOW_AGGREGATE_THRESHOLD_CALCULATION", 0.49),
    )
    hedge = detect_near_twin_ambiguity({"multi_source": ms}, _SEMANTIC)
    assert hedge is not None
    assert "CS_EXPANSION_NEIGHBOUR" not in hedge["siblings"]


def test_w150_siblings_capped_at_five():
    ms = _ms(
        ("CS_MINORITY_INTEREST_CAPITAL_ATTRIBUTABLE_TO_THIRD_PARTY_INCLUDED_IN_AT1_CAPITAL", 0.27),
        ("CS_MINORITY_INTEREST_CAPITAL_ATTRIBUTABLE_TO_THIRD_PARTY_INCLUDED_IN_TIER_2_CAPITAL", 0.28),
        ("CS_MINORITY_INTEREST_CAPITAL_ATTRIBUTABLE_TO_THIRD_PARTY_INCLUDED_IN_TOTAL_CAPITAL", 0.29),
        ("CS_MINORITY_INTEREST_CAPITAL_ATTRIBUTABLE_TO_THIRD_PARTY_INCLUDED_IN_COMMON_EQUITY_T1_CAPITAL", 0.30),
        ("CS_MINORITY_INTEREST_T1_CAPITAL_SURPLUS_ATTRIBUTABLE_TO_THIRD_PARTY", 0.31),
        ("CS_MINORITY_INTEREST_CAPITAL_EXTRA_SIXTH_SIBLING_CALCULATION", 0.315),
    )
    hedge = detect_near_twin_ambiguity({"multi_source": ms}, _SEMANTIC)
    assert hedge is not None
    assert len(hedge["siblings"]) == 5
