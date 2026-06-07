"""W162 Tier 2b: W57 check-1.3 megaline awareness.

Check 1.3 (range-repeat padding) fires ``GROUNDING-LOW`` when the same line
citation repeats more than the threshold. On a megaline-writer — a function
whose entire statement lives on ONE physical line (the ~5k-char OFSAA
one-liners W162 characterised, e.g. the ``CS_*`` writers of
FCT_STANDARD_ACCT_HEAD) — the model legitimately decomposes the one statement
into several logical steps, each correctly citing that single line ("Line 24"
× 5). Pre-Tier-2b this tripped a SPURIOUS padding warning on correct output.

The fix relaxes 1.3 ONLY when the repeated line is a CONFIRMED collapsed
``[N, N]`` span — verified by IDENTITY against the parsed graph nodes
(``line_start == line_end``), never by "the line was cited a lot". A normal
multi-line statement's interior line has no ``[N, N]`` node, so a repeated
citation of it is still padding and STILL fires — that two-sided discriminator
(megaline spared / multi-line still caught) is the guard-integrity proof.

Mirrors the W163 shape: identity (the span is really ``[N, N]``), not
assumption, and inert when ``redis_client`` is None so no offline hole opens.
"""

import pytest

import src.parsing.store as store
from src.agents.logic_explainer import (
    _w57_check_per_claim_binding,
    _w57_collapsed_megaline_lines,
)

# Sentinel: the helper only checks ``redis_client is None``; the real client is
# never touched because get_function_graph is monkeypatched.
_FAKE_REDIS = object()


def _src(line_count, text="dummy"):
    return [{"line": i, "text": text} for i in range(1, line_count + 1)]


def _patch_graph(monkeypatch, nodes_by_fn):
    """Make get_function_graph return ``{'nodes': nodes}`` per function.

    *nodes_by_fn* keys are upper-cased function names; values are lists of
    ``{line_start, line_end}`` node dicts. An absent function returns None
    (graph unreachable), exercising the helper's skip path.
    """
    upper = {k.upper(): v for k, v in nodes_by_fn.items()}

    def fake(redis_client, schema, function_name):
        nodes = upper.get((function_name or "").upper())
        return {"nodes": nodes} if nodes is not None else None

    monkeypatch.setattr(store, "get_function_graph", fake)


def _steps_citing(line, n):
    """*n* logical steps each citing the single physical ``Line {line}``."""
    return " ".join(f"Step {i}: it computes a sub-figure (Line {line})."
                    for i in range(1, n + 1))


# ===========================================================================
# FIXED: a confirmed megaline citation is no longer mistaken for padding
# ===========================================================================

def test_megaline_repeat_no_padding_warning(monkeypatch):
    """5× "Line 24" where line 24 is a confirmed [24,24] statement -> no LOW."""
    _patch_graph(monkeypatch, {"CS_NET_AT1": [{"line_start": 24, "line_end": 24}]})
    md = _steps_citing(24, 5)
    multi = {"CS_NET_AT1": {"source_code": _src(30), "schema": "OFSERM"}}
    warnings = _w57_check_per_claim_binding(
        md, multi, ["CS_NET_AT1"], redis_client=_FAKE_REDIS,
    )
    assert not any("Line 24 cited" in w for w in warnings), warnings
    assert warnings == [], warnings


def test_short_one_line_statement_also_spared(monkeypatch):
    """The gate is "is it a complete one-physical-line statement", NOT char
    length: a short genuine [N,N] statement cited per-step is also legitimate,
    so it is spared too. Documents that identity — not megaline size — is the
    axis."""
    _patch_graph(monkeypatch, {"FN_SHORT": [{"line_start": 7, "line_end": 7}]})
    md = _steps_citing(7, 6)
    multi = {"FN_SHORT": {"source_code": _src(20), "schema": "OFSERM"}}
    warnings = _w57_check_per_claim_binding(
        md, multi, ["FN_SHORT"], redis_client=_FAKE_REDIS,
    )
    assert not any("Line 7 cited" in w for w in warnings), warnings


# ===========================================================================
# STILL CAUGHT: padding on a multi-line statement keeps firing (guard proof)
# ===========================================================================

def test_multiline_interior_line_repeat_still_fires(monkeypatch):
    """GUARD-INTEGRITY PROOF. Line 24 is interior to a genuine MULTI-line
    statement [10,60] — there is NO [24,24] node — so repeating it is padding
    and the GROUNDING-LOW warning STILL fires. Same repeat count as the
    megaline case; only the span shape differs."""
    _patch_graph(monkeypatch, {"FN_NORMAL": [{"line_start": 10, "line_end": 60}]})
    md = _steps_citing(24, 5)
    multi = {"FN_NORMAL": {"source_code": _src(100), "schema": "OFSERM"}}
    warnings = _w57_check_per_claim_binding(
        md, multi, ["FN_NORMAL"], redis_client=_FAKE_REDIS,
    )
    assert any(
        w.startswith("GROUNDING-LOW:") and "Line 24 cited 5 times" in w
        for w in warnings
    ), warnings


def test_multiline_range_repeat_not_relaxed(monkeypatch):
    """A repeated MULTI-line range (start != end) is never a one-physical-line
    statement, so it is never relaxed — even when the function happens to own a
    [24,24] node elsewhere. The single-line gate alone excludes it."""
    _patch_graph(monkeypatch, {"FN_X": [{"line_start": 24, "line_end": 24}]})
    md = " ".join(f"Step {i} (Lines 24-30)." for i in range(1, 6))
    multi = {"FN_X": {"source_code": _src(100), "schema": "OFSERM"}}
    warnings = _w57_check_per_claim_binding(
        md, multi, ["FN_X"], redis_client=_FAKE_REDIS,
    )
    assert any(
        w.startswith("GROUNDING-LOW:") and "Lines 24-30 cited 5 times" in w
        for w in warnings
    ), warnings


def test_uncovered_line_repeat_still_fires(monkeypatch):
    """A repeated line that maps to NO node at all (comment / BEGIN-END /
    declaration) cannot be confirmed as a collapsed statement, so the
    conservative default holds: it still fires."""
    _patch_graph(monkeypatch, {"FN_Y": [{"line_start": 100, "line_end": 140}]})
    md = _steps_citing(24, 5)
    multi = {"FN_Y": {"source_code": _src(200), "schema": "OFSERM"}}
    warnings = _w57_check_per_claim_binding(
        md, multi, ["FN_Y"], redis_client=_FAKE_REDIS,
    )
    assert any("Line 24 cited 5 times" in w for w in warnings), warnings


# ===========================================================================
# Fallback: no redis_client -> relaxation inert, pre-Tier-2b behavior kept
# ===========================================================================

def test_no_redis_client_megaline_still_fires():
    """With redis_client=None (unit / pre-startup) the identity signal is
    unreachable, so the relaxation is inert and the warning fires exactly as
    before — the cure only applies on the live path, opening no offline hole."""
    md = _steps_citing(24, 5)
    multi = {"CS_NET_AT1": {"source_code": _src(30), "schema": "OFSERM"}}
    warnings = _w57_check_per_claim_binding(
        md, multi, ["CS_NET_AT1"],  # redis_client defaults to None
    )
    assert any("Line 24 cited 5 times" in w for w in warnings), warnings


def test_below_threshold_megaline_no_warning(monkeypatch):
    """Sanity: exactly 3 repeats is below the >3 threshold, so no warning
    fires regardless of span shape (and no graph scan is needed)."""
    _patch_graph(monkeypatch, {"CS_NET_AT1": [{"line_start": 24, "line_end": 24}]})
    md = _steps_citing(24, 3)
    multi = {"CS_NET_AT1": {"source_code": _src(30), "schema": "OFSERM"}}
    warnings = _w57_check_per_claim_binding(
        md, multi, ["CS_NET_AT1"], redis_client=_FAKE_REDIS,
    )
    assert not any("cited 3 times" in w for w in warnings), warnings


# ===========================================================================
# Helper unit: collapsed-span identity semantics
# ===========================================================================

def test_helper_none_redis_returns_empty():
    assert _w57_collapsed_megaline_lines(
        {"FN": {"schema": "OFSERM"}}, None
    ) == set()


def test_helper_collects_only_collapsed_spans(monkeypatch):
    """Only [N,N] nodes contribute; multi-line nodes are excluded."""
    _patch_graph(monkeypatch, {"FN": [
        {"line_start": 24, "line_end": 24},   # collapsed -> 24
        {"line_start": 10, "line_end": 60},   # multi-line -> excluded
        {"line_start": 88, "line_end": 88},   # collapsed -> 88
    ]})
    result = _w57_collapsed_megaline_lines(
        {"FN": {"schema": "OFSERM"}}, _FAKE_REDIS,
    )
    assert result == {24, 88}, result


def test_helper_skips_entry_without_schema(monkeypatch):
    """A multi_source entry with no schema cannot resolve a graph -> skipped,
    no collapsed lines contributed (no crash)."""
    _patch_graph(monkeypatch, {"FN": [{"line_start": 24, "line_end": 24}]})
    result = _w57_collapsed_megaline_lines(
        {"FN": {"source_code": _src(30)}}, _FAKE_REDIS,  # no "schema"
    )
    assert result == set(), result


def test_helper_unreachable_graph_returns_empty(monkeypatch):
    """When get_function_graph returns None (graph not indexed) the helper
    contributes nothing rather than raising."""
    _patch_graph(monkeypatch, {})  # every lookup -> None
    result = _w57_collapsed_megaline_lines(
        {"FN": {"schema": "OFSERM"}}, _FAKE_REDIS,
    )
    assert result == set(), result
