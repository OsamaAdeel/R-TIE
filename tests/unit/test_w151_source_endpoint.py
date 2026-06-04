"""W151 Phase 5 — /v1/source bounded-window unit tests.

Covers the trust-critical W51 bound directly via the pure helper extracted
from the endpoint: the cited range + fixed margin, clamped to the body, and
HARD-capped at SOURCE_MAX_LINES so a single response can never dump a whole
function body. No server / Redis / Oracle needed.
"""

from src.main import (
    _bound_source_window,
    SOURCE_CONTEXT_MARGIN,
    SOURCE_MAX_LINES,
)


def _src(lo, hi):
    """Numbered source_code (the {line, text} shape fetch_logic returns)."""
    return [{"line": n, "text": f"line {n}"} for n in range(lo, hi + 1)]


def test_window_applies_fixed_margin_each_side():
    src = _src(1, 100)
    ws, we, lines, clamped = _bound_source_window(src, 40, 50)
    assert ws == 40 - SOURCE_CONTEXT_MARGIN  # 37
    assert we == 50 + SOURCE_CONTEXT_MARGIN  # 53
    assert [l["line"] for l in lines] == list(range(37, 54))
    assert clamped is False


def test_margin_clamps_to_body_bounds():
    # citation at the very top/bottom — margin can't go past the body.
    src = _src(10, 20)
    ws, we, lines, clamped = _bound_source_window(src, 10, 20)
    assert ws == 10 and we == 20  # margin clamped to [10,20], not 7..23
    assert len(lines) == 11


def test_hard_cap_fires_on_pathological_range():
    # A [1, 99999]-style request must NOT dump the whole (large) body.
    src = _src(1, 5000)
    ws, we, lines, clamped = _bound_source_window(src, 1, 99999)
    assert clamped is True
    assert len(lines) == SOURCE_MAX_LINES          # the hard ceiling, exactly
    assert we - ws + 1 == SOURCE_MAX_LINES
    assert ws == 1                                  # capped from the window start


def test_cap_is_the_ceiling_regardless_of_input():
    # Property: the returned line count never exceeds SOURCE_MAX_LINES.
    src = _src(1, 2000)
    for start, end in [(1, 2000), (1, 99999), (500, 1900), (1, 401)]:
        _, _, lines, _ = _bound_source_window(src, start, end)
        assert len(lines) <= SOURCE_MAX_LINES


def test_small_range_under_cap_not_clamped():
    src = _src(1, 5000)
    _, _, lines, clamped = _bound_source_window(src, 100, 120)
    assert clamped is False
    assert len(lines) == (120 - 100 + 1) + 2 * SOURCE_CONTEXT_MARGIN


def test_range_outside_body_returns_empty():
    src = _src(1, 50)
    _, _, lines, clamped = _bound_source_window(src, 900, 950)
    assert lines == [] and clamped is False


def test_empty_source_returns_empty():
    ws, we, lines, clamped = _bound_source_window([], 1, 10)
    assert (ws, we, lines, clamped) == (0, 0, [], False)


def test_ignores_malformed_line_entries():
    src = [{"line": 1, "text": "a"}, {"text": "no line"}, {"line": 2, "text": "b"}, "junk"]
    _, _, lines, _ = _bound_source_window(src, 1, 2)
    assert [l["line"] for l in lines] == [1, 2]
