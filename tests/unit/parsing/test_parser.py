"""
Unit tests for src.parsing.parser — Tests 1-6.
"""

import pytest

from src.parsing.parser import (
    parse_function,
    is_in_block_comment,
    detect_execution_condition,
    extract_table_names,
    extract_conditions,
)


# -----------------------------------------------------------------------
# Test 1: test_parse_insert_node
# -----------------------------------------------------------------------

def test_parse_insert_node():
    """INSERT INTO ... SELECT ... FROM ... ; followed by COMMIT produces
    one raw_block with block_type='INSERT' and correct line boundaries."""
    source_lines = [
        "BEGIN\n",
        "  INSERT INTO STG_PRODUCT_PROCESSOR B (\n",
        "    FIC_MIS_DATE, V_GL_CODE, N_EOP_BAL\n",
        "  ) SELECT\n",
        "    GL.FIC_MIS_DATE,\n",
        "    GL.V_GL_CODE,\n",
        "    GL.N_AMOUNT_LCY\n",
        "  FROM STG_GL_DATA GL;\n",
        "  COMMIT;\n",
        "END;\n",
    ]

    result = parse_function(source_lines, "FN_TEST_INSERT")

    assert result["function_name"] == "FN_TEST_INSERT"
    assert result["total_lines"] == len(source_lines)

    blocks = result["raw_blocks"]
    insert_blocks = [b for b in blocks if b["block_type"] == "INSERT"]
    assert len(insert_blocks) == 1

    block = insert_blocks[0]
    assert block["block_type"] == "INSERT"
    # line_start / line_end are 1-based
    assert block["line_start"] >= 2
    assert block["line_end"] <= 9
    assert any("INSERT" in line.upper() for line in block["raw_lines"])


# -----------------------------------------------------------------------
# Test 2: test_parse_while_loop
# -----------------------------------------------------------------------

def test_parse_while_loop():
    """WHILE ... LOOP ... END LOOP; produces a WHILE raw_block whose
    raw_lines include the inner body."""
    source_lines = [
        "BEGIN\n",
        "  WHILE LN_COUNTER <= 2 LOOP\n",
        "    INSERT INTO STG_TMP (COL1) SELECT COL1 FROM SRC;\n",
        "    LN_COUNTER := LN_COUNTER + 1;\n",
        "  END LOOP;\n",
        "END;\n",
    ]

    result = parse_function(source_lines, "FN_TEST_WHILE")
    blocks = result["raw_blocks"]

    while_blocks = [b for b in blocks if b["block_type"] == "WHILE"]
    assert len(while_blocks) == 1

    wb = while_blocks[0]
    assert wb["block_type"] == "WHILE"
    # The inner lines (INSERT, assignment, END LOOP) must be captured
    inner_text = "\n".join(wb["raw_lines"])
    assert "INSERT" in inner_text.upper()
    assert "END LOOP" in inner_text.upper()


# -----------------------------------------------------------------------
# Test 3: test_detect_execution_condition_december
# -----------------------------------------------------------------------

def test_detect_execution_condition_december():
    """An IF ... EXTRACT(MONTH ...) = 12 guard after BEGIN is detected as
    a MONTH_CHECK execution condition."""
    source_lines = [
        "CREATE OR REPLACE FUNCTION FN_DEC RETURN NUMBER AS\n",
        "BEGIN\n",
        "  IF TO_NUMBER(EXTRACT(MONTH FROM TO_DATE(CQD, 'DD-MON-RR'))) = 12 THEN\n",
        "    INSERT INTO TBL (COL) SELECT 1 FROM DUAL;\n",
        "  END IF;\n",
        "END;\n",
    ]

    cond = detect_execution_condition(source_lines)

    assert cond is not None
    assert cond["field"] == "MONTH"
    assert cond["value"] == "12"
    assert "EXTRACT" in cond["raw_condition"].upper()


# -----------------------------------------------------------------------
# Test 4: test_is_in_block_comment_true
# -----------------------------------------------------------------------

def test_is_in_block_comment_true():
    """Lines inside /* ... */ are detected; lines outside are not."""
    lines = [
        "code line 0",
        "/* start of block comment",
        "inside block comment",
        "end of block comment */",
        "code line 4",
    ]

    # Line 2 ("inside block comment") is inside the comment region
    assert is_in_block_comment(lines, 2) is True

    # Line 0 ("code line 0") is outside
    assert is_in_block_comment(lines, 0) is False

    # Line 4 ("code line 4") is after the comment closes
    assert is_in_block_comment(lines, 4) is False


# -----------------------------------------------------------------------
# Test 5: test_extract_table_names_insert
# -----------------------------------------------------------------------

def test_extract_table_names_insert():
    """extract_table_names correctly identifies target and source tables
    for an INSERT ... SELECT ... FROM ... JOIN ... statement."""
    raw_lines = [
        "INSERT INTO TARGET_TABLE (COL1, COL2)\n",
        "SELECT A.COL1, B.COL2\n",
        "FROM SOURCE1 A\n",
        "INNER JOIN SOURCE2 B ON A.ID = B.ID;\n",
    ]

    result = extract_table_names(raw_lines, "INSERT")

    assert result["target_table"] == "TARGET_TABLE"
    source_upper = [s.upper() for s in result["source_tables"]]
    assert "SOURCE1" in source_upper
    assert "SOURCE2" in source_upper


# -----------------------------------------------------------------------
# Test 6: test_extract_conditions_and_clause
# -----------------------------------------------------------------------

def test_extract_conditions_and_clause():
    """extract_conditions splits a WHERE clause on top-level AND into
    individual condition strings."""
    raw_lines = [
        "SELECT * FROM TBL A\n",
        "WHERE A.FIC_MIS_DATE = CQD AND A.V_LV_CODE = 'ABL' AND A.V_GL_CODE IN ('101','102');\n",
    ]

    conditions = extract_conditions(raw_lines)

    assert len(conditions) == 3
    cond_texts = [c.upper() for c in conditions]
    assert any("FIC_MIS_DATE" in c for c in cond_texts)
    assert any("V_LV_CODE" in c for c in cond_texts)
    assert any("V_GL_CODE" in c for c in cond_texts)


# -----------------------------------------------------------------------
# Test: test_update_block_not_split_by_lines
# -----------------------------------------------------------------------

def test_update_block_not_split_by_lines():
    """One logical UPDATE with CASE WHEN should be ONE raw_block."""
    lines = [
        "BEGIN",
        "   UPDATE STG_OPS_RISK_DATA OPS",
        "      SET OPS.N_ANNUAL_GROSS_INCOME =",
        "         CASE",
        "            WHEN OPS.V_LOB_CODE = 'CBA'",
        "            THEN NVL(OPS.N_ANNUAL_GROSS_INCOME + TOT1 + CBA_DEDUCTION, 0)",
        "            WHEN OPS.V_LOB_CODE = 'RBA'",
        "            THEN NVL(OPS.N_ANNUAL_GROSS_INCOME, 0) + LN_DEDUCITON_RATIO_1",
        "         END",
        "    WHERE OPS.FIC_MIS_DATE = CQD",
        "      AND OPS.V_LOB_CODE IN ('CBA', 'RBA')",
        "      AND OPS.V_LV_CODE <> 'ABLIBG';",
        "   COMMIT;",
        "END;",
    ]
    result = parse_function(lines, "TEST_FN")
    update_blocks = [b for b in result["raw_blocks"] if b["block_type"] == "UPDATE"]
    assert len(update_blocks) == 1, f"Expected 1 UPDATE block, got {len(update_blocks)}"


# -----------------------------------------------------------------------
# Test: test_assignment_detected_as_scalar_compute
# -----------------------------------------------------------------------

def test_assignment_detected_as_scalar_compute():
    """PL/SQL := assignment should be detected as SCALAR_COMPUTE."""
    lines = [
        "BEGIN",
        "   TOT1 := LN_TOTAL_DEDUCT + (-1 * LN_DEDUCITON_RATIO_1);",
        "END;",
    ]
    result = parse_function(lines, "TEST_FN")
    sc_blocks = [b for b in result["raw_blocks"] if b["block_type"] == "SCALAR_COMPUTE"]
    assert len(sc_blocks) >= 1
    assert sc_blocks[0].get("output_variable") == "TOT1"


# -----------------------------------------------------------------------
# Test: test_select_into_detected_as_scalar_compute
# -----------------------------------------------------------------------

def test_select_into_detected_as_select_into():
    """SELECT INTO should be detected as SELECT_INTO block type."""
    lines = [
        "BEGIN",
        "   SELECT ROUND(SUM(N_AMOUNT_LCY), 2)",
        "     INTO LN_TOTAL_DEDUCT",
        "     FROM STG_GL_DATA GLD",
        "    WHERE GLD.V_GL_CODE = 'DBS';",
        "END;",
    ]
    result = parse_function(lines, "TEST_FN")
    si_blocks = [b for b in result["raw_blocks"] if b["block_type"] == "SELECT_INTO"]
    assert len(si_blocks) >= 1


# -----------------------------------------------------------------------
# Test: test_visited_lines_prevents_duplicate_blocks
# -----------------------------------------------------------------------

def test_visited_lines_prevents_duplicate_blocks():
    """Scanner should not create duplicate blocks for the same lines."""
    lines = [
        "BEGIN",
        "   UPDATE STG_OPS_RISK_DATA OPS",
        "      SET OPS.N_ANNUAL_GROSS_INCOME = 100",
        "    WHERE OPS.FIC_MIS_DATE = CQD;",
        "   COMMIT;",
        "END;",
    ]
    result = parse_function(lines, "TEST_FN")
    update_blocks = [b for b in result["raw_blocks"] if b["block_type"] == "UPDATE"]
    assert len(update_blocks) == 1, f"Expected exactly 1 UPDATE, got {len(update_blocks)}"


# -----------------------------------------------------------------------
# Test: test_commented_line_stripped
# -----------------------------------------------------------------------

def test_commented_line_stripped():
    """Single-line -- comment should be cleaned to empty string."""
    from src.parsing.parser import clean_source_lines
    lines = [
        "  WHEN OPS.V_LOB_CODE = 'CBA'",
        "  --  WHEN OPS.V_LOB_CODE = 'CFI' THEN NVL(...) - LN_RATIO",
        "  WHEN OPS.V_LOB_CODE = 'RBA'",
    ]
    cleaned, _ = clean_source_lines(lines)
    assert cleaned[1].strip() == "", f"Expected empty, got: '{cleaned[1]}'"
    assert "CBA" in cleaned[0]
    assert "RBA" in cleaned[2]


# -----------------------------------------------------------------------
# Test: test_inline_comment_stripped
# -----------------------------------------------------------------------

def test_inline_comment_stripped():
    """Inline -- comment should be truncated."""
    from src.parsing.parser import clean_source_lines
    lines = ["  WHEN V_LOB = 'CBA' -- this is a comment"]
    cleaned, _ = clean_source_lines(lines)
    assert "CBA" in cleaned[0]
    assert "comment" not in cleaned[0]


# -----------------------------------------------------------------------
# Test: test_block_comment_stripped
# -----------------------------------------------------------------------

def test_block_comment_stripped():
    """Lines inside /* */ should be cleaned to empty strings."""
    from src.parsing.parser import clean_source_lines
    lines = [
        "active line 1",
        "active line 2",
        "/* start of comment",
        "inside comment",
        "still inside",
        "end of comment */",
        "active line 3",
    ]
    cleaned, ranges = clean_source_lines(lines)
    assert cleaned[0].strip() != ""
    assert cleaned[1].strip() != ""
    assert cleaned[3].strip() == "", f"Line 3 should be empty, got: '{cleaned[3]}'"
    assert cleaned[4].strip() == "", f"Line 4 should be empty, got: '{cleaned[4]}'"
    assert cleaned[6].strip() != ""


# -----------------------------------------------------------------------
# Test: test_update_with_case_is_one_block
# -----------------------------------------------------------------------

def test_update_with_case_is_one_block():
    """UPDATE with CASE WHEN spanning 15 lines should be ONE block."""
    from src.parsing.parser import clean_source_lines, find_block_end
    lines = [
        "   UPDATE STG_OPS_RISK_DATA OPS",
        "      SET OPS.N_ANNUAL_GROSS_INCOME =",
        "         CASE",
        "            WHEN OPS.V_LOB_CODE = 'CBA'",
        "            THEN NVL(OPS.N_ANNUAL_GROSS_INCOME + TOT1 + CBA_DEDUCTION, 0)",
        "            WHEN OPS.V_LOB_CODE = 'RBA'",
        "            THEN NVL(OPS.N_ANNUAL_GROSS_INCOME, 0) + LN_DEDUCITON_RATIO_1",
        "         END",
        "    WHERE OPS.FIC_MIS_DATE = CQD",
        "      AND OPS.V_LOB_CODE IN ('CBA', 'RBA')",
        "      AND OPS.V_LV_CODE <> 'ABLIBG';",
    ]
    cleaned, _ = clean_source_lines(lines)
    end = find_block_end(cleaned, 0, "UPDATE")
    assert end == 10, f"Expected end at line 10, got {end}"
