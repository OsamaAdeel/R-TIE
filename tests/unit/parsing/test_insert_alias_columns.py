"""Alias-qualified INSERT column lists (parser fix; W-number TBD).

`_extract_insert_columns` previously (a) failed to match
``INSERT INTO TBL <alias> (...)`` because the regex allowed no token between
the table name and the column-list paren, and (b) did not strip an
``alias.`` prefix from extracted column names. Both meant any column written
ONLY via an alias-qualified INSERT (e.g. ``POPULATE_PP_FROMGL`` writing
``N_EOP_BAL`` via ``INSERT INTO STG_PRODUCT_PROCESSOR B (B.N_EOP_BAL, ...)``)
was invisible to the entire column index.

This fix mirrors ``_extract_update_set``'s existing alias-strip. The
``extract_column_maps`` return shape is unchanged — it is fed correct columns.
"""

from __future__ import annotations

import pytest

from src.parsing.parser import _extract_insert_columns, extract_column_maps


class TestInsertAliasColumns:
    def test_alias_qualified_columns_extracted(self):
        cols, _ = _extract_insert_columns(
            "INSERT INTO STG_PRODUCT_PROCESSOR B (B.FIC_MIS_DATE, B.N_EOP_BAL) "
            "SELECT A.C1, A.C2 FROM STG_GL_DATA A"
        )
        assert cols == ["FIC_MIS_DATE", "N_EOP_BAL"]

    def test_unqualified_still_works(self):
        # Regression guard: the pre-fix happy path is unchanged.
        cols, _ = _extract_insert_columns(
            "INSERT INTO T (C1, C2) SELECT C1, C2 FROM X"
        )
        assert cols == ["C1", "C2"]

    def test_mixed_qualified_and_unqualified(self):
        cols, _ = _extract_insert_columns(
            "INSERT INTO T B (B.C1, C2, B.C3) SELECT 1, 2, 3 FROM X"
        )
        assert cols == ["C1", "C2", "C3"]

    def test_different_alias_tokens_stripped(self):
        cols, _ = _extract_insert_columns(
            "INSERT INTO T TGT (TGT.C1, TGT.C2) SELECT 1, 2 FROM X"
        )
        assert cols == ["C1", "C2"]

    def test_exact_name_guard_no_truncation(self):
        # The whole column name must survive — NOT 'EOP', NOT 'N_EOP'.
        cols, _ = _extract_insert_columns(
            "INSERT INTO T B (B.N_EOP_BAL) SELECT 1 FROM X"
        )
        assert cols == ["N_EOP_BAL"]

    def test_alias_without_space_before_paren(self):
        cols, _ = _extract_insert_columns("INSERT INTO T B(B.C1) SELECT 1 FROM X")
        assert cols == ["C1"]

    def test_malformed_no_column_list_returns_empty(self):
        # Controlled failure: no closing paren / no list → [], no exception.
        cols, _ = _extract_insert_columns("INSERT INTO T B (")
        assert cols == []

    def test_values_without_column_list_not_treated_as_columns(self):
        # "INSERT INTO T VALUES (1,2,3)" has no column list — the VALUES
        # keyword must NOT be mistaken for a table alias (regression the
        # alias-widening introduced and the negative lookahead fixes).
        cols, _ = _extract_insert_columns("INSERT INTO T VALUES (1, 2, 3)")
        assert cols == []

    def test_values_keyword_case_insensitive(self):
        cols, _ = _extract_insert_columns("insert into t values (1, 2, 3)")
        assert cols == []

    def test_column_list_then_values_still_extracted(self):
        cols, _ = _extract_insert_columns("INSERT INTO T (C1, C2) VALUES (1, 2)")
        assert cols == ["C1", "C2"]

    def test_extract_column_maps_shape_unchanged(self):
        # The contract of extract_column_maps is untouched — it now just
        # receives correct columns. mapping pairs target<-source positionally.
        cm = extract_column_maps(
            ["INSERT INTO STG_PRODUCT_PROCESSOR B (B.FIC_MIS_DATE, B.N_EOP_BAL)",
             "SELECT A.FIC_MIS_DATE, A.N_AMOUNT_LCY FROM STG_GL_DATA A"],
            "INSERT",
        )
        assert set(cm.keys()) == {"columns", "values", "mapping"}
        assert cm["columns"] == ["FIC_MIS_DATE", "N_EOP_BAL"]
        assert cm["mapping"]["N_EOP_BAL"] == "A.N_AMOUNT_LCY"


class TestMergeInsertBranch:
    """Stop-and-report finding (documented as a test).

    A MERGE ``WHEN NOT MATCHED THEN INSERT (cols) VALUES (...)`` branch does
    NOT route through ``_extract_insert_columns`` — ``extract_column_maps``
    dispatches block_type 'MERGE' to ``_extract_update_set`` (the SET path),
    which finds no SET clause in an INSERT...VALUES fragment and returns no
    assignments. So MERGE-insert column lists remain uncaptured. That is a
    SEPARATE pre-existing gap, out of scope for this fix (reported, not fixed).

    This test pins both facts: (1) the standalone extractor handles a
    MERGE-insert fragment correctly should it ever be routed here, and
    (2) the current MERGE dispatch yields assignments, not insert columns.
    """

    def test_standalone_extractor_handles_merge_insert_fragment(self):
        cols, _ = _extract_insert_columns(
            "WHEN NOT MATCHED THEN INSERT B (B.C1, B.C2) VALUES (S.C1, S.C2)"
        )
        # The leading "WHEN NOT MATCHED THEN " precedes INSERT; the regex
        # anchors on INSERT INTO — without INTO this fragment yields no
        # match, documenting that MERGE-insert needs its own handling.
        assert cols == []

    def test_merge_dispatch_uses_update_set_not_insert(self):
        cm = extract_column_maps(
            ["MERGE INTO T USING S ON (T.ID = S.ID)",
             "WHEN NOT MATCHED THEN INSERT (C1, C2) VALUES (S.C1, S.C2)"],
            "MERGE",
        )
        # MERGE returns the UPDATE-style {"assignments": [...]} shape, NOT
        # {"columns": [...]} — confirms the INSERT branch is not consulted.
        assert "assignments" in cm
        assert "columns" not in cm
