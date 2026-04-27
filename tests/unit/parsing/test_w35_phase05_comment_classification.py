"""
W35 Phase 0.5 — Tests for the comment-classification fix (Bug B1).

The OFSAA execution-log extraction collapses each DML to a single megaline
that embeds an Oracle optimizer hint (``/*+ PARALLEL(4) */`` or similar).
Pre-fix, the parser's ``_build_comment_map`` flagged any line containing
``/*`` as commented — even when ``*/`` closed on the same line — which
routed every OFSAA-wrapped MERGE/INSERT to ``commented_out_nodes``.

These tests pin down:

  - ``_build_comment_map`` correctly distinguishes inline self-closing
    comments from multi-line block-comment regions (single inline hint,
    two inline hints on one line, multi-line block comment, mixed).
  - End-to-end via ``build_function_graph`` — OFSAA megaline DMLs land
    in ``nodes``, OFSMDM patterns are preserved, multi-line commented
    DMLs stay out of ``nodes``.

The companion fix for Bug B2 (committed_after wiring) is covered by
tests added in the B2 commit.
"""

from src.parsing.parser import (
    _build_comment_map,
    parse_function,
)
from src.parsing.builder import build_function_graph


# ---------------------------------------------------------------------
# B1 — _build_comment_map edge cases
# ---------------------------------------------------------------------

def test_single_inline_hint_does_not_flag_line():
    """A line with one self-closing /*+ HINT */ must be False in the map.

    This is the dominant OFSAA-megaline shape: MERGE/INSERT keyword and
    /*+ PARALLEL(4) */ on the same line. Pre-fix, the line was True.
    """
    lines = [
        "BEGIN\n",
        "  MERGE INTO FCT_X TT USING (SELECT /*+ PARALLEL(4) */ a FROM b);\n",
        "  COMMIT;\n",
        "END;\n",
    ]
    cmap = _build_comment_map(lines)
    assert cmap == [False, False, False, False], (
        "Inline self-closing /*+ HINT */ must not flag the line as commented"
    )


def test_two_inline_hints_on_same_line_does_not_flag_line():
    """Two self-closing /* */ on one line — both must be consumed.

    The fix walks the line consuming closed pairs; a second /* later in the
    line must NOT trip "starts_multiline_block" if it also closes.
    """
    lines = [
        "BEGIN\n",
        "  MERGE /*+ FIRST_ROWS */ INTO X USING (SELECT /*+ PARALLEL(4) */ a FROM b);\n",
        "  COMMIT;\n",
        "END;\n",
    ]
    cmap = _build_comment_map(lines)
    assert cmap == [False, False, False, False], (
        "Two inline self-closing comments on one line must not flag the line"
    )


def test_multi_line_block_comment_still_flags_inner_lines():
    """A genuine multi-line /* ... */ region must still be flagged True
    on every line it covers, including the opener and the closer line.

    Guards against an over-correction that would ignore all /* markers.
    """
    lines = [
        "BEGIN\n",
        "  /* TODO retire this:\n",        # opener, no closer on this line
        "     MERGE INTO X USING ...;\n",   # inside the block
        "     COMMIT;\n",                   # inside the block
        "  */\n",                            # closer
        "  INSERT INTO Y VALUES (1);\n",    # back to normal code
        "  COMMIT;\n",
        "END;\n",
    ]
    cmap = _build_comment_map(lines)
    expected = [False, True, True, True, True, False, False, False]
    assert cmap == expected, (
        f"Multi-line block comment tracking broken: got {cmap}, expected {expected}"
    )


def test_inline_hint_followed_by_real_block_comment_on_next_line():
    """Belt-and-braces: inline hint on one line, multi-line block opens
    on the next line. The first line must be False, subsequent lines True
    until the closer.
    """
    lines = [
        "MERGE INTO X (/*+ APPEND */ a, b) VALUES (1, 2);\n",  # inline hint
        "/* opening block\n",                                    # opener
        "   still in block\n",                                   # inside
        "*/\n",                                                  # closer
        "COMMIT;\n",                                             # normal
    ]
    cmap = _build_comment_map(lines)
    assert cmap == [False, True, True, True, False]


# ---------------------------------------------------------------------
# B1 — end-to-end through build_function_graph
# ---------------------------------------------------------------------

def test_ofsaa_megaline_merge_lands_in_nodes():
    """OFSAA-style megaline (MERGE + /*+ PARALLEL(4) */ on one line) must
    produce a node in `nodes`, not in `commented_out_nodes`."""
    lines = [
        "CREATE OR REPLACE FUNCTION OFSERM.FN_TEST_OFSAA RETURN VARCHAR2 AS\n",
        "BEGIN\n",
        "    MERGE INTO FCT_STANDARD_ACCT_HEAD TT USING (SELECT /*+ PARALLEL(4) */ a, b FROM DIM_X) SS ON (TT.A = SS.a) WHEN MATCHED THEN UPDATE SET TT.B = SS.b;\n",
        "    COMMIT;\n",
        "    RETURN 'OK';\n",
        "EXCEPTION\n",
        "    WHEN OTHERS THEN ROLLBACK; RETURN 'FAIL';\n",
        "END;\n",
    ]
    graph = build_function_graph(lines, "FN_TEST_OFSAA", "test.sql", "OFSERM")
    assert len(graph["nodes"]) == 1, (
        f"Expected 1 node, got {len(graph['nodes'])} (commented={len(graph['commented_out_nodes'])})"
    )
    assert len(graph["commented_out_nodes"]) == 0
    node = graph["nodes"][0]
    assert node["type"] == "MERGE"
    assert node["target_table"] == "FCT_STANDARD_ACCT_HEAD"


def test_two_ofsaa_megalines_same_function():
    """Two megaline DMLs in one function — both must land in nodes."""
    lines = [
        "CREATE OR REPLACE FUNCTION OFSERM.FN_TEST_TWO RETURN VARCHAR2 AS\n",
        "BEGIN\n",
        "    INSERT /*+ APPEND */ INTO FSI_X (a, b) SELECT /*+ PARALLEL(4) */ x, y FROM SRC1;\n",
        "    UPDATE FSI_Y SET a = (SELECT /*+ FIRST_ROWS */ MAX(b) FROM SRC2);\n",
        "    COMMIT;\n",
        "    RETURN 'OK';\n",
        "END;\n",
    ]
    graph = build_function_graph(lines, "FN_TEST_TWO", "test.sql", "OFSERM")
    assert len(graph["nodes"]) == 2, (
        f"Expected 2 nodes, got {len(graph['nodes'])}"
    )
    types = sorted(n["type"] for n in graph["nodes"])
    assert types == ["INSERT", "UPDATE"]


def test_multiline_commented_out_dml_does_not_land_in_nodes():
    """If a MERGE is fully wrapped in a multi-line block comment, it must
    NOT appear in `nodes`. Whether it appears in `commented_out_nodes` is
    acceptable either way — the parser may not detect a DML keyword in the
    cleaned (comment-stripped) line."""
    lines = [
        "CREATE OR REPLACE FUNCTION OFSERM.FN_TEST_RETIRED RETURN VARCHAR2 AS\n",
        "BEGIN\n",
        "    /*\n",
        "    MERGE INTO X USING (SELECT a FROM b) ON (1=1) WHEN MATCHED THEN UPDATE SET y = z;\n",
        "    COMMIT;\n",
        "    */\n",
        "    INSERT INTO ACTIVE_T VALUES (1);\n",
        "    COMMIT;\n",
        "    RETURN 'OK';\n",
        "END;\n",
    ]
    graph = build_function_graph(lines, "FN_TEST_RETIRED", "test.sql", "OFSERM")
    # The retired MERGE must not appear in nodes; only the live INSERT.
    node_targets = {n.get("target_table") for n in graph["nodes"]}
    assert "X" not in node_targets, (
        f"Commented-out MERGE leaked into nodes: targets={node_targets}"
    )
    assert "ACTIVE_T" in node_targets, (
        f"Live INSERT missing from nodes: targets={node_targets}"
    )


def test_ofsmdm_multiline_pattern_preserved():
    """An OFSMDM-style multi-line DML with separate COMMIT keeps landing
    in `nodes`. Three DML blocks (DELETE, INSERT, UPDATE) must produce
    three nodes with the expected target tables.

    Note: the builder routes DELETE through build_update_node and emits
    type='UPDATE' for both UPDATE and DELETE blocks — that's a
    pre-existing design choice. We assert via target_table instead.
    """
    lines = [
        "CREATE OR REPLACE FUNCTION OFSMDM.FN_TEST_MDM RETURN VARCHAR2 AS\n",
        "BEGIN\n",
        "    DELETE FROM STG_OPS_RISK_DATA WHERE FIC_MIS_DATE = CQD;\n",
        "    COMMIT;\n",
        "    INSERT INTO STG_OPS_RISK_DATA (a, b)\n",
        "        SELECT a, b FROM ABL_OPS_RISK_DATA;\n",
        "    COMMIT;\n",
        "    UPDATE STG_OPS_TARGET_X SET v = 1 WHERE x = 'CBA';\n",
        "    COMMIT;\n",
        "    RETURN 'OK';\n",
        "END;\n",
    ]
    graph = build_function_graph(lines, "FN_TEST_MDM", "test.sql", "OFSMDM")
    targets = {n.get("target_table") for n in graph["nodes"]}
    assert "STG_OPS_RISK_DATA" in targets, (
        f"OFSMDM multi-line DELETE/INSERT lost: targets={targets}"
    )
    assert "STG_OPS_TARGET_X" in targets, (
        f"OFSMDM multi-line UPDATE lost: targets={targets}"
    )
    assert len(graph["nodes"]) == 3, (
        f"Expected 3 nodes (DELETE+INSERT+UPDATE), got {len(graph['nodes'])}"
    )
    assert len(graph["commented_out_nodes"]) == 0


# ---------------------------------------------------------------------
# B2 — committed_after wiring
# (added in the B2 commit; tests will fail against the parser-only B1
#  state because the builder field-name mismatch hasn't been fixed yet)
# ---------------------------------------------------------------------
