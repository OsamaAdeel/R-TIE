"""Phase 8 — DELETE-block parser classification.

Pre-Phase-8, DELETE blocks were dispatched to ``build_update_node`` and
emerged with ``type="UPDATE"`` and an empty ``column_maps``. Downstream
consumers (Phase 2 ``proof_builder``, ``query_templates``) saw them as
no-op updates. Phase 8 adds a dedicated ``build_delete_node`` so DELETE
blocks produce correctly-typed nodes with ``target_table``, parsed
``conditions``, the line range, and the ``committed_after`` flag.

These tests exercise:
  - The fixed builder produces ``type="DELETE"`` (not "UPDATE") for a
    DELETE block, and the node carries the four required fields.
  - INSERT, UPDATE, MERGE classifications are unchanged (regression).
  - The dispatch table routes DELETE to ``build_delete_node``, not back
    to ``build_update_node`` (regression guard).
  - End-to-end against the corpus: ``TLX_LOAD_DELETE_OFSMDM.sql`` (the
    DELETE-heaviest function in the loader corpus) produces 7 DELETE
    nodes, all correctly typed.
"""

from __future__ import annotations

import os

import pytest

from src.parsing.builder import (
    _BUILDER_DISPATCH,
    build_delete_node,
    build_function_graph,
    build_node,
    build_update_node,
)


# ---------------------------------------------------------------------------
# DELETE node shape
# ---------------------------------------------------------------------------


_DELETE_SOURCE = [
    "BEGIN\n",
    "  DELETE FROM STG_PRODUCT_PROCESSOR\n",
    "        WHERE     fic_mis_date = ld_mis_date\n",
    "              AND V_LV_CODE IN ('ABL', 'AMC');\n",
    "  COMMIT;\n",
    "END;\n",
]


def test_delete_block_emits_type_delete_not_update():
    """The headline regression: DELETE → type='DELETE', not 'UPDATE'."""
    graph = build_function_graph(
        source_lines=_DELETE_SOURCE,
        function_name="FN_TEST_DELETE",
        file_name="FN_TEST_DELETE.sql",
        schema="OFSMDM",
    )
    types = [n["type"] for n in graph["nodes"]]
    assert "DELETE" in types
    assert "UPDATE" not in types


def test_delete_node_carries_required_fields():
    """A DELETE node must carry target_table, conditions, line range,
    and committed_after — the four fields the W35 Phase 8 prompt
    enumerated."""
    graph = build_function_graph(
        source_lines=_DELETE_SOURCE,
        function_name="FN_TEST_DELETE",
        file_name="FN_TEST_DELETE.sql",
        schema="OFSMDM",
    )
    deletes = [n for n in graph["nodes"] if n["type"] == "DELETE"]
    assert len(deletes) == 1
    node = deletes[0]

    assert node["target_table"] == "STG_PRODUCT_PROCESSOR"
    # Multi-clause WHERE produced parsed condition entries.
    assert len(node["conditions"]) >= 1
    # Line range covers the DELETE block (1-based).
    assert isinstance(node["line_start"], int)
    assert isinstance(node["line_end"], int)
    assert node["line_start"] < node["line_end"]
    # COMMIT immediately after → committed_after = True.
    assert node["committed_after"] is True
    # Summary is rendered.
    assert "STG_PRODUCT_PROCESSOR" in node["summary"]


def test_delete_node_has_no_column_maps():
    """DELETE has no SET clause; the node intentionally omits
    column_maps / calculation / overrides (the UPDATE-shape fields)."""
    graph = build_function_graph(
        source_lines=_DELETE_SOURCE,
        function_name="FN_TEST_DELETE",
        file_name="FN_TEST_DELETE.sql",
        schema="OFSMDM",
    )
    delete = next(n for n in graph["nodes"] if n["type"] == "DELETE")
    assert "column_maps" not in delete
    assert "calculation" not in delete
    assert "overrides" not in delete


def test_delete_without_following_commit_marks_committed_after_false():
    src = [
        "BEGIN\n",
        "  DELETE FROM STG_OPS_RISK_DATA\n",
        "        WHERE fic_mis_date = ld_mis_date;\n",
        "  -- no COMMIT here\n",
        "  INSERT INTO STG_OPS_RISK_DATA (FIC_MIS_DATE) VALUES (ld_mis_date);\n",
        "END;\n",
    ]
    graph = build_function_graph(src, "FN_X", "x.sql", "OFSMDM")
    delete = next(n for n in graph["nodes"] if n["type"] == "DELETE")
    assert delete["committed_after"] is False


# ---------------------------------------------------------------------------
# Dispatch-table regression guards
# ---------------------------------------------------------------------------


def test_dispatch_maps_delete_to_build_delete_node():
    """Pre-Phase-8 _BUILDER_DISPATCH['DELETE'] was build_update_node;
    Phase 8 fixed it. This regression guards against a revert."""
    assert _BUILDER_DISPATCH["DELETE"] is build_delete_node
    assert _BUILDER_DISPATCH["DELETE"] is not build_update_node


def test_dispatch_keeps_other_dml_unchanged():
    """INSERT / UPDATE / MERGE dispatch must not have been disturbed."""
    from src.parsing.builder import (
        build_insert_node,
        build_merge_node,
    )
    assert _BUILDER_DISPATCH["INSERT"] is build_insert_node
    assert _BUILDER_DISPATCH["UPDATE"] is build_update_node
    assert _BUILDER_DISPATCH["MERGE"] is build_merge_node


# ---------------------------------------------------------------------------
# Cross-DML regressions: INSERT / UPDATE / MERGE classifications unchanged
# ---------------------------------------------------------------------------


def test_insert_block_still_emits_type_insert():
    src = [
        "BEGIN\n",
        "  INSERT INTO STG_TMP (COL1, COL2)\n",
        "    SELECT A, B FROM SRC_TABLE;\n",
        "  COMMIT;\n",
        "END;\n",
    ]
    graph = build_function_graph(src, "FN_INS", "x.sql", "OFSMDM")
    types = [n["type"] for n in graph["nodes"]]
    assert "INSERT" in types


def test_update_block_still_emits_type_update():
    src = [
        "BEGIN\n",
        "  UPDATE STG_TMP\n",
        "     SET COL1 = 'X'\n",
        "   WHERE FIC_MIS_DATE = ld_mis_date;\n",
        "  COMMIT;\n",
        "END;\n",
    ]
    graph = build_function_graph(src, "FN_UPD", "x.sql", "OFSMDM")
    types = [n["type"] for n in graph["nodes"]]
    assert "UPDATE" in types
    upd = next(n for n in graph["nodes"] if n["type"] == "UPDATE")
    # UPDATE-shape fields must still be present.
    assert "column_maps" in upd
    assert "calculation" in upd


def test_merge_block_still_emits_type_merge():
    src = [
        "BEGIN\n",
        "  MERGE INTO STG_TGT t\n",
        "  USING (SELECT * FROM STG_SRC) s\n",
        "     ON (t.id = s.id)\n",
        "  WHEN MATCHED THEN UPDATE SET t.val = s.val\n",
        "  WHEN NOT MATCHED THEN INSERT (id, val) VALUES (s.id, s.val);\n",
        "  COMMIT;\n",
        "END;\n",
    ]
    graph = build_function_graph(src, "FN_MRG", "x.sql", "OFSMDM")
    types = [n["type"] for n in graph["nodes"]]
    assert "MERGE" in types


# ---------------------------------------------------------------------------
# End-to-end: real corpus function with 7 DELETE blocks
# ---------------------------------------------------------------------------


_CORPUS_DELETE_SQL = os.path.join(
    os.path.dirname(__file__),
    "..", "..", "..",
    "db", "modules", "OFSDMINFO_ABL_DATA_PREPARATION", "functions",
    "TLX_LOAD_DELETE_OFSMDM.sql",
)


@pytest.mark.skipif(
    not os.path.exists(_CORPUS_DELETE_SQL),
    reason="corpus function not present on this checkout",
)
def test_corpus_tlx_load_delete_emits_seven_delete_nodes():
    """``TLX_LOAD_DELETE_OFSMDM`` is the DELETE-heaviest corpus function.
    It contains exactly 7 DELETE blocks (no other DML) — every one must
    classify as type='DELETE' under Phase 8."""
    with open(_CORPUS_DELETE_SQL, "r", encoding="utf-8") as f:
        source_lines = f.readlines()
    graph = build_function_graph(
        source_lines=source_lines,
        function_name="TLX_LOAD_DELETE_OFSMDM",
        file_name="TLX_LOAD_DELETE_OFSMDM.sql",
        schema="OFSMDM",
    )

    delete_count = sum(1 for n in graph["nodes"] if n["type"] == "DELETE")
    update_count = sum(1 for n in graph["nodes"] if n["type"] == "UPDATE")
    assert delete_count == 7, (
        f"expected 7 DELETE nodes, got {delete_count}; "
        f"types seen: {[n['type'] for n in graph['nodes']]}"
    )
    # Pre-Phase-8 every one of these would have been UPDATE.
    assert update_count == 0
