"""W108 — explainer raw-source concat token-budget cap.

Background. After the b68918a corpus expansion (2026-05-19, Stage 1-3,
+30 active functions), the explainer's ``stream_semantic`` raw-source
fallback path concatenated 35 retrieved functions into a single user
prompt totalling 134,652 tokens for "How does FN_LOAD_OPS_RISK_DATA
work?". gpt-4o-mini's 128K context limit was exceeded by ~5% and
OpenAI returned ``BadRequestError: context_length_exceeded``. The
explainer surfaced DECLINED with type=llm_api_error and zero source
citations — every broad "How does X work?" query was broken in
practice.

W108 adds a defensive char-budget cap before the prompt is sent. Two
contracts pinned by these tests:

1. **Cap is silent when not needed** — small multi_source dicts pass
   through unchanged, no functions dropped, no warning emitted.
2. **Cap preserves the W97 anchor** — position 0 in the multi_source
   dict is the W97-promoted anchor; it must survive the cap even when
   the anchor's own section is larger than the budget.

The integration with ``stream_semantic`` is exercised live by the W98
canary battery (Canary A: "How does FN_LOAD_OPS_RISK_DATA work?"); the
unit suite here pins the helper in isolation so future refactors of
the streamer don't silently bypass the cap.
"""
from __future__ import annotations

import pytest

from src.agents.logic_explainer import (
    LogicExplainer,
    SOURCE_CONCAT_CHAR_BUDGET,
)


def _section_chars(fn_name: str, source_lines: int = 50, line_len: int = 80) -> int:
    """Estimate the formatted-section size for a function with the given
    number of source lines and per-line length. Used to pick body sizes
    that deterministically straddle a budget in the tests below."""
    # Header overhead (FUNCTION / Description / Tables Read / Tables
    # Written / Source Code: …) is ~250 chars on average; plus the body
    # of ~(line_len + 6) chars per source line ("L{n}: " prefix + text).
    return 250 + source_lines * (line_len + 6)


def _make_fn(name: str, *, body_lines: int = 50, line_len: int = 80, score: float = 0.5) -> dict:
    """Build a synthetic multi_source entry with predictable source size."""
    text = "X" * line_len
    return {
        "source_code": [
            {"line": i + 1, "text": text} for i in range(body_lines)
        ],
        "score": score,
        "description": "synthetic test function",
        "tables_read": "FCT_TEST",
        "tables_written": "FCT_TEST_OUT",
    }


@pytest.fixture
def explainer():
    """A bare LogicExplainer — _build_capped_concat_sections doesn't
    touch instance state, so no setup is needed."""
    # Bypass __init__ side effects (e.g. LLM factory creation) by
    # constructing without calling __init__. The helper only uses
    # ``self`` to reach _format_source_code, which is also instance-
    # only-by-convention. A vanilla instance suffices.
    return LogicExplainer.__new__(LogicExplainer)


class TestNoCapNeeded:
    """When the multi_source total fits comfortably under the budget,
    the helper is a pass-through — no drops, no warning."""

    def test_small_multi_source_passes_through_unchanged(self, explainer):
        multi_source = {
            f"FN_{i}": _make_fn(f"FN_{i}", body_lines=10) for i in range(5)
        }
        sections, kept, dropped, total = explainer._build_capped_concat_sections(
            multi_source
        )

        assert kept == 5
        assert dropped == []
        assert total < SOURCE_CONCAT_CHAR_BUDGET
        # Order preserved — sections appear in iteration order.
        assert "FN_0" in sections[0]
        assert "FN_4" in sections[4]

    def test_empty_multi_source_returns_empty_lists(self, explainer):
        sections, kept, dropped, total = explainer._build_capped_concat_sections({})
        assert sections == []
        assert kept == 0
        assert dropped == []
        assert total == 0


class TestCapFires:
    """When the running total would exceed the budget, lower-ranked
    functions are dropped while position 0 (the W97 anchor) is
    preserved."""

    def test_lower_ranked_dropped_when_budget_exceeded(self, explainer):
        # 10 functions of ~4000 chars each ≈ 40K chars total. A 20K-char
        # budget will keep the first ~5 and drop the rest.
        multi_source = {
            f"FN_{i:02d}": _make_fn(f"FN_{i:02d}", body_lines=50)
            for i in range(10)
        }
        budget = 20_000

        sections, kept, dropped, total = explainer._build_capped_concat_sections(
            multi_source, char_budget=budget
        )

        assert 0 < kept < 10, f"expected partial keep, got kept={kept}"
        assert len(dropped) == 10 - kept
        assert total <= budget + len(sections[-1])  # last kept may straddle
        # Anchor preserved at position 0
        assert "FN_00" in sections[0]
        # Dropped functions are the tail (lower-ranked), in order
        dropped_indices = [int(name.split("_")[1]) for name in dropped]
        assert dropped_indices == sorted(dropped_indices)
        assert min(dropped_indices) > max(
            int(s.split("FUNCTION: FN_")[1][:2]) for s in sections
        )

    def test_anchor_preserved_even_when_its_own_section_exceeds_budget(self, explainer):
        """W97 contract: the anchor at position 0 must survive the cap
        unconditionally, even if its own section is larger than the
        budget. An explainer response without the anchor is useless."""
        # One huge anchor + a few small followers. Budget set BELOW the
        # anchor's own size so the natural "running > budget" guard
        # would drop everything if not for the position-0 exemption.
        multi_source = {
            "ANCHOR_FN": _make_fn("ANCHOR_FN", body_lines=500),  # ~43K chars
            "FOLLOWER_1": _make_fn("FOLLOWER_1", body_lines=10),
            "FOLLOWER_2": _make_fn("FOLLOWER_2", body_lines=10),
        }
        budget = 1_000  # well below the anchor's own size

        sections, kept, dropped, total = explainer._build_capped_concat_sections(
            multi_source, char_budget=budget
        )

        assert kept == 1, "anchor must be kept even when oversized"
        assert "ANCHOR_FN" in sections[0]
        assert dropped == ["FOLLOWER_1", "FOLLOWER_2"]

    def test_cap_default_uses_module_constant(self, explainer):
        """Caller-less invocation uses ``SOURCE_CONCAT_CHAR_BUDGET``.
        Documents the public-default contract so a future refactor that
        changes the default won't silently regress the constant."""
        multi_source = {
            "FN_A": _make_fn("FN_A", body_lines=5),
            "FN_B": _make_fn("FN_B", body_lines=5),
        }
        sections_default, _, dropped_default, _ = (
            explainer._build_capped_concat_sections(multi_source)
        )
        sections_explicit, _, dropped_explicit, _ = (
            explainer._build_capped_concat_sections(
                multi_source, char_budget=SOURCE_CONCAT_CHAR_BUDGET
            )
        )
        assert sections_default == sections_explicit
        assert dropped_default == dropped_explicit


class TestBudgetBoundary:
    """Boundary case: a single follower whose section exactly fits.

    Pins the inclusive-or-exclusive boundary so a future refactor that
    flips ``>`` to ``>=`` (or vice versa) trips this test."""

    def test_follower_fitting_within_budget_is_kept(self, explainer):
        anchor = _make_fn("ANCHOR_FN", body_lines=10)
        follower = _make_fn("FOLLOWER_FN", body_lines=10)

        # Probe: build each section in isolation to get exact sizes
        anchor_section_size = len(
            explainer._build_capped_concat_sections({"ANCHOR_FN": anchor})[0][0]
        )
        follower_section_size = len(
            explainer._build_capped_concat_sections({"FOLLOWER_FN": follower})[0][0]
        )

        # Budget set so both JUST fit (sum of both section sizes).
        budget_just_fits = anchor_section_size + follower_section_size
        multi_source = {"ANCHOR_FN": anchor, "FOLLOWER_FN": follower}
        _, kept, dropped, _ = explainer._build_capped_concat_sections(
            multi_source, char_budget=budget_just_fits
        )
        assert kept == 2
        assert dropped == []

        # Budget one char short — follower must be dropped.
        _, kept, dropped, _ = explainer._build_capped_concat_sections(
            multi_source, char_budget=budget_just_fits - 1
        )
        assert kept == 1
        assert dropped == ["FOLLOWER_FN"]
