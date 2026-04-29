"""Unit tests for W35 Phase 5 — business identifier literal extraction.

Covers:
  - Pattern compilation (default + custom + invalid handling)
  - Literal extraction across the four roles (filter, case_when_source,
    case_when_target, in_list_member)
  - Per-schema index aggregation
"""

import pytest

from src.parsing.literals import (
    DEFAULT_BUSINESS_IDENTIFIER_PATTERNS,
    CompiledPattern,
    classify_role,
    compile_patterns,
    extract_literals,
    merge_into_index,
)


# ---------------------------------------------------------------------------
# Pattern matching: bare identifier matches and non-matches
# ---------------------------------------------------------------------------

class TestPatternMatching:
    def setup_method(self):
        self.compiled = compile_patterns(DEFAULT_BUSINESS_IDENTIFIER_PATTERNS)
        assert len(self.compiled) == 1
        self.cap = self.compiled[0]

    def test_matches_three_digit_cap_codes(self):
        for code in ("CAP973", "CAP943", "CAP013", "CAP000", "CAP999"):
            assert self.cap.bare.fullmatch(code) is not None, code

    def test_does_not_match_four_digit_cap_code(self):
        # CAP1234 is too long for the default CAP\d{3} pattern.
        assert self.cap.bare.fullmatch("CAP1234") is None

    def test_does_not_match_two_digit_cap_code(self):
        assert self.cap.bare.fullmatch("CAP12") is None

    def test_does_not_match_non_numeric_suffix(self):
        # CAPX does not match because the suffix isn't 3 digits.
        assert self.cap.bare.fullmatch("CAPX") is None

    def test_does_not_match_no_digits(self):
        # CAP_ has no digit suffix at all.
        assert self.cap.bare.fullmatch("CAP_") is None
        assert self.cap.bare.fullmatch("CAP") is None

    def test_lowercase_does_not_match(self):
        # OFSAA convention: CAP-codes are uppercase. The default pattern
        # is case-sensitive on purpose so a column literal like
        # 'cap973' (which doesn't exist in this corpus) doesn't get
        # double-counted.
        assert self.cap.bare.fullmatch("cap973") is None
        assert self.cap.bare.fullmatch("Cap973") is None


# ---------------------------------------------------------------------------
# Pattern compilation: defaults / custom / invalid
# ---------------------------------------------------------------------------

class TestCompilePatterns:
    def test_none_config_returns_default(self):
        compiled = compile_patterns(None)
        assert len(compiled) == 1
        assert compiled[0].name == "cap_codes"

    def test_empty_config_returns_default(self):
        compiled = compile_patterns({})
        assert len(compiled) == 1

    def test_default_can_disable_via_explicit_empty(self):
        # Passing a sentinel empty dict to compile_patterns falls back to
        # default. To DISABLE indexing, callers pass an empty dict that
        # bypasses the default fallback at extract_literals via empty
        # iterable.
        compiled = compile_patterns({})
        # The Phase 5 prompt requires that the default fires when the
        # config block is omitted — locked in by this test.
        assert any(p.name == "cap_codes" for p in compiled)

    def test_loading_multiple_patterns(self):
        cfg = {
            "cap_codes": {"regex": r"CAP\d{3}", "description": "Basel"},
            "gl_codes": {"regex": r"GL\d{6}", "description": "GL accounts"},
        }
        compiled = compile_patterns(cfg)
        names = {p.name for p in compiled}
        assert names == {"cap_codes", "gl_codes"}

    def test_invalid_regex_is_skipped_but_others_kept(self):
        cfg = {
            "broken": {"regex": "[invalid("},     # unbalanced
            "good": {"regex": r"CAP\d{3}"},
        }
        compiled = compile_patterns(cfg)
        names = {p.name for p in compiled}
        # The broken entry is dropped; "good" survives.
        assert names == {"good"}

    def test_missing_regex_field_is_skipped(self):
        cfg = {"empty": {"description": "no regex"}}
        compiled = compile_patterns(cfg)
        assert compiled == []

    def test_non_dict_entry_is_skipped(self):
        cfg = {"weird": "not-a-dict"}
        compiled = compile_patterns(cfg)
        assert compiled == []


# ---------------------------------------------------------------------------
# Role classification (the heart of Phase 5)
# ---------------------------------------------------------------------------

def _find_first(text: str, target: str) -> tuple[int, int]:
    """Locate the first occurrence of *target* (a quote-stripped identifier)
    inside a quoted form ``'target'`` and return (start, end) of the
    identifier text only (NOT including the quotes)."""
    needle = "'" + target + "'"
    pos = text.index(needle)
    return (pos + 1, pos + 1 + len(target))


class TestClassifyRole:
    def test_in_list_member_simple(self):
        text = "WHERE V_STD_ACCT_HEAD_ID IN ('CAP139', 'CAP943', 'CAP973')"
        for code in ("CAP139", "CAP943", "CAP973"):
            s, e = _find_first(text, code)
            assert classify_role(text, s, e) == "in_list_member", code

    def test_in_subquery_is_not_in_list(self):
        # IN (SELECT ...) is a subquery, not a literal list. The literal
        # inside the subquery's WHERE is itself a filter.
        text = (
            "CASE WHEN n_skey IN ("
            "SELECT n_skey FROM DIM WHERE v_std_acct_head_id = 'CAP309') "
            "THEN amt ELSE NULL END"
        )
        s, e = _find_first(text, "CAP309")
        # NOT in_list_member because the enclosing paren contains SELECT.
        assert classify_role(text, s, e) != "in_list_member"

    def test_case_when_target_int_flag(self):
        text = (
            "MIN(CASE WHEN ( ((DIM.V_STD_ACCT_HEAD_ID  = 'CAP943')) ) "
            "THEN 10 ELSE 11 END) AS COND_X"
        )
        s, e = _find_first(text, "CAP943")
        assert classify_role(text, s, e) == "case_when_target"

    def test_case_when_target_or_chain(self):
        # CS_Regulatory_Adjustments_Phase_In_Deduction_Amount has an
        # OR-chain of equalities inside one CASE WHEN. Each CAP-code in
        # the chain should classify as case_when_target.
        text = (
            "(CASE WHEN ( ((DIM.V_STD_ACCT_HEAD_ID  = 'CAP139')) "
            "OR ((DIM.V_STD_ACCT_HEAD_ID  = 'CAP015')) "
            "OR ((DIM.V_STD_ACCT_HEAD_ID  = 'CAP943')) ) "
            "THEN 10 ELSE 11 END)"
        )
        for code in ("CAP139", "CAP015", "CAP943"):
            s, e = _find_first(text, code)
            assert classify_role(text, s, e) == "case_when_target", code

    def test_case_when_source_amount_branch(self):
        # CS_Deferred_Tax pattern: literal in subquery WHERE, CASE WHEN's
        # THEN returns CAPITAL_ACCOUNTING.n_std_acct_head_amt. Should be
        # case_when_source.
        text = (
            "(MAX(CASE WHEN CAPITAL_ACCOUNTING.n_std_acct_head_skey "
            "IN (SELECT n_std_acct_head_skey FROM DIM_STANDARD_ACCT_HEAD "
            "WHERE v_std_acct_head_id = 'CAP309') "
            "THEN CAPITAL_ACCOUNTING.n_std_acct_head_amt ELSE NULL END )"
        )
        s, e = _find_first(text, "CAP309")
        assert classify_role(text, s, e) == "case_when_source"

    def test_case_when_source_subtraction_pair(self):
        # The CAP863 sibling of CAP309 in CS_Deferred_Tax — same shape.
        text = (
            "MAX(CASE WHEN CAPITAL_ACCOUNTING.N_STD_ACCT_HEAD_SKEY "
            "IN (SELECT DIM.N_STD_ACCT_HEAD_SKEY FROM DIM "
            "WHERE DIM.V_STD_ACCT_HEAD_ID = 'CAP863') "
            "THEN CAPITAL_ACCOUNTING.n_std_acct_head_amt ELSE NULL END )"
        )
        s, e = _find_first(text, "CAP863")
        assert classify_role(text, s, e) == "case_when_source"

    def test_filter_in_where_clause(self):
        text = (
            "WHERE (1=1) AND DIM_DATES.D_CALENDAR_DATE = TO_DATE('20260331','yyyymmdd') "
            "AND ( (((DIM.V_STD_ACCT_HEAD_ID  = 'CAP943'))) ) "
            "GROUP BY x"
        )
        s, e = _find_first(text, "CAP943")
        assert classify_role(text, s, e) == "filter"

    def test_filter_in_join_predicate(self):
        # An equality on a CAP-code inside a JOIN ON predicate (no CASE,
        # no IN-list) — falls through to the default 'filter' role.
        text = (
            "LEFT OUTER JOIN FSI_SETUP_CAPITAL_HEAD ON "
            "FSI_SETUP_CAPITAL_HEAD.V_STD_ACCT_HEAD_ID = 'CAP936' "
            "AND DIM_DATES.D_CALENDAR_DATE BETWEEN x AND y"
        )
        s, e = _find_first(text, "CAP936")
        assert classify_role(text, s, e) == "filter"


# ---------------------------------------------------------------------------
# extract_literals — end-to-end on representative fixtures
# ---------------------------------------------------------------------------

class TestExtractLiterals:
    def setup_method(self):
        self.patterns = compile_patterns(None)

    def test_cs_deferred_tax_merge_classifies_correctly(self):
        # Trimmed-down replica of CS_Deferred_Tax_Asset_Net_of_DTL_Calculation
        # focusing on the three CAP-codes whose roles Phase 5 must
        # distinguish. Spread over multiple lines so the fixture exercises
        # line-number tracking.
        source = [
            "CREATE OR REPLACE FUNCTION OFSERM.CS_DEFERRED_TAX RETURN VARCHAR2 AS\n",
            "BEGIN\n",
            "    MERGE INTO FCT_STANDARD_ACCT_HEAD TT USING (\n",
            "      SELECT MIN(CASE WHEN ( ((DIM.V_STD_ACCT_HEAD_ID = 'CAP943')) ) "
            "THEN 10 ELSE 11 END) AS COND_X,\n",
            "      (MAX(CASE WHEN CAPITAL_ACCOUNTING.n_std_acct_head_skey "
            "IN (SELECT n_std_acct_head_skey FROM DIM "
            "WHERE v_std_acct_head_id = 'CAP309') "
            "THEN CAPITAL_ACCOUNTING.n_std_acct_head_amt ELSE NULL END) "
            "- MAX(CASE WHEN CAPITAL_ACCOUNTING.N_STD_ACCT_HEAD_SKEY "
            "IN (SELECT DIM.N_STD_ACCT_HEAD_SKEY FROM DIM "
            "WHERE DIM.V_STD_ACCT_HEAD_ID = 'CAP863') "
            "THEN CAPITAL_ACCOUNTING.n_std_acct_head_amt ELSE NULL END)) AS EXP_X\n",
            "      FROM FCT_STANDARD_ACCT_HEAD\n",
            "      WHERE (1=1) AND ( (((DIM.V_STD_ACCT_HEAD_ID = 'CAP943'))) )\n",
            "    ) SS;\n",
            "END;\n",
        ]

        records = extract_literals(
            source_lines=source,
            function_name="CS_DEFERRED_TAX",
            patterns=self.patterns,
        )

        # Group by (identifier, role) for assertions.
        by_ident_role: dict[tuple[str, str], list[dict]] = {}
        for r in records:
            by_ident_role.setdefault((r["identifier"], r["role"]), []).append(r)

        # CAP943: at least one case_when_target (line 4) AND one filter (line 7).
        assert ("CAP943", "case_when_target") in by_ident_role
        assert ("CAP943", "filter") in by_ident_role

        # CAP309 and CAP863: case_when_source (subquery WHERE inside CASE WHEN).
        assert ("CAP309", "case_when_source") in by_ident_role
        assert ("CAP863", "case_when_source") in by_ident_role

        # All records carry the function name.
        assert {r["function"] for r in records} == {"CS_DEFERRED_TAX"}

    def test_regulatory_adjustment_in_list(self):
        source = [
            "CREATE OR REPLACE FUNCTION OFSERM.REGULATORY_ADJUSTMENT_DATA_POP RETURN VARCHAR2 AS\n",
            "BEGIN\n",
            "    INSERT INTO FCT_STANDARD_ACCT_HEAD (col) SELECT x FROM DIM\n",
            "      WHERE DIM.V_STD_ACCT_HEAD_ID IN "
            "('CAP139', 'CAP852', 'CAP943', 'CAP973');\n",
            "END;\n",
        ]
        records = extract_literals(
            source_lines=source,
            function_name="REGULATORY_ADJUSTMENT_DATA_POP",
            patterns=self.patterns,
        )

        # Every CAP-code should be in_list_member.
        roles_by_id = {r["identifier"]: r["role"] for r in records}
        assert roles_by_id == {
            "CAP139": "in_list_member",
            "CAP852": "in_list_member",
            "CAP943": "in_list_member",
            "CAP973": "in_list_member",
        }

    def test_non_cap_literal_is_not_extracted(self):
        # The default pattern is CAP\d{3}; a 'ABL' literal (run code) or
        # 'CS' (risk type) must not surface as a business identifier.
        source = [
            "FUNCTION FN AS BEGIN\n",
            "  WHERE V_LV_CODE = 'ABL' AND V_RISK_SUB_TYPE = 'CS';\n",
            "END;\n",
        ]
        records = extract_literals(
            source_lines=source,
            function_name="FN",
            patterns=self.patterns,
        )
        assert records == []

    def test_no_patterns_returns_empty(self):
        # Empty patterns iterable → no extraction at all (extraction
        # becomes a no-op even when source contains CAP-codes).
        source = ["WHERE V_STD_ACCT_HEAD_ID = 'CAP973';\n"]
        records = extract_literals(
            source_lines=source,
            function_name="FN",
            patterns=[],
        )
        assert records == []

    def test_records_sorted_by_function_line_identifier(self):
        # Determinism guard — mixed input order in source must produce
        # output ordered by (function, line, identifier).
        source = [
            "WHERE x = 'CAP973' AND y = 'CAP139';\n",
            "WHERE z = 'CAP015';\n",
        ]
        records = extract_literals(
            source_lines=source,
            function_name="FN",
            patterns=self.patterns,
        )
        # All on the same function; sorted by line, then identifier
        # alphabetically among same-line records.
        keys = [(r["function"], r["line"], r["identifier"]) for r in records]
        assert keys == sorted(keys)

    def test_multiple_patterns_dedupe_same_match(self):
        # Two different pattern entries that BOTH match the same token
        # (e.g. broad and narrow forms) must not double-count.
        patterns = compile_patterns({
            "narrow": {"regex": r"CAP973"},
            "broad": {"regex": r"CAP\d{3}"},
        })
        source = ["WHERE x = 'CAP973';\n"]
        records = extract_literals(
            source_lines=source,
            function_name="FN",
            patterns=patterns,
        )
        # Both patterns match 'CAP973' on the same line with the same role,
        # so the de-dup logic in extract_literals collapses them.
        assert len(records) == 1
        assert records[0]["identifier"] == "CAP973"


# ---------------------------------------------------------------------------
# merge_into_index — per-schema aggregation
# ---------------------------------------------------------------------------

class TestMergeIntoIndex:
    def test_records_grouped_by_identifier(self):
        index: dict[str, list[dict]] = {}
        merge_into_index(index, [
            {"identifier": "CAP943", "function": "CS_DEFERRED_TAX",
             "line": 4, "role": "case_when_target"},
            {"identifier": "CAP943", "function": "CS_DEFERRED_TAX",
             "line": 7, "role": "filter"},
            {"identifier": "CAP309", "function": "CS_DEFERRED_TAX",
             "line": 5, "role": "case_when_source"},
        ])
        merge_into_index(index, [
            {"identifier": "CAP943", "function": "REGULATORY_ADJUSTMENT_DATA_POP",
             "line": 4, "role": "in_list_member"},
        ])

        assert set(index.keys()) == {"CAP943", "CAP309"}

        # CAP943 has three records, sorted by (function, line, role).
        cap943 = index["CAP943"]
        assert len(cap943) == 3
        # REGULATORY_ADJUSTMENT_... < CS_DEFERRED_TAX alphabetically? No:
        # "C" < "R", so CS_DEFERRED_TAX comes first.
        functions_in_order = [r["function"] for r in cap943]
        assert functions_in_order == [
            "CS_DEFERRED_TAX",
            "CS_DEFERRED_TAX",
            "REGULATORY_ADJUSTMENT_DATA_POP",
        ]
        # Within CS_DEFERRED_TAX, lines ordered ascending.
        assert cap943[0]["line"] < cap943[1]["line"]

    def test_does_not_carry_identifier_in_records(self):
        # The identifier is the index key, so per-record entries should
        # carry only function/line/role — keeps the Redis payload tight.
        index: dict[str, list[dict]] = {}
        merge_into_index(index, [
            {"identifier": "CAP943", "function": "FN",
             "line": 1, "role": "filter"},
        ])
        rec = index["CAP943"][0]
        assert "identifier" not in rec
        assert set(rec.keys()) == {"function", "line", "role"}
