"""W70 — explainer anchor injection.

Cascade tests for :func:`determine_primary_anchor`, boundary cases for
:func:`_is_clean_function_name`, output shape for
:func:`build_anchor_block` at each confidence tier, and an integration
test that verifies :meth:`LogicExplainer.stream_semantic` prepends the
W70 anchor block to the system message before invoking the LLM.

W70 fixes the dominant fabrication pattern from v2 benchmark Run 7:
the explainer LLM picks the wrong function from the retrieved set to
anchor its body on. Two flavors:

  Flavor 1 — anchor on a hallucinated function NOT in retrieval
  (CAP973 case, citing REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP).

  Flavor 2 — anchor on a real-but-wrong function FROM retrieval
  (the OPS_RISK_DATA_POPULATION_CSTM C2 case whose body drifted to
  the upstream CAP_CONSL_EFFECTIVE_SHAREHOLDING_PERCENT).

These tests cover the cascade that picks the primary anchor and the
prompt-block builder that formalizes it for the LLM.
"""

from __future__ import annotations

import pytest

from src.agents.anchor_resolution import (
    _is_clean_function_name,
    apply_w70_anchor,
    build_anchor_block,
    determine_primary_anchor,
)


# ---------------------------------------------------------------------------
# determine_primary_anchor cascade
# ---------------------------------------------------------------------------


class TestDetermineprimaryAnchorCascade:
    """The four-layer cascade picks the strongest available signal."""

    def test_w76_prefix_anchor_wins_over_everything_else(self):
        """Layer 1: when the orchestrator's W76 prefix rule fired, that
        function is the user's explicit anchor and beats every other
        layer regardless of what's in object_name / bi_routing /
        multi_source."""
        state = {
            "w76_anchor": {
                "function": "OPS_RISK_DATA_POPULATION_CSTM",
                "source": "prefix",
            },
            "object_name": "OPS_RISK_DATA_POPULATION_CSTM",
            "bi_routing": {"function": "OTHER_FN"},
            "multi_source": {"YET_ANOTHER_FN": {"score": 0.05}},
        }
        out = determine_primary_anchor(state)
        assert out == {
            "function": "OPS_RISK_DATA_POPULATION_CSTM",
            "source": "w76_prefix",
            "confidence": "high",
        }

    def test_w76_alias_fallback_keeps_high_confidence(self):
        """Mechanism 2 of W76 (alias-literal target_variable + recovered
        function) is also an explicit anchor — high confidence — but the
        source label distinguishes it from the prefix variant."""
        state = {
            "w76_anchor": {
                "function": "ENCLOSING_FN_NAME",
                "source": "alias_fallback",
            },
            "object_name": "How is X calculated?",
            "multi_source": {},
        }
        out = determine_primary_anchor(state)
        assert out == {
            "function": "ENCLOSING_FN_NAME",
            "source": "w76_alias_fallback",
            "confidence": "high",
        }

    def test_w76_empty_function_falls_through(self):
        """When W76 cleared an alias literal but found no enclosing
        function (alias_fallback_no_function variant), function is ""
        and the cascade must keep walking."""
        state = {
            "w76_anchor": {
                "function": "",
                "alias_literal_cleared": "EXP_11",
                "source": "alias_fallback_no_function",
            },
            "object_name": "FN_LOAD_OPS_RISK_DATA",
            "bi_routing": {},
            "multi_source": {},
        }
        out = determine_primary_anchor(state)
        assert out["function"] == "FN_LOAD_OPS_RISK_DATA"
        assert out["source"] == "classifier_object"
        assert out["confidence"] == "high"

    def test_clean_object_name_when_no_w76_no_bi(self):
        """Layer 2: a clean function name in object_name (e.g. when a
        future classifier variant produces one directly, or W76 wrote
        it) anchors at high confidence."""
        state = {
            "w76_anchor": {},
            "bi_routing": {},
            "object_name": "FN_LOAD_OPS_RISK_DATA",
            "multi_source": {"FN_LOAD_OPS_RISK_DATA": {"score": 0.05}},
        }
        out = determine_primary_anchor(state)
        assert out == {
            "function": "FN_LOAD_OPS_RISK_DATA",
            "source": "classifier_object",
            "confidence": "high",
        }

    def test_enriched_blob_object_name_falls_through_to_bi(self):
        """The classifier's enriched search blob is NOT a clean function
        name. Layer 2 must skip it and the cascade must reach Layer 3
        (BI routing)."""
        state = {
            "w76_anchor": {},
            "object_name": "How is CAP973 calculated? Explain step by step.",
            "bi_routing": {
                "function": "CS_PHASE_IN_DEDUCTION_AMOUNT",
                "identifier": "CAP973",
            },
            "multi_source": {"CS_PHASE_IN_DEDUCTION_AMOUNT": {"score": 0.2}},
        }
        out = determine_primary_anchor(state)
        assert out == {
            "function": "CS_PHASE_IN_DEDUCTION_AMOUNT",
            "source": "bi_routing",
            "confidence": "medium",
        }

    def test_bi_rewrote_object_name_uses_bi_layer_not_classifier(self):
        """When BI routing rewrote object_name to a clean function
        name, the cascade must still label the anchor as ``bi_routing``
        (medium) rather than ``classifier_object`` (high). The user
        typed a CAP code, not a function name — the medium framing is
        the honest one."""
        state = {
            "w76_anchor": {},
            "object_name": "CS_PHASE_IN_DEDUCTION_AMOUNT",
            "bi_routing": {
                "function": "CS_PHASE_IN_DEDUCTION_AMOUNT",
                "identifier": "CAP973",
            },
            "multi_source": {},
        }
        out = determine_primary_anchor(state)
        assert out["source"] == "bi_routing"
        assert out["confidence"] == "medium"
        assert out["function"] == "CS_PHASE_IN_DEDUCTION_AMOUNT"

    def test_semantic_top1_when_only_multi_source(self):
        """Layer 5: with no anchor, no clean object_name, no BI, no
        raw_query-named function in multi_source, the cascade picks
        the lowest-score (best cosine match) entry from multi_source
        at low confidence."""
        state = {
            "w76_anchor": {},
            "object_name": "How does X work?",
            "bi_routing": {},
            "raw_query": "How does X work?",
            "multi_source": {
                "WORST": {"score": 0.5},
                "BEST": {"score": 0.05},
                "MIDDLE": {"score": 0.2},
            },
        }
        out = determine_primary_anchor(state)
        assert out == {
            "function": "BEST",
            "source": "semantic_top1",
            "confidence": "low",
        }

    def test_semantic_top1_handles_missing_score(self):
        """Entries without a score field are treated as worst-rank so
        a real low-score entry still wins."""
        state = {
            "w76_anchor": {},
            "object_name": "",
            "bi_routing": {},
            "multi_source": {
                "NO_SCORE": {},
                "WITH_SCORE": {"score": 0.1},
            },
        }
        out = determine_primary_anchor(state)
        assert out["function"] == "WITH_SCORE"

    def test_empty_state_returns_none(self):
        """No anchor signal of any kind — caller skips block injection."""
        state = {
            "w76_anchor": {},
            "object_name": "",
            "bi_routing": {},
            "multi_source": {},
        }
        assert determine_primary_anchor(state) is None

    def test_completely_empty_state_returns_none(self):
        """Defensive: missing keys must not raise."""
        assert determine_primary_anchor({}) is None


# ---------------------------------------------------------------------------
# W98 — raw_query function-name scan layer (cascade Layer 4)
# ---------------------------------------------------------------------------


class TestW98RawQueryScanLayer:
    """W98 — closes the gap W80 v1 exposed: bare-function-name queries
    ("How does <Fn> work?" / "Explain <Fn>") that escape Layer 1 (no
    "In <Fn>," prefix) and Layer 3 (no BI code) used to fall straight
    to Layer 5 semantic top-1, which on the FN_LOAD_OPS_RISK_DATA
    canary returned the wrong function (PREV_QTR_CET1_…).

    The new Layer 4 scans raw_query via ``extract_function_candidates``
    and fires when exactly ONE candidate is also present in
    ``multi_source`` — the post-fetch survivorship gate keeps the
    diagnostic stamp honest about what the explainer actually has a
    body for.
    """

    def test_bare_function_query_fires_high_confidence(self):
        """The W84 canary: ``How does FN_LOAD_OPS_RISK_DATA work?``
        has no W76 prefix, no BI code, no clean object_name. With the
        named function present in multi_source (post-W80c-v2 widened
        retrieval surfaces it at rank 30), Layer 4 anchors on it at
        high confidence."""
        state = {
            "w76_anchor": {},
            "object_name": "",
            "bi_routing": {},
            "raw_query": "How does FN_LOAD_OPS_RISK_DATA work?",
            "multi_source": {
                # Note: the wrong-anchor case has the asked-about
                # function at a low rank. Cascade still picks it via
                # Layer 4 regardless of multi_source order.
                "PREV_QTR_CET1_STANDARD_ACCT_HEAD_DATA_POP": {
                    "score": 0.10
                },
                "FN_LOAD_OPS_RISK_DATA": {"score": 0.30},
            },
        }
        out = determine_primary_anchor(state)
        assert out == {
            "function": "FN_LOAD_OPS_RISK_DATA",
            "source": "raw_query_scan",
            "confidence": "high",
        }

    def test_w76_prefix_wins_over_w98(self):
        """When the W76 prefix rule fires, Layer 1 returns immediately
        — Layer 4 must not double-stamp or override."""
        state = {
            "w76_anchor": {
                "function": "FN_FROM_PREFIX",
                "source": "prefix",
            },
            "object_name": "FN_FROM_PREFIX",
            "bi_routing": {},
            "raw_query": (
                "In FN_FROM_PREFIX, what does FN_OTHER_LOOKUP do?"
            ),
            "multi_source": {
                "FN_FROM_PREFIX": {"score": 0.05},
                "FN_OTHER_LOOKUP": {"score": 0.10},
            },
        }
        out = determine_primary_anchor(state)
        assert out["function"] == "FN_FROM_PREFIX"
        assert out["source"] == "w76_prefix"
        assert out["confidence"] == "high"

    def test_bi_routing_wins_over_w98(self):
        """BI routing (Layer 3) fires before the W98 raw_query scan
        (Layer 4). A CAP-code query that ALSO mentions a function
        name in body (e.g. "How is CAP973 calculated by FN_FOO?")
        anchors via BI at medium confidence, not via W98."""
        state = {
            "w76_anchor": {},
            "object_name": "",
            "bi_routing": {
                "function": "CS_PHASE_IN_DEDUCTION_AMOUNT",
                "identifier": "CAP973",
            },
            "raw_query": "How is CAP973 calculated?",
            "multi_source": {
                "CS_PHASE_IN_DEDUCTION_AMOUNT": {"score": 0.08},
            },
        }
        out = determine_primary_anchor(state)
        assert out["function"] == "CS_PHASE_IN_DEDUCTION_AMOUNT"
        assert out["source"] == "bi_routing"
        assert out["confidence"] == "medium"

    def test_no_function_named_falls_through(self):
        """``Trace N_EOP_BAL`` — N_EOP_BAL is filtered by W58 column-
        type-prefix rules in extract_function_candidates, so the
        candidate list is empty and Layer 4 doesn't fire. Falls
        through to Layer 5 semantic top-1."""
        state = {
            "w76_anchor": {},
            "object_name": "",
            "bi_routing": {},
            "raw_query": "Trace N_EOP_BAL",
            "multi_source": {
                "SOME_UPSTREAM_FN": {"score": 0.20},
                "ANOTHER_FN": {"score": 0.40},
            },
        }
        out = determine_primary_anchor(state)
        assert out["source"] == "semantic_top1"
        assert out["function"] == "SOME_UPSTREAM_FN"
        assert out["confidence"] == "low"

    def test_two_candidates_one_in_multi_source_fires(self):
        """``How does FN_X call FN_Y?`` — two callable candidates. If
        only one survived retrieval, Layer 4 anchors on the surviving
        one at high confidence (the user's question implies both, but
        the body we have is the one to describe)."""
        state = {
            "w76_anchor": {},
            "object_name": "",
            "bi_routing": {},
            "raw_query": (
                "How does FN_LOAD_OPS_RISK_DATA call FN_MISSING_HELPER?"
            ),
            "multi_source": {
                "FN_LOAD_OPS_RISK_DATA": {"score": 0.20},
                "UNRELATED_SIBLING": {"score": 0.10},
            },
        }
        out = determine_primary_anchor(state)
        assert out["function"] == "FN_LOAD_OPS_RISK_DATA"
        assert out["source"] == "raw_query_scan"
        assert out["confidence"] == "high"

    def test_candidate_not_in_multi_source_falls_through(self):
        """``How does FN_UNKNOWN_FUNCTION work?`` — candidate passes
        extract_function_candidates but isn't in multi_source.
        Layer 4 honestly skips (would no-op W97 promotion anyway).
        Falls through to Layer 5."""
        state = {
            "w76_anchor": {},
            "object_name": "",
            "bi_routing": {},
            "raw_query": "How does FN_UNKNOWN_FUNCTION work?",
            "multi_source": {
                "RANDOM_SEMANTIC_HIT": {"score": 0.30},
            },
        }
        out = determine_primary_anchor(state)
        assert out["function"] == "RANDOM_SEMANTIC_HIT"
        assert out["source"] == "semantic_top1"

    def test_multiple_candidates_multiple_in_multi_source_falls_through(self):
        """Genuinely ambiguous: two named functions both survived
        retrieval. Layer 4 abstains rather than pick — Layer 5
        semantic top-1 takes over, and W85 ANCHOR-MISMATCH-HIGH
        provides defense-in-depth if top-1 disagrees with the user's
        intent."""
        state = {
            "w76_anchor": {},
            "object_name": "",
            "bi_routing": {},
            "raw_query": (
                "Compare FN_LOAD_OPS_RISK_DATA and FN_LOAD_OPS_RISK_CBA"
            ),
            "multi_source": {
                "FN_LOAD_OPS_RISK_DATA": {"score": 0.20},
                "FN_LOAD_OPS_RISK_CBA": {"score": 0.10},
                "UNRELATED": {"score": 0.40},
            },
        }
        out = determine_primary_anchor(state)
        # Layer 4 skips. Layer 5 picks lowest-score.
        assert out["source"] == "semantic_top1"
        assert out["function"] == "FN_LOAD_OPS_RISK_CBA"

    def test_empty_multi_source_returns_none(self):
        """Defensive: empty multi_source means there's nothing the
        explainer can anchor on. Layer 4 abstains (no point stamping
        a function whose body isn't loaded), Layer 5 has nothing to
        pick from, cascade returns None."""
        state = {
            "w76_anchor": {},
            "object_name": "",
            "bi_routing": {},
            "raw_query": "How does FN_LOAD_OPS_RISK_DATA work?",
            "multi_source": {},
        }
        assert determine_primary_anchor(state) is None

    def test_case_insensitive_multi_source_match(self):
        """``extract_function_candidates`` preserves case; multi_source
        keys are uppercase (loader convention). Layer 4 matches case-
        insensitively and returns the multi_source key as-stored."""
        state = {
            "w76_anchor": {},
            "object_name": "",
            "bi_routing": {},
            "raw_query": "How does Fn_Load_Ops_Risk_Data work?",
            "multi_source": {
                "FN_LOAD_OPS_RISK_DATA": {"score": 0.25},
            },
        }
        out = determine_primary_anchor(state)
        assert out is not None
        assert out["function"] == "FN_LOAD_OPS_RISK_DATA"
        assert out["source"] == "raw_query_scan"


# ---------------------------------------------------------------------------
# _is_clean_function_name boundary cases
# ---------------------------------------------------------------------------


class TestIsCleanFunctionName:
    """Discriminator for the orchestrator's clean-anchor object_name vs
    the classifier's enriched search-query blob, with W58 filtering."""

    def test_uppercase_pl_sql_function_name(self):
        assert _is_clean_function_name("FN_LOAD_OPS_RISK_DATA") is True

    def test_real_long_function_name(self):
        assert (
            _is_clean_function_name("OPS_RISK_DATA_POPULATION_CSTM")
            is True
        )

    def test_enriched_search_blob_is_not_clean(self):
        """The classifier puts a long question/intent string into
        object_name; this must NOT be treated as a function name."""
        assert (
            _is_clean_function_name(
                "How does FN_LOAD_OPS_RISK_DATA work?"
            )
            is False
        )

    def test_alias_literal_is_not_clean(self):
        """W58.b: EXP_11 is an OFSAA-generated CASE-branch label, not
        a callable function."""
        assert _is_clean_function_name("EXP_11") is False

    def test_table_prefix_is_not_clean(self):
        """W58.a: FCT_ tables are never functions."""
        assert _is_clean_function_name("FCT_OPS_RISK_DATA") is False

    def test_column_prefix_is_not_clean(self):
        """W58.c / column-type prefix: N_EOP_BAL is a column."""
        assert _is_clean_function_name("N_EOP_BAL") is False

    def test_empty_string_is_not_clean(self):
        assert _is_clean_function_name("") is False

    def test_whitespace_only_is_not_clean(self):
        assert _is_clean_function_name("   ") is False

    def test_too_short_is_not_clean(self):
        """extract_function_candidates rejects names < 6 chars."""
        assert _is_clean_function_name("FN_X") is False

    def test_no_underscore_is_not_clean(self):
        """Bare uppercase keyword 'CASE' must not pass."""
        assert _is_clean_function_name("CASE") is False


# ---------------------------------------------------------------------------
# build_anchor_block confidence-tiered output
# ---------------------------------------------------------------------------


class TestBuildAnchorBlock:
    """The block prepended to SEMANTIC_EXPLANATION_PROMPT carries
    different language at each confidence tier."""

    def test_high_confidence_must_language(self):
        block = build_anchor_block(
            {
                "function": "FN_TARGET",
                "source": "w76_prefix",
                "confidence": "high",
            }
        )
        assert "PRIMARY FUNCTION: FN_TARGET" in block
        assert "MUST describe THIS function" in block
        assert "say so explicitly" in block
        # The high block ends with a blank line so the existing prompt
        # body starts cleanly.
        assert block.endswith("\n\n")

    def test_medium_confidence_bi_routing_language(self):
        block = build_anchor_block(
            {
                "function": "FN_BI_TARGET",
                "source": "bi_routing",
                "confidence": "medium",
            }
        )
        assert "PRIMARY FUNCTION: FN_BI_TARGET" in block
        assert "business identifier" in block
        # Medium does NOT use the strict MUST language.
        assert "MUST describe" not in block

    def test_low_confidence_likely_primary_language(self):
        block = build_anchor_block(
            {
                "function": "FN_TOP1",
                "source": "semantic_top1",
                "confidence": "low",
            }
        )
        assert "LIKELY PRIMARY FUNCTION: FN_TOP1" in block
        assert "may anchor" in block
        # Low explicitly permits anchoring elsewhere with a name-it-up-
        # front instruction.
        assert "state which function you're describing" in block

    def test_none_anchor_returns_empty_string(self):
        """When the cascade returns None, no block is prepended."""
        assert build_anchor_block(None) == ""


# ---------------------------------------------------------------------------
# apply_w70_anchor — stamps state, logs decision
# ---------------------------------------------------------------------------


class TestApplyW70Anchor:
    """The user-facing helper stamps state["w70_anchor"] for
    diagnostic visibility — the same pattern as W76's anchor stamp."""

    def test_stamps_high_confidence_anchor(self):
        state = {
            "w76_anchor": {"function": "PRIMARY_FN", "source": "prefix"},
            "object_name": "",
            "bi_routing": {},
            "multi_source": {},
        }
        out = apply_w70_anchor(state)
        assert out["function"] == "PRIMARY_FN"
        assert out["source"] == "w76_prefix"
        assert out["confidence"] == "high"
        # Diagnostic stamp matches the returned anchor.
        assert state["w70_anchor"] == out

    def test_stamps_none_when_no_signal(self):
        state = {
            "w76_anchor": {},
            "object_name": "",
            "bi_routing": {},
            "multi_source": {},
        }
        out = apply_w70_anchor(state)
        assert out is None
        assert state["w70_anchor"] is None


# ---------------------------------------------------------------------------
# Integration: stream_semantic prepends anchor block to system prompt
# ---------------------------------------------------------------------------


class _FakeChunk:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeStreamingLLM:
    """Captures the messages it would send and yields a single token."""

    def __init__(self) -> None:
        self.captured_messages = None

    async def astream(self, messages):
        self.captured_messages = messages
        yield _FakeChunk("ok")


@pytest.mark.asyncio
async def test_stream_semantic_prepends_high_confidence_anchor(monkeypatch):
    """End-to-end verification: with state["w76_anchor"] anchoring on
    OPS_RISK_DATA_POPULATION_CSTM, the SystemMessage sent to the LLM
    must begin with the high-confidence anchor block citing that
    function — independent of which order multi_source iterates."""
    from src.agents import logic_explainer

    fake_llm = _FakeStreamingLLM()

    def fake_create_llm(**kwargs):
        return fake_llm

    monkeypatch.setattr(logic_explainer, "create_llm", fake_create_llm)

    state = {
        "raw_query": (
            "In OPS_RISK_DATA_POPULATION_CSTM, where does "
            "N_SHAREHOLDING_PERCENT come from?"
        ),
        "w76_anchor": {
            "function": "OPS_RISK_DATA_POPULATION_CSTM",
            "source": "prefix",
        },
        "object_name": "OPS_RISK_DATA_POPULATION_CSTM",
        "bi_routing": {},
        "multi_source": {
            # Intentionally place the upstream sibling first to prove
            # iteration order doesn't matter — the anchor cascade picks
            # via W76, not via dict order.
            "CAP_CONSL_EFFECTIVE_SHAREHOLDING_PERCENT": {
                "score": 0.10,
                "description": "x",
                "tables_read": "x",
                "tables_written": "x",
                "source_code": [],
            },
            "OPS_RISK_DATA_POPULATION_CSTM": {
                "score": 0.05,
                "description": "x",
                "tables_read": "x",
                "tables_written": "x",
                "source_code": [],
            },
        },
    }

    explainer = logic_explainer.LogicExplainer()
    async for _ in explainer.stream_semantic(state):
        pass

    assert fake_llm.captured_messages is not None
    sys_content = fake_llm.captured_messages[0].content

    # Anchor block prepended verbatim.
    assert sys_content.startswith(
        "PRIMARY FUNCTION: OPS_RISK_DATA_POPULATION_CSTM"
    )
    assert "MUST describe THIS function" in sys_content

    # Existing prompt body preserved unchanged below the block.
    assert "RULES:" in sys_content
    assert "OFSAA FSAPPS regulatory capital calculations" in sys_content

    # Diagnostic stamp visible on state.
    assert state["w70_anchor"] == {
        "function": "OPS_RISK_DATA_POPULATION_CSTM",
        "source": "w76_prefix",
        "confidence": "high",
    }
