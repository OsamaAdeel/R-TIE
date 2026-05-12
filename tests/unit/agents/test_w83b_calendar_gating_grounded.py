"""W83B (W57 Check 7): content-grounded calendar-gating detector.

W83a (`_w57_check_december_paraphrase`) matches a verb-direct regex
set (``is executed only in December``, ``operates only at
year-end``). Run 8 / Run 9 confirmed gpt-4o-mini also emits hedged
framings that share the same fabrication semantics but use a
syntactic family W83a's regex set deliberately excluded:

  - "operates under the condition that the reporting month is December"
  - "contingent on the reporting month being December"
  - "particularly when the reporting month is December"

A2 (CS_Goodwill_Calculation, ESCAPE in both Run 8 and Run 9) is the
canonical case. W83B catches it via a sentence-bounded co-occurrence
rule over three token classes — gating language (A), restrictive
qualifier (B), calendar referent (C) — and validates against the
same source-content gate (:func:`_w57_source_has_december_gate`) W83a
uses.

Asymmetric design (matches W83a): false positives on legitimate
December-gated functions must NOT happen. False negatives on edge
hedging — overgeneralization where source has localized December
logic but body claims whole-function gating (the B3 case) — are
acknowledged scope for v2 (W83C / future ticket).

Anchor resolution: prefers W84's ``state["w70_anchor"]["function"]``
when present (cascade-resolved primary). Falls back to
:func:`_w57_resolve_primary_function`. NO-OPs when neither resolves
a target.
"""

from src.agents.logic_explainer import (
    _W83B_GATING_LANGUAGE,
    _W83B_RESTRICTIVE_QUALIFIER,
    _W83B_CALENDAR_REFERENT,
    _W83B_PROXIMITY_CHARS,
    _w57_check_calendar_gating_grounded,
    _w83b_body_has_hedged_calendar_gating,
    _w83b_sentence_matches,
    _w83b_split_sentences,
    w57_enforce_grounding,
)


def _src_text(text: str):
    """Build a single-line source_code list with arbitrary text."""
    return [{"line": 1, "text": text}]


# A2's failure surface verbatim (Run 8 and Run 9 captures). The
# CS_Goodwill_Calculation function's source has zero month-12
# references — confirmed via grep at 2026-05-12.
_A2_HEDGED_BODY = (
    "The CS_Goodwill_Calculation function is designed to compute and "
    "merge goodwill-related capital adjustments. This function is "
    "executed under specific conditions, particularly when the "
    "reporting month is December, which is crucial for year-end "
    "financial reporting."
)

# Source for CS_Goodwill_Calculation. Real grep against the source
# file returned zero matches for EXTRACT(MONTH / MONTH = 12 / etc.
# This excerpt is shape-faithful: a date filter on a hardcoded
# March-31 calendar date, with no month-12 predicate.
_CS_GOODWILL_SRC_NO_DECEMBER = (
    "  WHERE D_CALENDAR_DATE = TO_DATE('20260331','YYYYMMDD') "
    "AND DIM_RUN.n_run_skey = '870'"
)

# Genuine December-gated source: matches the
# `_w57_source_has_december_gate` regex set. Used as the negative
# control to confirm the source-content gate suppresses W83B.
_GENUINE_DECEMBER_SRC = (
    "  IF TO_NUMBER (EXTRACT (MONTH FROM TO_DATE (CQD, 'DD-MON-RR'))) = 12 "
    "THEN ..."
)


# ===========================================================================
# Co-occurrence rule helpers (white-box on the sentence matcher)
# ===========================================================================

def test_sentence_matches_canonical_a2_hedged_framing():
    """The verbatim A2 sentence shape. The matcher must fire."""
    sentence = (
        "this function is executed under specific conditions, "
        "particularly when the reporting month is december, which "
        "is crucial for year-end financial reporting"
    )
    assert _w83b_sentence_matches(sentence)


def test_sentence_matches_contingent_on_december():
    """Canary C from W83a's validation — the hedged framing W83a
    deliberately excluded. W83B must catch it."""
    sentence = (
        "the function is contingent on the reporting month being december"
    )
    assert _w83b_sentence_matches(sentence)


def test_sentence_matches_operates_under_the_condition():
    sentence = (
        "the function operates under the condition that the reporting "
        "month is december"
    )
    assert _w83b_sentence_matches(sentence)


def test_sentence_matches_fires_only_at_year_end():
    sentence = "the function is fired only at year-end"
    assert _w83b_sentence_matches(sentence)


def test_sentence_matches_q4_hedged():
    sentence = "the function is restricted to the fourth quarter reporting cycle"
    assert _w83b_sentence_matches(sentence)


def test_sentence_does_not_match_descriptive_december_mention():
    """No B-class qualifier → no fire. December alone is descriptive
    (the function may legitimately mention December in its narrative
    without claiming gating)."""
    sentence = (
        "the function processes records used in december's reporting"
    )
    assert not _w83b_sentence_matches(sentence)


def test_sentence_does_not_match_year_end_descriptive():
    sentence = "the year-end column is one of the inputs"
    assert not _w83b_sentence_matches(sentence)


def test_sentence_does_not_match_december_alone_no_b():
    """No B-class qualifier at all → no fire even with A and C."""
    sentence = "the function executes a december query"
    assert not _w83b_sentence_matches(sentence)


def test_sentence_does_not_match_b_without_c():
    """No calendar referent → no fire even with A and B."""
    sentence = "the function executes only in march"
    assert not _w83b_sentence_matches(sentence)


def test_split_sentences_strips_sql_fences():
    """SQL fence content is stripped before sentence-splitting so
    DIM_DATES.D_CALENDAR_DATE inside ```sql ... ``` does not shred
    the natural-language sentence boundary."""
    body = (
        "this is sentence one.\n"
        "```sql\nWHERE DIM_DATES.D_CALENDAR_DATE = ... AND x = 1.\n```\n"
        "this is sentence two."
    )
    sentences = _w83b_split_sentences(body)
    assert "this is sentence one" in sentences[0]
    # The SQL block should not have fragmented the body into many
    # tiny "sentences" — expect 2 sentences plus whatever the strip
    # leaves.
    assert len(sentences) <= 3


def test_a_b_c_split_across_two_sentences_no_match():
    """The prompt's edge case: A and B in sentence 1, C in sentence 2.
    Sentence-bounded rule must NOT fire."""
    body = "the function fires only on specific months. mainly december"
    assert not _w83b_body_has_hedged_calendar_gating(body)


# ===========================================================================
# Full check (`_w57_check_calendar_gating_grounded`) — positive cases
# ===========================================================================

def test_check_fires_on_a2_hedged_body_no_source_gate():
    """The canonical W83B target. A2 body shape + source with no
    month-12 logic → warning."""
    multi_source = {
        "CS_GOODWILL_CALCULATION": {
            "source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)
        }
    }
    warnings = _w57_check_calendar_gating_grounded(
        markdown=_A2_HEDGED_BODY,
        multi_source=multi_source,
        asked_about_function="CS_GOODWILL_CALCULATION",
    )
    assert len(warnings) == 1
    assert "GROUNDING-CALENDAR-HIGH" in warnings[0]
    assert "CS_GOODWILL_CALCULATION" in warnings[0]


def test_check_fires_on_contingent_on_december_no_source_gate():
    body = (
        "## FN_X\nThe function is contingent on the reporting month "
        "being December, which is required by the regulatory framework."
    )
    multi_source = {"FN_X": {"source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1


def test_check_fires_on_softener_particularly_when_december():
    body = (
        "FN_X performs goodwill calculation, particularly when the "
        "reporting month is December for year-end processing."
    )
    multi_source = {"FN_X": {"source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1


def test_check_fires_on_year_end_hedged():
    body = (
        "FN_X operates exclusively during year-end processing windows."
    )
    multi_source = {"FN_X": {"source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1


def test_check_fires_on_q4_restricted():
    body = (
        "FN_X is restricted to the fourth quarter reporting cycle as "
        "part of regulatory requirements."
    )
    multi_source = {"FN_X": {"source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1


# ===========================================================================
# Source-content gate suppresses false positives
# ===========================================================================

def test_check_no_op_when_source_has_genuine_december_gate():
    """If the source actually has EXTRACT(MONTH ...) = 12, the
    December claim is grounded and W83B must NOT fire."""
    multi_source = {
        "FN_X": {"source_code": _src_text(_GENUINE_DECEMBER_SRC)}
    }
    warnings = _w57_check_calendar_gating_grounded(
        markdown=_A2_HEDGED_BODY.replace(
            "CS_Goodwill_Calculation", "FN_X"
        ),
        multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


def test_check_no_op_when_source_has_year_end_calendar_literal():
    """TO_DATE('YYYY1231', ...) counts as a year-end gate. W83B suppresses."""
    src = "  WHERE D_CALENDAR_DATE = TO_DATE('20251231','yyyymmdd')"
    body = (
        "FN_X is fired only at year-end, as the reporting cycle requires."
    )
    multi_source = {"FN_X": {"source_code": _src_text(src)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


# ===========================================================================
# Negative cases (no warning)
# ===========================================================================

def test_check_no_op_on_descriptive_december_mention():
    body = (
        "FN_X processes records used in December's reporting. The output "
        "is written to FCT_STANDARD_ACCT_HEAD."
    )
    multi_source = {"FN_X": {"source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


def test_check_no_op_on_year_end_descriptive_only():
    body = "The year-end column is one of the inputs to FN_X."
    multi_source = {"FN_X": {"source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


def test_check_no_op_on_empty_multi_source():
    warnings = _w57_check_calendar_gating_grounded(
        markdown=_A2_HEDGED_BODY,
        multi_source={},
        asked_about_function="CS_GOODWILL_CALCULATION",
    )
    assert warnings == []


def test_check_no_op_when_no_anchor_resolved():
    """No asked_about_function AND no w70_anchor AND multi_source has
    multiple keys → `_w57_resolve_primary_function` returns None →
    check skips."""
    multi_source = {
        "FN_A": {"source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)},
        "FN_B": {"source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)},
    }
    body = "Some explanation without any function name cited."
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function=None,
        w70_anchor=None,
    )
    # Resolver returns None because: no asked fn, neither FN_A nor
    # FN_B is cited in body. Skip.
    assert warnings == []


# ===========================================================================
# Dedup vs Check 5 and W83a
# ===========================================================================

def test_check_no_op_when_check5_literal_phrase_present():
    """Check 5's literal phrase `only runs in december` triggers W83a's
    skip too. W83B inherits the same dedup."""
    body = (
        "FN_X only runs in December for year-end. Additionally, it "
        "operates under the condition that the reporting month is "
        "December."
    )
    multi_source = {"FN_X": {"source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


def test_check_no_op_when_w83a_paraphrase_present():
    """If a W83a verb-direct paraphrase already matched, W83B defers
    to its (more specific) warning."""
    body = (
        "FN_X is executed only when the reporting month is December "
        "for year-end. It also operates under the condition that "
        "the reporting month is December."
    )
    multi_source = {"FN_X": {"source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


def test_enforce_grounding_emits_only_one_calendar_warning_per_response():
    """Multi-sentence hedging in one body → at most one
    GROUNDING-CALENDAR-HIGH warning after enforce-level dedup."""
    body = (
        "FN_X operates under the condition that the reporting month "
        "is December. Specifically when the reporting month is "
        "December, FN_X writes to FCT_STANDARD_ACCT_HEAD. The "
        "function is contingent on the reporting month being "
        "December for compliance reasons."
    )
    multi_source = {"FN_X": {"source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)}}
    warnings = w57_enforce_grounding(
        raw_query="How does FN_X work?",
        markdown=body,
        multi_source=multi_source,
        functions_analyzed=["FN_X"],
    )
    cal_warnings = [w for w in warnings if "CALENDAR" in w]
    assert len(cal_warnings) == 1


# ===========================================================================
# Anchor resolution (W84 w70_anchor preferred over W76 fallback)
# ===========================================================================

def test_w70_anchor_overrides_asked_about_function():
    """When w70_anchor names a function in multi_source, the check
    consults THAT function's source — even if asked_about_function
    points elsewhere."""
    # FN_A has a real December gate; FN_B does not. If the check
    # consulted asked_about_function (FN_B), it would fire. With
    # w70_anchor naming FN_A, the gate suppresses.
    multi_source = {
        "FN_A": {"source_code": _src_text(_GENUINE_DECEMBER_SRC)},
        "FN_B": {"source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)},
    }
    body = (
        "FN_A operates under the condition that the reporting month "
        "is December. It writes the result to FCT_STANDARD_ACCT_HEAD."
    )
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_B",
        w70_anchor={"function": "FN_A", "source": "object_name", "confidence": "high"},
    )
    assert warnings == []


def test_w70_anchor_with_non_matching_function_falls_back():
    """w70_anchor names a function NOT in multi_source → falls back to
    asked_about_function / _w57_resolve_primary_function."""
    multi_source = {
        "FN_B": {"source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)},
    }
    body = (
        "FN_B operates under the condition that the reporting month "
        "is December."
    )
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_B",
        w70_anchor={"function": "NOT_IN_MULTI_SOURCE", "source": "?", "confidence": "low"},
    )
    assert len(warnings) == 1
    assert "FN_B" in warnings[0]


def test_w70_anchor_none_falls_back_to_asked_about():
    """w70_anchor None → uses asked_about_function path. A2 reproduction."""
    multi_source = {
        "CS_GOODWILL_CALCULATION": {
            "source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)
        }
    }
    warnings = _w57_check_calendar_gating_grounded(
        markdown=_A2_HEDGED_BODY,
        multi_source=multi_source,
        asked_about_function="CS_GOODWILL_CALCULATION",
        w70_anchor=None,
    )
    assert len(warnings) == 1


def test_w70_anchor_empty_dict_falls_back():
    """w70_anchor={} (empty) → same as None path."""
    multi_source = {
        "CS_GOODWILL_CALCULATION": {
            "source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)
        }
    }
    warnings = _w57_check_calendar_gating_grounded(
        markdown=_A2_HEDGED_BODY,
        multi_source=multi_source,
        asked_about_function="CS_GOODWILL_CALCULATION",
        w70_anchor={},
    )
    assert len(warnings) == 1


# ===========================================================================
# Integration via w57_enforce_grounding (end-to-end check ordering)
# ===========================================================================

def test_enforce_grounding_fires_w83b_on_a2_canonical():
    """End-to-end: A2 body shape + matching multi_source → W83B fires
    via the enforcement loop."""
    multi_source = {
        "CS_GOODWILL_CALCULATION": {
            "source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)
        }
    }
    warnings = w57_enforce_grounding(
        raw_query="How does CS_Goodwill_Calculation work?",
        markdown=_A2_HEDGED_BODY,
        multi_source=multi_source,
        functions_analyzed=["CS_GOODWILL_CALCULATION"],
    )
    cal_warnings = [w for w in warnings if "GROUNDING-CALENDAR-HIGH" in w]
    assert len(cal_warnings) == 1


def test_enforce_grounding_w83a_takes_precedence_over_w83b():
    """A body that matches both W83a (verb-direct) AND W83B
    (co-occurrence) → only W83a's warning, not both."""
    body = (
        "FN_X is executed only when the reporting month is December. "
        "FN_X also operates under the condition that the reporting "
        "month is December."
    )
    multi_source = {"FN_X": {"source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)}}
    warnings = w57_enforce_grounding(
        raw_query="How does FN_X work?",
        markdown=body,
        multi_source=multi_source,
        functions_analyzed=["FN_X"],
    )
    # W83a's message is "executes only in December / at year-end
    # (paraphrase form)"; W83B's is "gated on December (hedged form)".
    # Both fire would mean two GROUNDING-HIGH messages on the same
    # claim. Dedup must keep one.
    w83a_msgs = [
        w for w in warnings
        if "executes only" in w and "paraphrase form" in w
    ]
    w83b_msgs = [
        w for w in warnings if "GROUNDING-CALENDAR-HIGH" in w
    ]
    assert len(w83a_msgs) == 1
    assert len(w83b_msgs) == 0


def test_enforce_grounding_w70_anchor_threads_through():
    """The w70_anchor kwarg on w57_enforce_grounding reaches the W83B
    check (verified by anchor-override behavior end-to-end)."""
    multi_source = {
        "FN_A": {"source_code": _src_text(_GENUINE_DECEMBER_SRC)},
        "FN_B": {"source_code": _src_text(_CS_GOODWILL_SRC_NO_DECEMBER)},
    }
    body = (
        "FN_A operates under the condition that the reporting month "
        "is December."
    )
    warnings = w57_enforce_grounding(
        raw_query="How does FN_A work?",
        markdown=body,
        multi_source=multi_source,
        functions_analyzed=["FN_A"],
        w70_anchor={"function": "FN_A", "source": "object_name", "confidence": "high"},
    )
    # FN_A's source has December gate → no W83B fire.
    cal = [w for w in warnings if "GROUNDING-CALENDAR-HIGH" in w]
    assert cal == []


# ===========================================================================
# Proximity rule edge cases
# ===========================================================================

def test_b_and_c_too_far_apart_no_match():
    """B and C in the same sentence but >80 chars apart → no fire."""
    sentence = (
        "the function operates only "
        + ("x " * 50)  # 100 chars of filler
        + " and reads december-related data"
    )
    assert not _w83b_sentence_matches(sentence)


def test_a_present_b_close_but_c_too_far_no_match():
    """A and B within 80 chars of each other, but C is >80 chars from
    A. Per Rule 1 the C-to-A distance constraint must fail. Per Rule
    2 (B+C) the B-to-C distance also fails. No fire."""
    sentence = (
        "the function executes only "
        + ("x " * 50)
        + " and processes december reports"
    )
    assert not _w83b_sentence_matches(sentence)


def test_inferred_a_via_b_c_relaxation():
    """Sentence has B and C within window but no explicit A from the
    class list. Rule 2 still fires (A inferred)."""
    sentence = "particularly when the reporting month is december"
    # "executes"/"runs"/etc. NOT present in this sentence.
    assert _w83b_sentence_matches(sentence)


# ===========================================================================
# Constants sanity (lock in token classes against accidental edits)
# ===========================================================================

def test_proximity_window_is_80_chars():
    """The proximity-window value is part of the public contract for
    this check. Future regressions on tightening / loosening it should
    surface in this test."""
    assert _W83B_PROXIMITY_CHARS == 80


def test_calendar_referents_include_december_and_year_end():
    assert "december" in _W83B_CALENDAR_REFERENT
    assert "year-end" in _W83B_CALENDAR_REFERENT
    assert "q4" in _W83B_CALENDAR_REFERENT


def test_restrictive_qualifiers_include_hedged_forms():
    """The whole point of W83B is to catch hedged forms. These three
    are the canonical Run-9 evidence."""
    assert "under the condition that" in _W83B_RESTRICTIVE_QUALIFIER
    assert "contingent on" in _W83B_RESTRICTIVE_QUALIFIER
    assert "particularly when" in _W83B_RESTRICTIVE_QUALIFIER


def test_gating_language_includes_verb_variants():
    assert "executes" in _W83B_GATING_LANGUAGE
    assert "runs" in _W83B_GATING_LANGUAGE
    assert "fires" in _W83B_GATING_LANGUAGE
    assert "operates" in _W83B_GATING_LANGUAGE
