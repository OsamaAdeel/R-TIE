"""Unit tests for W35 Phase 6 — derivation extraction.

Covers:
  - Pattern A (SUBTRACT) detection on the canonical CS_Deferred_Tax fixture.
  - Pattern B (DIRECT_ASSIGN) detection on a single-MAX-CASE-WHEN fixture.
  - Negative cases: no MERGE block, no derivation pattern, missing
    UPDATE clause, COALESCE/GREATEST wrappers (deferred shapes).
  - Multi-target MERGE: multiple flag/literal pairs in one COND, one
    derivation per clean EXP.
  - Cross-referencing into the literal index.
"""

from __future__ import annotations

import pytest

from src.parsing.derivations import (
    OP_DIRECT_ASSIGN,
    OP_SUBTRACT,
    attach_derivations_to_literal_index,
    extract_derivations,
)
from src.parsing.literals import (
    DEFAULT_BUSINESS_IDENTIFIER_PATTERNS,
    compile_patterns,
)


@pytest.fixture
def patterns():
    return compile_patterns(DEFAULT_BUSINESS_IDENTIFIER_PATTERNS)


# ---------------------------------------------------------------------------
# Canonical Pattern A — MAX(CASE WHEN A) - MAX(CASE WHEN B)
# ---------------------------------------------------------------------------

# Distilled from CS_Deferred_Tax_Asset_Net_of_DTL_Calculation. The exact
# whitespace and join shape vary across the corpus; this fixture preserves
# only the structural elements the extractor cares about.
_PATTERN_A_FIXTURE = """\
CREATE OR REPLACE FUNCTION OFSERM.PATTERN_A_FN RETURN VARCHAR2 AS
BEGIN
    MERGE INTO FCT_STANDARD_ACCT_HEAD TT USING (
      SELECT TT.N_RUN_SKEY,
        MIN(CASE WHEN ((DIM.V_STD_ACCT_HEAD_ID = 'CAP943')) THEN 10 ELSE 11 END) AS COND_111_10,
        (MAX(CASE WHEN CAPITAL_ACCOUNTING.n_std_acct_head_skey IN
              (SELECT n_std_acct_head_skey FROM DIM WHERE v_std_acct_head_id = 'CAP309')
              THEN CAPITAL_ACCOUNTING.n_std_acct_head_amt ELSE NULL END )
         - MAX(CASE WHEN CAPITAL_ACCOUNTING.n_std_acct_head_skey IN
              (SELECT n_std_acct_head_skey FROM DIM WHERE v_std_acct_head_id = 'CAP863')
              THEN CAPITAL_ACCOUNTING.n_std_acct_head_amt ELSE NULL END )) AS EXP_111_10,
        MIN(FCT_STANDARD_ACCT_HEAD.N_STD_ACCT_HEAD_AMT) AS EXP_111_11
      FROM FCT_STANDARD_ACCT_HEAD
      WHERE ((DIM.V_STD_ACCT_HEAD_ID = 'CAP943'))
    ) SS ON (TT.id = SS.id)
    WHEN MATCHED THEN UPDATE SET
      TT.N_STD_ACCT_HEAD_AMT = CASE WHEN COND_111_10=10 THEN EXP_111_10 ELSE EXP_111_11 END;
    COMMIT;
    RETURN 'OK';
END;
/
"""


class TestPatternA:
    def test_extracts_one_subtract_derivation(self, patterns):
        derivs = extract_derivations(
            _PATTERN_A_FIXTURE.splitlines(keepends=True),
            "PATTERN_A_FN",
            patterns,
        )
        assert len(derivs) == 1
        d = derivs[0]
        assert d["target_literal"] == "CAP943"
        assert d["target_column"] == "N_STD_ACCT_HEAD_AMT"
        assert d["operation"] == OP_SUBTRACT
        assert d["source_literals"] == ["CAP309", "CAP863"]
        assert [op["literal"] for op in d["operands"]] == ["CAP309", "CAP863"]
        # Both operands reference the same amount column in this fixture.
        for op in d["operands"]:
            assert op["amount_column"].endswith("n_std_acct_head_amt")

    def test_includes_function_name_and_line_range(self, patterns):
        derivs = extract_derivations(
            _PATTERN_A_FIXTURE.splitlines(keepends=True),
            "PATTERN_A_FN",
            patterns,
        )
        d = derivs[0]
        assert d["function"] == "PATTERN_A_FN"
        # MERGE statement starts on line 4 in this fixture (1-based, 1=
        # "CREATE OR REPLACE...", 2="BEGIN", 3="    MERGE INTO ...").
        assert d["line_range"][0] >= 3
        assert d["line_range"][1] >= d["line_range"][0]

    def test_emits_no_derivations_when_patterns_empty(self):
        derivs = extract_derivations(
            _PATTERN_A_FIXTURE.splitlines(keepends=True),
            "PATTERN_A_FN",
            [],
        )
        assert derivs == []


# ---------------------------------------------------------------------------
# Canonical Pattern B — single MAX(CASE WHEN A)
# ---------------------------------------------------------------------------

_PATTERN_B_FIXTURE = """\
CREATE OR REPLACE FUNCTION OFSERM.PATTERN_B_FN RETURN VARCHAR2 AS
BEGIN
    MERGE INTO FCT_STANDARD_ACCT_HEAD TT USING (
      SELECT TT.N_RUN_SKEY,
        MIN(CASE WHEN ((DIM.V_STD_ACCT_HEAD_ID = 'CAP577')) THEN 10 ELSE 11 END) AS COND_222_10,
        MAX(CASE WHEN CAPITAL_ACCOUNTING.n_std_acct_head_skey IN
            (SELECT n_std_acct_head_skey FROM DIM WHERE v_std_acct_head_id = 'CAP577')
            THEN CAPITAL_ACCOUNTING.n_std_acct_head_amt ELSE NULL END) AS EXP_222_10,
        MIN(FCT_STANDARD_ACCT_HEAD.N_STD_ACCT_HEAD_AMT) AS EXP_222_11
      FROM FCT_STANDARD_ACCT_HEAD
      WHERE ((DIM.V_STD_ACCT_HEAD_ID = 'CAP577'))
    ) SS ON (TT.id = SS.id)
    WHEN MATCHED THEN UPDATE SET
      TT.N_STD_ACCT_HEAD_AMT = CASE WHEN COND_222_10=10 THEN EXP_222_10 ELSE EXP_222_11 END;
    COMMIT;
    RETURN 'OK';
END;
/
"""


class TestPatternB:
    def test_extracts_direct_assign_derivation(self, patterns):
        derivs = extract_derivations(
            _PATTERN_B_FIXTURE.splitlines(keepends=True),
            "PATTERN_B_FN",
            patterns,
        )
        assert len(derivs) == 1
        d = derivs[0]
        assert d["target_literal"] == "CAP577"
        assert d["operation"] == OP_DIRECT_ASSIGN
        assert d["source_literals"] == ["CAP577"]
        assert len(d["operands"]) == 1
        assert d["operands"][0]["literal"] == "CAP577"


# ---------------------------------------------------------------------------
# Multi-target: COND with multiple WHEN branches
# ---------------------------------------------------------------------------

_MULTI_TARGET_FIXTURE = """\
CREATE OR REPLACE FUNCTION OFSERM.MULTI_TARGET_FN RETURN VARCHAR2 AS
BEGIN
    MERGE INTO FCT_STANDARD_ACCT_HEAD TT USING (
      SELECT TT.N_RUN_SKEY,
        MIN(CASE WHEN ((DIM.V_STD_ACCT_HEAD_ID = 'CAP100')) THEN 10
                 WHEN ((DIM.V_STD_ACCT_HEAD_ID = 'CAP200')) THEN 11
                 ELSE 12 END) AS COND_333_10,
        MAX(CASE WHEN x IN (SELECT v FROM DIM WHERE v_std_acct_head_id = 'CAP301')
            THEN CAPITAL.n_amt ELSE NULL END) AS EXP_333_10,
        (MAX(CASE WHEN x IN (SELECT v FROM DIM WHERE v_std_acct_head_id = 'CAP401')
              THEN CAPITAL.n_amt ELSE NULL END)
         - MAX(CASE WHEN x IN (SELECT v FROM DIM WHERE v_std_acct_head_id = 'CAP402')
              THEN CAPITAL.n_amt ELSE NULL END)) AS EXP_333_11,
        MIN(FCT_STANDARD_ACCT_HEAD.N_STD_ACCT_HEAD_AMT) AS EXP_333_12
      FROM FCT_STANDARD_ACCT_HEAD
      WHERE 1=1
    ) SS ON (TT.id = SS.id)
    WHEN MATCHED THEN UPDATE SET
      TT.N_STD_ACCT_HEAD_AMT = CASE
        WHEN COND_333_10=10 THEN EXP_333_10
        WHEN COND_333_10=11 THEN EXP_333_11
        ELSE EXP_333_12 END;
    RETURN 'OK';
END;
/
"""


class TestMultiTarget:
    def test_emits_one_derivation_per_clean_branch(self, patterns):
        derivs = extract_derivations(
            _MULTI_TARGET_FIXTURE.splitlines(keepends=True),
            "MULTI_TARGET_FN",
            patterns,
        )
        # The else branch routes to EXP_333_12 = MIN(...) which is not
        # Pattern A or B, so it's correctly not extracted. Two clean
        # branches: CAP100 = CAP301 (DIRECT_ASSIGN), CAP200 = CAP401 - CAP402
        # (SUBTRACT).
        targets = {d["target_literal"]: d for d in derivs}
        assert set(targets.keys()) == {"CAP100", "CAP200"}
        assert targets["CAP100"]["operation"] == OP_DIRECT_ASSIGN
        assert targets["CAP100"]["source_literals"] == ["CAP301"]
        assert targets["CAP200"]["operation"] == OP_SUBTRACT
        assert targets["CAP200"]["source_literals"] == ["CAP401", "CAP402"]


# ---------------------------------------------------------------------------
# Negative cases — extractor must return [] without crashing
# ---------------------------------------------------------------------------

class TestNegativeCases:
    def test_function_with_no_merge_returns_empty(self, patterns):
        text = """\
CREATE OR REPLACE FUNCTION OFSERM.NO_MERGE_FN RETURN VARCHAR2 AS
BEGIN
    INSERT INTO TGT (col)
      SELECT x FROM SRC WHERE V_STD_ACCT_HEAD_ID IN ('CAP100', 'CAP200');
    RETURN 'OK';
END;
/
"""
        assert extract_derivations(
            text.splitlines(keepends=True), "NO_MERGE_FN", patterns,
        ) == []

    def test_merge_without_when_matched_returns_empty(self, patterns):
        # MERGE with only WHEN NOT MATCHED — no UPDATE SET routing.
        text = """\
CREATE OR REPLACE FUNCTION OFSERM.NOT_MATCHED_FN RETURN VARCHAR2 AS
BEGIN
    MERGE INTO TGT TT USING (
      SELECT MIN(CASE WHEN ((D.V_STD_ACCT_HEAD_ID = 'CAP943')) THEN 10 ELSE 11 END) AS COND_444_10,
        MAX(CASE WHEN x IN (SELECT v FROM D WHERE v_std_acct_head_id = 'CAP309')
            THEN n_amt ELSE NULL END) AS EXP_444_10
      FROM SRC
    ) SS ON (TT.id = SS.id)
    WHEN NOT MATCHED THEN INSERT (id) VALUES (1);
    RETURN 'OK';
END;
/
"""
        assert extract_derivations(
            text.splitlines(keepends=True), "NOT_MATCHED_FN", patterns,
        ) == []

    def test_merge_with_no_cond_returns_empty(self, patterns):
        # MERGE has EXP and a WHEN MATCHED, but no COND alias — incomplete
        # template; the extractor refuses to guess.
        text = """\
CREATE OR REPLACE FUNCTION OFSERM.NO_COND_FN RETURN VARCHAR2 AS
BEGIN
    MERGE INTO TGT TT USING (
      SELECT MAX(CASE WHEN x IN (SELECT v FROM D WHERE v_std_acct_head_id = 'CAP309')
            THEN n_amt ELSE NULL END) AS EXP_444_10
      FROM SRC
    ) SS ON (TT.id = SS.id)
    WHEN MATCHED THEN UPDATE SET TT.col = EXP_444_10;
    RETURN 'OK';
END;
/
"""
        assert extract_derivations(
            text.splitlines(keepends=True), "NO_COND_FN", patterns,
        ) == []

    def test_coalesce_wrapped_max_is_skipped(self, patterns):
        # COALESCE(MAX(CASE WHEN ...), 0) wraps the canonical operand.
        # We deliberately reject this — Phase 7 surfacing must be
        # high-confidence, so wrapped shapes are observed-but-deferred.
        text = """\
CREATE OR REPLACE FUNCTION OFSERM.COALESCE_FN RETURN VARCHAR2 AS
BEGIN
    MERGE INTO TGT TT USING (
      SELECT MIN(CASE WHEN ((D.V_STD_ACCT_HEAD_ID = 'CAP943')) THEN 10 ELSE 11 END) AS COND_555_10,
        (COALESCE(MAX(CASE WHEN x IN (SELECT v FROM D WHERE v_std_acct_head_id = 'CAP309')
            THEN n_amt ELSE NULL END), 0)
         - COALESCE(MAX(CASE WHEN x IN (SELECT v FROM D WHERE v_std_acct_head_id = 'CAP863')
            THEN n_amt ELSE NULL END), 0)) AS EXP_555_10,
        MIN(N_AMT) AS EXP_555_11
      FROM SRC
    ) SS ON (TT.id = SS.id)
    WHEN MATCHED THEN UPDATE SET
      TT.col = CASE WHEN COND_555_10=10 THEN EXP_555_10 ELSE EXP_555_11 END;
    RETURN 'OK';
END;
/
"""
        assert extract_derivations(
            text.splitlines(keepends=True), "COALESCE_FN", patterns,
        ) == []

    def test_three_term_subtract_is_skipped(self, patterns):
        # Three MAX(...) - MAX(...) - MAX(...) doesn't match Pattern A's
        # exact-two-operand shape. Deferred.
        text = """\
CREATE OR REPLACE FUNCTION OFSERM.THREE_TERM_FN RETURN VARCHAR2 AS
BEGIN
    MERGE INTO TGT TT USING (
      SELECT MIN(CASE WHEN ((D.V_STD_ACCT_HEAD_ID = 'CAP1')) THEN 10 ELSE 11 END) AS COND_666_10,
        (MAX(CASE WHEN x IN (SELECT v FROM D WHERE v_std_acct_head_id = 'CAP2')
            THEN n_amt ELSE NULL END)
         - MAX(CASE WHEN x IN (SELECT v FROM D WHERE v_std_acct_head_id = 'CAP3')
            THEN n_amt ELSE NULL END)
         - MAX(CASE WHEN x IN (SELECT v FROM D WHERE v_std_acct_head_id = 'CAP4')
            THEN n_amt ELSE NULL END)) AS EXP_666_10,
        MIN(n_amt) AS EXP_666_11
      FROM SRC
    ) SS ON (TT.id = SS.id)
    WHEN MATCHED THEN UPDATE SET
      TT.col = CASE WHEN COND_666_10=10 THEN EXP_666_10 ELSE EXP_666_11 END;
    RETURN 'OK';
END;
/
"""
        # CAP1, CAP2, CAP3, CAP4 are all 1-digit so they don't match the
        # default CAP\d{3} pattern; widen the patterns for this test.
        from src.parsing.literals import compile_patterns
        widened = compile_patterns({
            "wide": {"regex": r"CAP\d{1,4}", "description": "any CAP"},
        })
        assert extract_derivations(
            text.splitlines(keepends=True), "THREE_TERM_FN", widened,
        ) == []

    def test_no_business_literal_returns_empty(self, patterns):
        # Function with a clean MERGE template but no business identifier
        # literals — the extractor produces nothing because there's no
        # target literal to attach to.
        text = """\
CREATE OR REPLACE FUNCTION OFSERM.NO_LIT_FN RETURN VARCHAR2 AS
BEGIN
    MERGE INTO TGT TT USING (
      SELECT MIN(CASE WHEN d.flag = 'X' THEN 10 ELSE 11 END) AS COND_444_10,
        MAX(CASE WHEN x IN (SELECT v FROM D WHERE v_std_acct_head_id = 'OTHER')
            THEN n_amt ELSE NULL END) AS EXP_444_10
      FROM SRC
    ) SS ON (TT.id = SS.id)
    WHEN MATCHED THEN UPDATE SET TT.col = EXP_444_10;
    RETURN 'OK';
END;
/
"""
        assert extract_derivations(
            text.splitlines(keepends=True), "NO_LIT_FN", patterns,
        ) == []


# ---------------------------------------------------------------------------
# Cross-reference helper — attach derivation summaries onto literal index
# ---------------------------------------------------------------------------

class TestAttachToLiteralIndex:
    def test_summary_added_only_to_case_when_target_records(self):
        index = {
            "CAP943": [
                {"function": "FN_X", "line": 24, "role": "case_when_target"},
                {"function": "FN_X", "line": 30, "role": "filter"},
                {"function": "OTHER_FN", "line": 5, "role": "in_list_member"},
            ],
            "CAP309": [
                {"function": "FN_X", "line": 24, "role": "case_when_source"},
            ],
        }
        derivations = [{
            "target_literal": "CAP943",
            "target_column": "N_STD_ACCT_HEAD_AMT",
            "source_literals": ["CAP309", "CAP863"],
            "operation": "SUBTRACT",
            "operands": [
                {"literal": "CAP309", "amount_column": "x.amt"},
                {"literal": "CAP863", "amount_column": "x.amt"},
            ],
            "function": "FN_X",
            "line_range": [24, 24],
        }]
        attach_derivations_to_literal_index(index, derivations)

        # The case_when_target record gets the derivation summary.
        targets = [r for r in index["CAP943"] if r["role"] == "case_when_target"]
        assert len(targets) == 1
        assert "derivation" in targets[0]
        assert targets[0]["derivation"]["operation"] == "SUBTRACT"
        assert targets[0]["derivation"]["source_literals"] == ["CAP309", "CAP863"]
        assert targets[0]["derivation"]["target_column"] == "N_STD_ACCT_HEAD_AMT"

        # filter / in_list_member records are untouched.
        for rec in index["CAP943"]:
            if rec["role"] != "case_when_target":
                assert "derivation" not in rec

        # CAP309 is a SOURCE in the derivation, not a target — its
        # case_when_source record gets nothing.
        for rec in index["CAP309"]:
            assert "derivation" not in rec

    def test_no_match_leaves_index_alone(self):
        # Derivation references a function that doesn't appear in the
        # literal index for that identifier — no-op.
        index = {
            "CAP943": [
                {"function": "OTHER_FN", "line": 1, "role": "case_when_target"},
            ],
        }
        derivations = [{
            "target_literal": "CAP943",
            "target_column": "X",
            "source_literals": ["CAP1"],
            "operation": "DIRECT_ASSIGN",
            "operands": [{"literal": "CAP1", "amount_column": "y"}],
            "function": "DIFFERENT_FN",
            "line_range": [1, 1],
        }]
        attach_derivations_to_literal_index(index, derivations)
        assert "derivation" not in index["CAP943"][0]

    def test_empty_derivations_is_noop(self):
        index = {"CAP943": [{"function": "FN", "line": 1, "role": "case_when_target"}]}
        attach_derivations_to_literal_index(index, [])
        assert "derivation" not in index["CAP943"][0]
