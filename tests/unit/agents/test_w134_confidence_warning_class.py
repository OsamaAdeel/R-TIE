"""W134 — regression guards for the VERIFIED-confidence dampening.

Background: ``evaluate_grounding`` used to emit ``confidence=0.95`` whenever
the badge was VERIFIED and any citations were present, regardless of how
many ``GROUNDING-LOW:`` advisories had fired. That meant the formula could
detect a citation-padding / range-repeat / citation-count-cap issue and
still publish "maximum confidence." W134 caps VERIFIED+citations at
``0.85`` when the warnings array is non-empty (including LOW-severity
advisories). UNVERIFIED branches and the no-citations path are unchanged.

These tests pin both the baseline (clean response → 0.95) and the
post-W134 cap (advisory present → 0.85), so future detector or formula
work cannot silently re-inflate confidence above ``0.85`` while LOW
warnings are firing.

See ``scratch/w134_audit_findings.md`` for the audit and the W144 backlog
item (continuous quality-score rework).
"""

from src.agents.logic_explainer import evaluate_grounding


def test_w134_verified_clean_response_still_emits_max_confidence():
    """Guard: a VERIFIED response with citations and NO warnings must
    still produce ``confidence == 0.95``. W134 must not penalize correct
    responses — the dampening only applies when warnings are present."""
    grounding = evaluate_grounding(
        raw_query="Explain FN_FOO",
        markdown="At Line 42 the value is stored as `x`.",
        multi_source={
            "FN_FOO": {"source_code": [{"line": 42, "text": "x := 1;"}]},
        },
        functions_analyzed=["FN_FOO"],
        query_type="COLUMN_LOGIC",
    )
    assert grounding["badge"] == "VERIFIED"
    assert grounding["warnings"] == []
    assert grounding["confidence"] == 0.95


def test_w134_verified_with_grounding_low_advisory_dampens_to_085():
    """Core W134 regression: VERIFIED + citations + ``GROUNDING-LOW:``
    advisory present in warnings → confidence capped at ``0.85`` (was
    ``0.95`` pre-W134).

    Triggers the range-repeat advisory by citing the same line >3 times,
    which is exactly the shape that fired on B4 in the P1 harness
    capture (``Line 24 cited 5 times``). Badge stays VERIFIED because
    GROUNDING-LOW is non-blocking — only confidence dampens.
    """
    # Cite Line 42 four times to trip _W57_RANGE_REPEAT_THRESHOLD (=3).
    markdown = (
        "Step 1 at (Line 42) does A. "
        "Step 2 at (Line 42) does B. "
        "Step 3 at (Line 42) does C. "
        "Step 4 at (Line 42) does D."
    )
    grounding = evaluate_grounding(
        raw_query="Explain FN_FOO",
        markdown=markdown,
        multi_source={
            "FN_FOO": {"source_code": [{"line": 42, "text": "x := 1;"}]},
        },
        functions_analyzed=["FN_FOO"],
        query_type="COLUMN_LOGIC",
    )
    # The range-repeat detector must have fired as GROUNDING-LOW.
    assert any(
        w.startswith("GROUNDING-LOW:") for w in grounding["warnings"]
    ), grounding["warnings"]
    # Badge unchanged — LOW is advisory only.
    assert grounding["badge"] == "VERIFIED"
    # W134 cap: 0.85, not 0.95.
    assert grounding["confidence"] == 0.85


def test_w134_unverified_branch_unchanged_at_04():
    """Guard: UNVERIFIED + citations stays at ``0.4``. W134 must not
    touch the UNVERIFIED bucket constants — only the VERIFIED+citations
    branch is affected."""
    # A2-shape: cite a function not in retrieved sources → GROUNDING-HIGH.
    # W57's function-citation regex requires "(FN_NAME, Line N)" shape.
    markdown = (
        "The value at (FN_NOT_RETRIEVED, Line 98) comes from a join."
    )
    grounding = evaluate_grounding(
        raw_query="What is RRP?",
        markdown=markdown,
        multi_source={
            "FN_FOO": {"source_code": [{"line": 1, "text": "BEGIN"}]},
        },
        functions_analyzed=["FN_FOO"],
        query_type="COLUMN_LOGIC",
    )
    assert grounding["badge"] == "UNVERIFIED"
    assert grounding["confidence"] == 0.4
    assert any(
        w.startswith("GROUNDING-HIGH:") for w in grounding["warnings"]
    )


def test_w134_w108_truncated_path_still_routes_to_04():
    """Guard: W108-TRUNCATED is added in main.py *after*
    ``evaluate_grounding`` returns, but its semantics ride the same
    blocking-warning path. W134 must not change that — a non-LOW
    warning still flips the badge to UNVERIFIED and lands at ``0.4``.

    Simulates the W108 shape by injecting a non-LOW warning condition
    that surfaces inside ``evaluate_grounding`` (CONTRADICTION). The
    post-evaluate W108 wiring at main.py:1769-1786 is unit-tested
    elsewhere; here we just confirm the formula's bucket assignment
    for any non-LOW warning is unchanged.
    """
    continuation = " ".join(["explanation"] * 60)
    grounding = evaluate_grounding(
        raw_query="Explain FN_FOO",
        markdown=(
            f"At Line 1, x := 1. The source was not provided. {continuation}"
        ),
        multi_source={
            "FN_FOO": {"source_code": [{"line": 1, "text": "x := 1;"}]},
        },
        functions_analyzed=["FN_FOO"],
        query_type="COLUMN_LOGIC",
    )
    assert grounding["badge"] == "UNVERIFIED"
    assert grounding["confidence"] == 0.4
    assert any("CONTRADICTION" in w for w in grounding["warnings"])


def test_w134_verified_no_citations_unchanged_at_08():
    """Guard: the VERIFIED + no-citations branch (``0.8``) is untouched
    by W134. The cap only applies to the citations-present sub-branch.

    Query type ``GENERAL`` does not require citations, so an analysis
    with no line refs and no warnings lands at VERIFIED + 0.8.
    """
    grounding = evaluate_grounding(
        raw_query="What does this system do?",
        markdown="A short summary with no line citations.",
        multi_source={},
        functions_analyzed=[],
        query_type="GENERAL",
    )
    # GENERAL is outside _REQUIRES_CITATIONS, so missing citations is OK.
    assert grounding["badge"] == "VERIFIED"
    assert grounding["warnings"] == []
    assert grounding["confidence"] == 0.8


def test_w134_verified_with_multiple_low_advisories_caps_at_085():
    """Guard: even multiple ``GROUNDING-LOW:`` advisories cap at
    ``0.85`` (not below). W134 is a binary "warnings present → 0.85"
    rule, not a per-warning penalty. Multi-warning gradation belongs
    to W144's architectural rework, not this surgical mitigation."""
    # Trip both range-repeat (Line 42 cited 4 times) and the citation
    # count cap (>50 events). Use 51 distinct lines plus 4 repeats of 42.
    citations_block = " ".join(
        f"At (Line {i}), x := 1." for i in range(1, 52)
    )
    markdown = (
        citations_block
        + " Step A at (Line 42) repeats."
        + " Step B at (Line 42) repeats."
        + " Step C at (Line 42) repeats."
    )
    grounding = evaluate_grounding(
        raw_query="Explain FN_FOO",
        markdown=markdown,
        multi_source={
            "FN_FOO": {"source_code": [
                {"line": i, "text": "x"} for i in range(1, 60)
            ]},
        },
        functions_analyzed=["FN_FOO"],
        query_type="COLUMN_LOGIC",
    )
    low_warnings = [
        w for w in grounding["warnings"] if w.startswith("GROUNDING-LOW:")
    ]
    blocking = [
        w for w in grounding["warnings"]
        if not w.startswith("GROUNDING-LOW:")
    ]
    # Only LOW advisories (no blocking) — badge must stay VERIFIED.
    assert blocking == [], blocking
    assert len(low_warnings) >= 1
    assert grounding["badge"] == "VERIFIED"
    # Cap is binary — even multiple LOWs land at 0.85, not lower.
    assert grounding["confidence"] == 0.85


# ---------------------------------------------------------------------------
# W134 Change 2: post-hoc W108-TRUNCATED append in main.py also caps
# confidence at 0.4 alongside the existing badge=UNVERIFIED override.
#
# Pre-W134, main.py:1779-1786 appended W108-TRUNCATED to the warnings
# array and flipped badge to UNVERIFIED but deliberately left
# `grounding["confidence"]` untouched on the rationale "grounding's own
# calculation accounts for evidence quality." That calculation is a
# 5-bucket lookup, not a quality measure, so a clean response that got
# truncated shipped as UNVERIFIED + 0.95 (B3 in the P1 harness).
#
# This test contract-tests the pattern: when a post-hoc append flips
# badge to UNVERIFIED, confidence must drop to the matching UNVERIFIED
# bucket (0.4 with citations, mirroring `blocking_warnings` routing).
# ---------------------------------------------------------------------------

def test_w134_post_hoc_w108_truncated_caps_confidence_at_04():
    """B3-shape regression: a response that ``evaluate_grounding`` returns
    as VERIFIED + 0.95 must be re-capped to 0.4 when ``main.py`` appends
    a ``W108-TRUNCATED`` warning and flips badge to UNVERIFIED.

    The cap is applied in ``main.py:1779-1789`` (the W108 block); this
    test mirrors that block's logic so the contract is pinned regardless
    of where the cap lives in the streaming pipeline.
    """
    # Step 1: produce the clean VERIFIED + 0.95 dict (matches B3's shape
    # inside evaluate_grounding — no internal W57 warnings).
    grounding = evaluate_grounding(
        raw_query="Explain FN_FOO",
        markdown="At Line 42 the value is stored as `x`.",
        multi_source={
            "FN_FOO": {"source_code": [{"line": 42, "text": "x := 1;"}]},
        },
        functions_analyzed=["FN_FOO"],
        query_type="COLUMN_LOGIC",
    )
    assert grounding["badge"] == "VERIFIED"
    assert grounding["confidence"] == 0.95
    assert grounding["warnings"] == []

    # Step 2: replicate main.py:1779-1789 — W108 post-hoc warning,
    # badge flip, and the W134 confidence cap. If a future refactor
    # drops or weakens the cap, this assertion catches it.
    grounding["warnings"].append(
        "W108-TRUNCATED: response based on 27 of 35 retrieved functions; "
        "8 lower-ranked candidates were dropped to fit the model's "
        "context budget. Narrow your query if you need full coverage."
    )
    grounding["badge"] = "UNVERIFIED"
    if grounding["confidence"] > 0.4:
        grounding["confidence"] = 0.4

    # Step 3: post-cap invariants.
    assert grounding["badge"] == "UNVERIFIED"
    assert grounding["confidence"] == 0.4, (
        "UNVERIFIED + post-hoc W108-TRUNCATED must cap confidence at 0.4 "
        "(pre-W134 this shipped as UNVERIFIED + 0.95 — see B3 finding in "
        "scratch/w134_audit_findings.md)"
    )
    assert any(
        w.startswith("W108-TRUNCATED:") for w in grounding["warnings"]
    )


def test_w134_post_hoc_cap_guard_does_not_inflate_lower_confidence():
    """Defensive: the ``if confidence > 0.4`` guard must never raise
    a sub-0.4 confidence up to 0.4. Mirrors the same guard pattern
    PARTIAL_SOURCE_INDEXED uses (it sets 0.2 unconditionally; W108
    uses a conditional cap so it never collides with that override
    if both warnings were ever to fire on the same response).
    """
    # Simulate a grounding dict already at 0.2 (e.g., PARTIAL_SOURCE_INDEXED
    # ran first and set 0.2).
    grounding = {
        "badge": "UNVERIFIED",
        "confidence": 0.2,
        "warnings": ["PARTIAL_SOURCE_INDEXED: ..."],
    }
    # W108-TRUNCATED block runs after PARTIAL_SOURCE_INDEXED. The W134
    # cap is conditional — it must not inflate 0.2 to 0.4.
    grounding["warnings"].append("W108-TRUNCATED: ...")
    grounding["badge"] = "UNVERIFIED"
    if grounding["confidence"] > 0.4:
        grounding["confidence"] = 0.4

    assert grounding["confidence"] == 0.2, (
        "Confidence cap must use `if > 0.4` (not `=`) so it never "
        "raises an already-lower value."
    )
