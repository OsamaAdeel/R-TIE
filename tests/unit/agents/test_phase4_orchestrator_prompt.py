"""Phase 4 — orchestrator classification prompt scope correction.

Pre-Phase-4 the classifier prompt at orchestrator.CLASSIFICATION_SYSTEM_PROMPT
told the LLM to mark every query referencing an ``FCT_*`` table as
``UNSUPPORTED``. That was a defensible backstop while routing was
OFSMDM-only — FCT tables lived only in OFSERM and the agent couldn't
reach them, so refusing prevented confidently wrong answers. After the
Phase 4 schema pivot, simple aggregates against FCT tables in any
discovered schema are answerable; only true reconciliation queries
(comparing values across STG and FCT) remain out of scope.

The classifier is an LLM, so we can't deterministically unit-test its
output. What we CAN pin is the prompt text itself — the bullets and
examples that teach the LLM where to draw the line. These tests are
regression guards: if a future edit accidentally restores the broad
"References to FCT_*" trigger, the suite fails before merge rather
than waiting for canary (e) to break in production.
"""

from __future__ import annotations

import re

from src.agents.orchestrator import CLASSIFICATION_SYSTEM_PROMPT


def test_prompt_does_not_carry_old_blanket_fct_trigger():
    """The pre-Phase-4 wording — any FCT_* reference -> UNSUPPORTED —
    must NOT be present. The exact bullet was:
    'References to FCT_* tables or downstream result tables not present
    in the graph (cross-table reconciliation).'"""
    forbidden = "References to FCT_* tables or downstream result tables"
    assert forbidden not in CLASSIFICATION_SYSTEM_PROMPT, (
        "The pre-Phase-4 broad FCT_* trigger has crept back into the "
        "classification prompt. Phase 4's scope correction tied the "
        "trigger to reconciliation phrasing only — bare FCT_* references "
        "must remain DATA_QUERY-eligible."
    )


def test_prompt_uses_reconciliation_language_trigger():
    """The new trigger must be reconciliation phrasing, not pattern
    matching on table names. The bullet should mention 'differs from',
    'doesn't match', or 'reconcile X with Y'."""
    # Case-insensitive substring checks — exact wording is allowed to
    # drift across edits as long as the reconciliation framing remains.
    lower = CLASSIFICATION_SYSTEM_PROMPT.lower()
    assert "reconciliation queries comparing values" in lower, (
        "Reconciliation-trigger framing is missing from the UNSUPPORTED "
        "bullet — the prompt may have regressed."
    )
    # At least one reconciliation phrasing example must appear.
    phrasings = ("differs from", "doesn't match", "reconcile")
    assert any(p in lower for p in phrasings), (
        "Expected at least one reconciliation phrasing example "
        f"({phrasings}) in the prompt; none found."
    )


def test_prompt_has_explicit_fct_aggregate_positive_example():
    """A few-shot example must demonstrate a bare FCT_* aggregate
    classifying as DATA_QUERY (not UNSUPPORTED). Without an explicit
    positive, the LLM may default to refusing FCT_* queries based on
    its training-data prior."""
    # The example uses FCT_STANDARD_ACCT_HEAD because OFSERM owns it
    # and it's the canary (e) target.
    assert "FCT_STANDARD_ACCT_HEAD" in CLASSIFICATION_SYSTEM_PROMPT
    # The DATA_QUERY classification must follow the FCT_STANDARD_ACCT_HEAD
    # mention within a small window — the example must teach
    # FCT_* + aggregate -> DATA_QUERY, not UNSUPPORTED.
    idx = CLASSIFICATION_SYSTEM_PROMPT.index("FCT_STANDARD_ACCT_HEAD")
    window = CLASSIFICATION_SYSTEM_PROMPT[idx:idx + 400]
    assert 'query_type: "DATA_QUERY"' in window, (
        "FCT_STANDARD_ACCT_HEAD example exists but doesn't clearly "
        "classify as DATA_QUERY — the positive case isn't being taught."
    )


def test_prompt_keeps_reconciliation_negative_examples():
    """Both pre-Phase-4 negative examples remain — they ARE genuine
    reconciliations (use 'differ'/'differs from') and must still
    classify as UNSUPPORTED to guard the rule from over-relaxing."""
    # Example 1: "Why does N_EOP_BAL differ between STG and FCT ..."
    assert "differ between STG and FCT" in CLASSIFICATION_SYSTEM_PROMPT
    # Example 2: "FCT_PRODUCT_EXPOSURES value differs from STG_PRODUCT_PROCESSOR ..."
    assert "differs from STG_PRODUCT_PROCESSOR" in CLASSIFICATION_SYSTEM_PROMPT
    # Both must be paired with UNSUPPORTED classification; spot-check by
    # locating the second example and confirming UNSUPPORTED appears in
    # the immediate window.
    idx = CLASSIFICATION_SYSTEM_PROMPT.index("differs from STG_PRODUCT_PROCESSOR")
    window = CLASSIFICATION_SYSTEM_PROMPT[idx:idx + 300]
    assert 'query_type: "UNSUPPORTED"' in window, (
        "FCT_PRODUCT_EXPOSURES reconciliation example no longer classifies "
        "as UNSUPPORTED — the negative case has regressed."
    )


def test_prompt_generalizes_current_schema_to_discovered_schemas():
    """The pre-Phase-4 wording 'capability outside read-only introspection
    of the current schema + graph' was schema-singular. Phase 4 makes it
    plural / discovery-driven. The literal phrase 'the current schema'
    in that bullet must NOT remain — and a plural form should appear
    somewhere in the prompt to confirm the generalization landed."""
    # The exact pre-Phase-4 phrase must be gone.
    assert "the current schema + graph" not in CLASSIFICATION_SYSTEM_PROMPT, (
        "Schema-singular phrasing 'the current schema + graph' is still "
        "present — Phase 4's generalization to discovered schemas hasn't "
        "fully landed."
    )
    # And a plural / discovery-driven form should appear.
    assert re.search(
        r"(any discovered schema|discovered schemas)",
        CLASSIFICATION_SYSTEM_PROMPT,
    ), (
        "Plural / discovery-driven schema phrasing not found — the "
        "Phase 4 generalization may have been removed."
    )
