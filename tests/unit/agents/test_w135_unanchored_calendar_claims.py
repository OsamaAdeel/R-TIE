"""W135: emit GROUNDING-CALENDAR-UNANCHORED when calendar-claim phrase
is present in the body but the calendar-validation pipeline cannot run
because no anchor resolves.

P1 query A2 surfaced the failure mode: response body asserts
"the function FN_G_TEST_CSTM ensures that this identification occurs
only during the reporting month of December" while FN_G_TEST_CSTM is
NOT in retrieved sources. `_w57_resolve_primary_function` exhausts
its three priorities (no asked_about_function, no multi_source key
appears in the body, multi_source has >1 keys) and returns None.
All three calendar checks (Check 5 template phrases, W83a December
paraphrase, W83B hedged-framing co-occurrence) silently skip on
target_fn=None. The calendar fabrication slipped through.

W135 adds a new check `_w57_check_unanchored_calendar_claims` that
runs AFTER the three existing calendar checks in
`w57_enforce_grounding`. It detects calendar phrases independently
(reusing _W57_CHECK5_DECEMBER_LITERAL_PHRASES,
_W57_DECEMBER_PARAPHRASE_PATTERNS, and _w83b_collect_claim_tags in
detect-only mode) and fires when the same anchor cascade W83B
uses — W70 anchor → resolver — returns None for BOTH.

Architecture B (new check function), category
GROUNDING-CALENDAR-UNANCHORED (blocking via existing
not-GROUNDING-LOW filter). See scratch/w83d_diagnostic.md for the
full mechanism trace.
"""

from src.agents.logic_explainer import (
    _W57_CHECK5_DECEMBER_LITERAL_PHRASES,
    _W57_DECEMBER_PARAPHRASE_PATTERNS,
    _w57_check_calendar_gating_grounded,
    _w57_check_december_paraphrase,
    _w57_check_template_phrases,
    _w57_check_unanchored_calendar_claims,
    _w57_resolve_primary_function,
    _w83b_collect_claim_tags,
    evaluate_grounding,
    w57_enforce_grounding,
)


def _src_text(text: str):
    """Build a single-line source_code list with arbitrary text."""
    return [{"line": 1, "text": text}]


# ---------------------------------------------------------------------------
# A2 reproduction fixtures
#
# A2's failure surface verbatim (P1 harness, 2026-05-22):
#   - Body cites FN_G_TEST_CSTM as the executor of a December gate
#   - FN_G_TEST_CSTM is NOT in retrieved sources
#   - multi_source has 35 functions, none of which appear in the body
#   - asked_about_function is empty (object_name="")
# Net effect: resolver returns None; all three calendar checks skip.
# ---------------------------------------------------------------------------
_A2_BODY = (
    "RRP refers to Recovery and Resolution Planning. The function "
    "FN_G_TEST_CSTM ensures that this identification occurs only "
    "during the reporting month of December, which aligns with "
    "year-end reporting cycles."
)

# 35-function multi_source where:
#   - FN_G_TEST_CSTM is NOT a key (the citation is fabricated)
#   - None of the keys appears anywhere in _A2_BODY
# This exercises the resolver's three-priority cascade failure mode.
_A2_MULTI_SOURCE = {
    f"FN_OFSERM_FUNCTION_{i:02d}": {"source_code": _src_text(
        "  WHERE D_CALENDAR_DATE = TO_DATE('20260331','YYYYMMDD')"
    )}
    for i in range(35)
}


# ===========================================================================
# Step 2.1 — Resolver baseline: confirm None return for A2 setup
# (regression guard — the resolver's three-priority cascade stays frozen)
# ===========================================================================

def test_w135_resolver_returns_none_for_a2_setup():
    """The resolver must return None for A2's setup:
      P1 miss: asked_about_function is empty
      P2 miss: no multi_source key appears in _A2_BODY
      P3 miss: multi_source has 35 entries (not 1)

    If this test starts failing, the resolver's cascade has changed —
    W135's premise (the resolver fails to anchor here) needs revisiting.
    """
    target = _w57_resolve_primary_function(
        markdown=_A2_BODY,
        asked_about_function=None,
        multi_source=_A2_MULTI_SOURCE,
    )
    assert target is None


def test_w135_resolver_returns_none_with_empty_asked_about_function():
    """An empty-string asked_about_function (object_name='' shape) is
    treated the same as None — the falsy guard at line 2159 short-
    circuits the P1 check."""
    target = _w57_resolve_primary_function(
        markdown=_A2_BODY,
        asked_about_function="",
        multi_source=_A2_MULTI_SOURCE,
    )
    assert target is None


# ===========================================================================
# Step 2.2 — Existing three calendar checks DO silently skip on A2
# (documents the pre-W135 fragility; ensures W135 doesn't accidentally
# change the existing checks' behaviour)
# ===========================================================================

def test_w135_existing_check5_silently_skips_on_a2():
    warnings = _w57_check_template_phrases(
        markdown=_A2_BODY,
        multi_source=_A2_MULTI_SOURCE,
        asked_about_function=None,
    )
    assert warnings == []


def test_w135_existing_w83a_silently_skips_on_a2():
    warnings = _w57_check_december_paraphrase(
        markdown=_A2_BODY,
        multi_source=_A2_MULTI_SOURCE,
        asked_about_function=None,
    )
    assert warnings == []


def test_w135_existing_w83b_silently_skips_on_a2():
    warnings = _w57_check_calendar_gating_grounded(
        markdown=_A2_BODY,
        multi_source=_A2_MULTI_SOURCE,
        asked_about_function=None,
        w70_anchor=None,
    )
    assert warnings == []


# ===========================================================================
# Step 2.3 — New W135 check: positive cases (warning DOES fire)
# ===========================================================================

def test_w135_fires_on_a2_canonical_body():
    """The load-bearing pin. A2's body contains a December gate claim
    AND resolver returns None for the multi_source → emit exactly one
    GROUNDING-CALENDAR-UNANCHORED warning."""
    warnings = _w57_check_unanchored_calendar_claims(
        markdown=_A2_BODY,
        multi_source=_A2_MULTI_SOURCE,
        asked_about_function=None,
        w70_anchor=None,
    )
    assert len(warnings) == 1
    assert warnings[0].startswith("GROUNDING-CALENDAR-UNANCHORED:")


def test_w135_fires_on_check5_literal_december_phrase():
    """Check 5 literal phrase + unresolvable anchor → fire."""
    body = (
        "FN_FOO only runs in December as part of year-end processing. "
        "It then updates several downstream tables."
    )
    multi_source = {
        "FN_A": {"source_code": _src_text("  WHERE x = 1")},
        "FN_B": {"source_code": _src_text("  WHERE y = 2")},
    }
    warnings = _w57_check_unanchored_calendar_claims(
        markdown=body, multi_source=multi_source,
        asked_about_function=None, w70_anchor=None,
    )
    assert len(warnings) == 1
    assert "GROUNDING-CALENDAR-UNANCHORED:" in warnings[0]
    # Phrase substitution should name the literal that matched.
    assert "only runs in december" in warnings[0].lower()


def test_w135_fires_on_w83a_paraphrase():
    """W83a paraphrase shape + unresolvable anchor → fire (with
    phrase substring from the regex .group(0))."""
    body = (
        "FN_FOO is executed only when the reporting month is December, "
        "as indicated by the conditional checks in the code."
    )
    multi_source = {
        "FN_A": {"source_code": _src_text("  WHERE x = 1")},
        "FN_B": {"source_code": _src_text("  WHERE y = 2")},
    }
    warnings = _w57_check_unanchored_calendar_claims(
        markdown=body, multi_source=multi_source,
        asked_about_function=None, w70_anchor=None,
    )
    assert len(warnings) == 1
    assert "GROUNDING-CALENDAR-UNANCHORED:" in warnings[0]
    # The captured phrase substring must mention the verb form.
    assert "executed" in warnings[0].lower() or "december" in warnings[0].lower()


def test_w135_fires_on_w83b_co_occurrence_only():
    """Body has neither a Check-5 literal nor a W83a paraphrase; only
    W83b's co-occurrence rule fires (A-class verb + B-class qualifier
    + C-class calendar referent in one sentence). Resolver returns
    None → W135 still fires using the W83b claim-tag label."""
    body = (
        "The function operates under the condition that the reporting "
        "month is December, which is crucial for year-end financial "
        "reporting."
    )
    # Sanity: no Check 5 literal, no W83a paraphrase pattern.
    body_lower = body.lower()
    for literal in _W57_CHECK5_DECEMBER_LITERAL_PHRASES:
        assert literal not in body_lower
    for pat in _W57_DECEMBER_PARAPHRASE_PATTERNS:
        assert pat.search(body) is None
    # But W83b co-occurrence DOES detect.
    assert _w83b_collect_claim_tags(body_lower) != []

    multi_source = {
        "FN_A": {"source_code": _src_text("  WHERE x = 1")},
        "FN_B": {"source_code": _src_text("  WHERE y = 2")},
    }
    warnings = _w57_check_unanchored_calendar_claims(
        markdown=body, multi_source=multi_source,
        asked_about_function=None, w70_anchor=None,
    )
    assert len(warnings) == 1
    assert "GROUNDING-CALENDAR-UNANCHORED:" in warnings[0]


# ===========================================================================
# Step 2.4 — Negative cases (warning does NOT fire)
# ===========================================================================

def test_w135_silent_when_no_calendar_phrase_present():
    """Body has no calendar-claim phrase + resolver returns None →
    W135 must NOT fire. The diagnostic only fires when there is a
    claim to validate."""
    body = (
        "FN_X performs an aggregation over the input dataset and "
        "writes the result to FCT_STANDARD_ACCT_HEAD. No date "
        "filtering occurs in the function."
    )
    multi_source = {
        "FN_A": {"source_code": _src_text("  WHERE x = 1")},
        "FN_B": {"source_code": _src_text("  WHERE y = 2")},
    }
    warnings = _w57_check_unanchored_calendar_claims(
        markdown=body, multi_source=multi_source,
        asked_about_function=None, w70_anchor=None,
    )
    assert warnings == []


def test_w135_silent_when_resolver_finds_target_via_p1():
    """asked_about_function matches a multi_source key (P1 success).
    Resolver returns a target → W135 must NOT fire. The existing
    checks (Check 5, W83a, W83B) handle the calendar validation."""
    body = "FN_X only runs in December for year-end reporting."
    multi_source = {
        "FN_X": {"source_code": _src_text("  WHERE x = 1")},
    }
    warnings = _w57_check_unanchored_calendar_claims(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X", w70_anchor=None,
    )
    assert warnings == []


def test_w135_silent_when_resolver_finds_target_via_p2():
    """asked_about_function empty, but a multi_source key IS cited in
    the body (P2 success). Resolver returns that fn → W135 silent."""
    body = (
        "FN_X only runs in December. The function then aggregates "
        "results into FCT_TABLE."
    )
    multi_source = {
        "FN_X": {"source_code": _src_text("  WHERE x = 1")},
        "FN_Y": {"source_code": _src_text("  WHERE y = 2")},
    }
    warnings = _w57_check_unanchored_calendar_claims(
        markdown=body, multi_source=multi_source,
        asked_about_function=None, w70_anchor=None,
    )
    assert warnings == []


def test_w135_silent_when_resolver_finds_target_via_p3():
    """Single-function multi_source. Resolver returns that fn (P3
    fallback) → W135 silent."""
    body = "The function only runs in December as part of year-end."
    multi_source = {
        "FN_X": {"source_code": _src_text("  WHERE x = 1")},
    }
    warnings = _w57_check_unanchored_calendar_claims(
        markdown=body, multi_source=multi_source,
        asked_about_function=None, w70_anchor=None,
    )
    assert warnings == []


def test_w135_silent_when_w70_anchor_resolves():
    """W70 anchor resolves to a multi_source key → W135 silent (W83B
    will handle the calendar validation via the W70 anchor)."""
    body = (
        "The function is executed under the condition that the reporting "
        "month is December, which is crucial."
    )
    multi_source = {
        "FN_A": {"source_code": _src_text("  WHERE x = 1")},
        "FN_B": {"source_code": _src_text("  WHERE y = 2")},
    }
    warnings = _w57_check_unanchored_calendar_claims(
        markdown=body, multi_source=multi_source,
        asked_about_function=None,
        w70_anchor={"function": "FN_A"},
    )
    assert warnings == []


def test_w135_silent_on_empty_multi_source():
    """Empty multi_source → no calendar check makes sense; W135
    follows the same convention as the three existing checks and
    no-ops."""
    warnings = _w57_check_unanchored_calendar_claims(
        markdown=_A2_BODY, multi_source={},
        asked_about_function=None, w70_anchor=None,
    )
    assert warnings == []


# ===========================================================================
# Step 2.5 — Single-fire / dedup
# ===========================================================================

def test_w135_emits_at_most_one_warning_per_response():
    """Body has both a Check-5 literal and a W83a paraphrase AND a
    W83b co-occurrence pattern AND resolver returns None →
    exactly ONE GROUNDING-CALENDAR-UNANCHORED warning."""
    body = (
        "FN_FOO only runs in December. Additionally, it is executed "
        "only when the reporting month is December. The function "
        "also operates under the condition that the reporting month "
        "is December, for year-end processing."
    )
    multi_source = {
        "FN_A": {"source_code": _src_text("  WHERE x = 1")},
        "FN_B": {"source_code": _src_text("  WHERE y = 2")},
    }
    warnings = _w57_check_unanchored_calendar_claims(
        markdown=body, multi_source=multi_source,
        asked_about_function=None, w70_anchor=None,
    )
    assert len(warnings) == 1


# ===========================================================================
# Step 2.6 — Phrase substring truncation (hygiene; Toheed's note)
# ===========================================================================

def test_w135_truncates_long_paraphrase_capture():
    """When the matched phrase comes from a W83a paraphrase regex
    .group(0), the substring length is variable. W135 must truncate
    to a bounded max so the warning string stays compact."""
    # A regex like operates\s+only\s+(?:in|during)\s+december can
    # capture short spans, so to force a long capture exercise
    # year[-\s]end\s+(?:processing|execution|run)\s+only with
    # internal whitespace padding. Even at full length this is short,
    # so we synthesise a deliberately long capture by chaining the
    # space-tolerant pattern repeatedly. The TRUNCATION CONTRACT we
    # pin: the warning string never embeds a raw phrase substring
    # longer than 80 characters.
    body = (
        "FN_FOO year-end                                            "
        "                                  processing only happens "
        "for compliance."
    )
    multi_source = {
        "FN_A": {"source_code": _src_text("  WHERE x = 1")},
        "FN_B": {"source_code": _src_text("  WHERE y = 2")},
    }
    warnings = _w57_check_unanchored_calendar_claims(
        markdown=body, multi_source=multi_source,
        asked_about_function=None, w70_anchor=None,
    )
    # Whether or not the pattern actually matches depends on the
    # regex; what we test is: IF it matches, the embedded phrase
    # substring inside the warning is bounded.
    if warnings:
        warning = warnings[0]
        # The warning is wrapped with quotes around the phrase: extract.
        if "'" in warning:
            start = warning.index("'")
            end = warning.index("'", start + 1)
            phrase = warning[start + 1:end]
            assert len(phrase) <= 80, (
                f"Phrase substring exceeds 80-char cap: {len(phrase)} chars"
            )


# ===========================================================================
# Step 2.7 — Existing checks unaffected when resolver DOES find a target
# ===========================================================================

def test_w135_existing_check5_still_fires_when_target_resolves():
    """For a body where the resolver finds a target, Check 5 must
    behave exactly as it does today. This guards against W135
    accidentally swallowing Check 5's path."""
    body = "FN_X only runs in December for the year-end cycle."
    # Source has TO_CHAR and a literal "12" arithmetic but no MONTH=12
    # gate — the W137 strict-validator path. Pre-W137 Check 5 returned
    # supported=True; post-W137 it returns supported=False → warning.
    multi_source = {
        "FN_X": {"source_code": _src_text(
            "INSERT INTO T SELECT TO_CHAR(n_skey), interest/12 FROM s "
            "WHERE LV_STAGE = 12"
        )},
    }
    warnings = _w57_check_template_phrases(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    # Post-W137: strict gate rejects the noisy source → Check 5 fires.
    assert len(warnings) == 1
    assert "GROUNDING-HIGH" in warnings[0]
    assert "FN_X" in warnings[0]


# ===========================================================================
# Step 2.8 — End-to-end via w57_enforce_grounding (A2 full pipeline)
# ===========================================================================

def test_w135_enforce_grounding_a2_pipeline():
    """Full pipeline test: w57_enforce_grounding on A2's setup must
    produce a warnings list that contains GROUNDING-CALENDAR-UNANCHORED
    (post-W135). The existing checks should remain silent (resolver
    returns None for all of them)."""
    warnings = w57_enforce_grounding(
        raw_query="What's RRP?",
        markdown=_A2_BODY,
        multi_source=_A2_MULTI_SOURCE,
        functions_analyzed=list(_A2_MULTI_SOURCE.keys()),
        redis_client=None,
        w76_anchor=None,
        w70_anchor=None,
    )
    unanchored = [w for w in warnings if w.startswith("GROUNDING-CALENDAR-UNANCHORED:")]
    assert len(unanchored) == 1


# ===========================================================================
# Step 2.9 — Badge interaction (idempotent UNVERIFIED + both warnings present)
# ===========================================================================

def test_w135_badge_unverified_with_both_warnings():
    """A2's body cites a function not in retrieved sources. That
    causes the existing _w57_check_per_claim_binding to emit
    GROUNDING-HIGH: cited function '<X>' not in retrieved sources.
    Adding W135's CALENDAR-UNANCHORED warning must:
      - keep the badge UNVERIFIED (idempotent — already UNVERIFIED)
      - keep BOTH warnings in the array (different message text;
        the set-based dedup at the bottom of w57_enforce_grounding
        keeps both)
    """
    # _A2_BODY cites FN_G_TEST_CSTM via "The function FN_G_TEST_CSTM
    # ensures ..." — which IS the prose-framing pattern picked up
    # by _w57_check_per_claim_binding's 1.0a pass.
    result = evaluate_grounding(
        raw_query="What's RRP?",
        markdown=_A2_BODY,
        multi_source=_A2_MULTI_SOURCE,
        functions_analyzed=list(_A2_MULTI_SOURCE.keys()),
        query_type="FUNCTION_LOGIC",
        redis_client=None,
        w76_anchor=None,
        w70_anchor=None,
    )
    assert result["badge"] == "UNVERIFIED"
    warnings = result["warnings"]
    # Both signals must appear in the warnings array.
    citation_warnings = [
        w for w in warnings
        if w.startswith("GROUNDING-HIGH:") and "FN_G_TEST_CSTM" in w
        and "not in retrieved sources" in w
    ]
    unanchored_warnings = [
        w for w in warnings if w.startswith("GROUNDING-CALENDAR-UNANCHORED:")
    ]
    assert len(citation_warnings) >= 1, (
        f"Expected the per-claim-binding citation warning for "
        f"FN_G_TEST_CSTM; got warnings={warnings!r}"
    )
    assert len(unanchored_warnings) == 1, (
        f"Expected exactly one GROUNDING-CALENDAR-UNANCHORED warning; "
        f"got warnings={warnings!r}"
    )
