"""Unit tests for the W33 CHAR blank-padding fix.

Covers the three compound defects enabled by Oracle's CHAR(n) semantics:

  * Part 1 — schema catalog learns about per-column data types.
  * Part 3 — SQLGuardian.validate_char_column_comparisons rejects
    un-RTRIM'd CHAR bind comparisons.
  * Part 4 — DataQueryAgent._check_suspicious_result flags zero-result
    aggregates against populated target tables.
  * Prompt snapshot — the SYSTEM_PROMPT still carries the CHAR rule so
    the generator knows to wrap CHAR columns in RTRIM.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.agents.data_query import (
    DataQueryAgent,
    SYSTEM_PROMPT,
    format_column_type,
    load_column_types,
)
from src.tools.sql_guardian import (
    CharPaddingError,
    GuardianRejectionError,
    SQLGuardian,
)


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture
def guardian() -> SQLGuardian:
    return SQLGuardian()


@pytest.fixture
def typed_catalog() -> dict[str, dict[str, dict]]:
    """Schema snapshot with the data types that reproduce the W33 bug.

    F_EXPOSURE_ENABLED_IND is CHAR(3) — the column that silently returned
    zero rows in the sanity test. V_LV_CODE is VARCHAR2(20) so a
    comparison against it is always safe without RTRIM. N_EOP_BAL is
    NUMBER, FIC_MIS_DATE is DATE. F_FLAG_ONE is CHAR(1) — one-char CHAR
    is *exempt* from the RTRIM rule because exact-length binds match.
    """
    return {
        "STG_PRODUCT_PROCESSOR": {
            "V_ACCOUNT_NUMBER": {
                "data_type": "VARCHAR2", "data_length": 50,
                "data_precision": None, "data_scale": None,
            },
            "V_LV_CODE": {
                "data_type": "VARCHAR2", "data_length": 20,
                "data_precision": None, "data_scale": None,
            },
            "F_EXPOSURE_ENABLED_IND": {
                "data_type": "CHAR", "data_length": 3,
                "data_precision": None, "data_scale": None,
            },
            "F_FLAG_ONE": {
                "data_type": "CHAR", "data_length": 1,
                "data_precision": None, "data_scale": None,
            },
            "N_EOP_BAL": {
                "data_type": "NUMBER", "data_length": 22,
                "data_precision": 15, "data_scale": 2,
            },
            "FIC_MIS_DATE": {
                "data_type": "DATE", "data_length": 7,
                "data_precision": None, "data_scale": None,
            },
        },
        "STG_GL_DATA": {
            "V_GL_CODE": {
                "data_type": "VARCHAR2", "data_length": 30,
                "data_precision": None, "data_scale": None,
            },
            "N_AMOUNT_LCY": {
                "data_type": "NUMBER", "data_length": 22,
                "data_precision": 15, "data_scale": 2,
            },
            "FIC_MIS_DATE": {
                "data_type": "DATE", "data_length": 7,
                "data_precision": None, "data_scale": None,
            },
        },
    }


class _FakeRedis:
    """Minimal sync Redis fake — supports only .get / .set / .keys."""

    def __init__(self, storage: dict[bytes, bytes] | None = None) -> None:
        self._storage = storage or {}

    def keys(self, pattern: str) -> list[bytes]:
        prefix = pattern[:-1] if pattern.endswith("*") else pattern
        return [k for k in self._storage.keys() if k.decode().startswith(prefix)]

    def get(self, key) -> bytes | None:
        if isinstance(key, str):
            key = key.encode()
        return self._storage.get(key)

    def set(self, key, value) -> None:
        if isinstance(key, str):
            key = key.encode()
        if isinstance(value, str):
            value = value.encode()
        self._storage[key] = value


# ---------------------------------------------------------------------
# Test 1 — Schema catalog includes data types for the four major kinds
# ---------------------------------------------------------------------

def test_1_schema_catalog_loads_char_varchar_number_date_types():
    snapshot = {
        "tables": {
            "STG_PRODUCT_PROCESSOR": {
                "columns": {
                    "V_ACCOUNT_NUMBER": {
                        "data_type": "VARCHAR2", "data_length": 50,
                        "data_precision": None, "data_scale": None,
                        "nullable": "Y",
                    },
                    "F_EXPOSURE_ENABLED_IND": {
                        "data_type": "CHAR", "data_length": 3,
                        "data_precision": None, "data_scale": None,
                        "nullable": "Y",
                    },
                    "N_EOP_BAL": {
                        "data_type": "NUMBER", "data_length": 22,
                        "data_precision": 15, "data_scale": 2,
                        "nullable": "Y",
                    },
                    "FIC_MIS_DATE": {
                        "data_type": "DATE", "data_length": 7,
                        "data_precision": None, "data_scale": None,
                        "nullable": "N",
                    },
                },
            },
        },
    }
    fake = _FakeRedis()
    fake.set("rtie:schema:snapshot:OFSMDM", json.dumps(snapshot))

    loaded = load_column_types(fake, "OFSMDM")
    pp = loaded["STG_PRODUCT_PROCESSOR"]
    assert pp["F_EXPOSURE_ENABLED_IND"]["data_type"] == "CHAR"
    assert pp["F_EXPOSURE_ENABLED_IND"]["data_length"] == 3
    assert pp["V_ACCOUNT_NUMBER"]["data_type"] == "VARCHAR2"
    assert pp["N_EOP_BAL"]["data_type"] == "NUMBER"
    assert pp["N_EOP_BAL"]["data_precision"] == 15
    assert pp["FIC_MIS_DATE"]["data_type"] == "DATE"


# ---------------------------------------------------------------------
# Test 2 — CHAR(3) is distinguished from CHAR(1) and VARCHAR2(3)
# ---------------------------------------------------------------------

def test_2_format_column_type_distinguishes_char_lengths_and_varchar2():
    assert format_column_type({"data_type": "CHAR", "data_length": 3}) == "CHAR(3)"
    assert format_column_type({"data_type": "CHAR", "data_length": 1}) == "CHAR(1)"
    assert format_column_type({"data_type": "VARCHAR2", "data_length": 3}) == "VARCHAR2(3)"
    assert format_column_type(
        {"data_type": "NUMBER", "data_precision": 10, "data_scale": 2}
    ) == "NUMBER(10,2)"
    assert format_column_type({"data_type": "DATE"}) == "DATE"
    # Missing info → empty string so callers can degrade gracefully.
    assert format_column_type(None) == ""
    assert format_column_type({}) == ""


# ---------------------------------------------------------------------
# Test 3 — SQLGuardian rejects unbounded CHAR comparison
# ---------------------------------------------------------------------

def test_3_guardian_rejects_unwrapped_char_bind_comparison(guardian, typed_catalog):
    bad_sql = (
        "SELECT COUNT(DISTINCT V_ACCOUNT_NUMBER) AS ACCOUNT_COUNT "
        "FROM STG_PRODUCT_PROCESSOR "
        "WHERE F_EXPOSURE_ENABLED_IND = :exposure_ind "
        "AND FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')"
    )
    with pytest.raises(CharPaddingError) as exc_info:
        guardian.validate_char_column_comparisons(bad_sql, typed_catalog)
    assert exc_info.value.column == "F_EXPOSURE_ENABLED_IND"
    assert exc_info.value.table == "STG_PRODUCT_PROCESSOR"
    assert exc_info.value.char_length == 3
    # Subclass of GuardianRejectionError so the existing catch chain
    # still lights up.
    assert isinstance(exc_info.value, GuardianRejectionError)


def test_3b_guardian_rejects_char_in_predicate(guardian, typed_catalog):
    bad_sql = (
        "SELECT COUNT(*) FROM STG_PRODUCT_PROCESSOR "
        "WHERE F_EXPOSURE_ENABLED_IND IN (:a, :b)"
    )
    with pytest.raises(CharPaddingError):
        guardian.validate_char_column_comparisons(bad_sql, typed_catalog)


# ---------------------------------------------------------------------
# Test 4 — SQLGuardian accepts RTRIM-wrapped CHAR comparison
# ---------------------------------------------------------------------

@pytest.mark.parametrize("wrapper", ["RTRIM", "TRIM", "LTRIM"])
def test_4_guardian_accepts_wrapped_char_comparison(guardian, typed_catalog, wrapper):
    good_sql = (
        "SELECT COUNT(*) FROM STG_PRODUCT_PROCESSOR "
        f"WHERE {wrapper}(F_EXPOSURE_ENABLED_IND) = :exposure_ind "
        "AND FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')"
    )
    assert guardian.validate_char_column_comparisons(good_sql, typed_catalog) is True


def test_4b_guardian_accepts_char_one_without_rtrim(guardian, typed_catalog):
    # CHAR(1) is exempt: an exact-length bind always matches.
    sql = (
        "SELECT COUNT(*) FROM STG_PRODUCT_PROCESSOR "
        "WHERE F_FLAG_ONE = :flag"
    )
    assert guardian.validate_char_column_comparisons(sql, typed_catalog) is True


# ---------------------------------------------------------------------
# Test 5 — SQLGuardian accepts VARCHAR2 comparison without RTRIM
# ---------------------------------------------------------------------

def test_5_guardian_accepts_varchar2_comparison_without_rtrim(guardian, typed_catalog):
    good_sql = (
        "SELECT COUNT(*) FROM STG_PRODUCT_PROCESSOR "
        "WHERE V_LV_CODE = :lv_code AND N_EOP_BAL > :amount "
        "AND FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')"
    )
    assert guardian.validate_char_column_comparisons(good_sql, typed_catalog) is True


def test_5b_guardian_noop_when_types_empty(guardian):
    sql = "SELECT COUNT(*) FROM STG_PRODUCT_PROCESSOR WHERE F_X = :x"
    assert guardian.validate_char_column_comparisons(sql, {}) is True


def test_5c_guardian_accepts_mixed_char_and_varchar(guardian, typed_catalog):
    good_sql = (
        "SELECT COUNT(*) FROM STG_PRODUCT_PROCESSOR "
        "WHERE RTRIM(F_EXPOSURE_ENABLED_IND) = :exposure_ind "
        "AND V_LV_CODE = :lv_code"
    )
    assert guardian.validate_char_column_comparisons(good_sql, typed_catalog) is True


# ---------------------------------------------------------------------
# Test 6 — Suspicious-result detector flags zero over populated table
# ---------------------------------------------------------------------

class _FakeSchemaToolsFixedBaseline:
    """Schema tools stub whose execute_raw always returns the same
    baseline COUNT(*). Records every SQL it was asked to execute."""

    def __init__(self, baseline: int) -> None:
        self._baseline = baseline
        self.executed: list[tuple[str, dict]] = []

    async def execute_raw(self, sql: str, params: dict) -> list[tuple]:
        self.executed.append((sql, params))
        return [(self._baseline,)]


def test_6_suspicious_detector_flags_zero_count_on_populated_table(guardian):
    agent = DataQueryAgent(
        schema_tools=_FakeSchemaToolsFixedBaseline(baseline=669),
        redis_client=None,
        sql_guardian=guardian,
    )
    sql = (
        "SELECT COUNT(DISTINCT V_ACCOUNT_NUMBER) FROM STG_PRODUCT_PROCESSOR "
        "WHERE F_EXPOSURE_ENABLED_IND = :exposure_ind "
        "AND FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')"
    )
    suspicious, reason = asyncio.run(
        agent._check_suspicious_result(
            sql=sql,
            query_kind="AGGREGATE",
            columns=["ACCOUNT_COUNT"],
            rows=[[0]],
            params={"exposure_ind": "N", "mis_date": "2025-12-31"},
        )
    )
    assert suspicious is True
    assert "STG_PRODUCT_PROCESSOR" in reason
    assert "669" in reason
    assert "F_EXPOSURE_ENABLED_IND" in reason


# ---------------------------------------------------------------------
# Test 7 — Does NOT flag when the target table is empty at the date
# ---------------------------------------------------------------------

def test_7_suspicious_detector_does_not_flag_empty_date(guardian):
    agent = DataQueryAgent(
        schema_tools=_FakeSchemaToolsFixedBaseline(baseline=0),
        redis_client=None,
        sql_guardian=guardian,
    )
    sql = (
        "SELECT COUNT(*) FROM STG_PRODUCT_PROCESSOR "
        "WHERE F_EXPOSURE_ENABLED_IND = :exposure_ind "
        "AND FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')"
    )
    suspicious, reason = asyncio.run(
        agent._check_suspicious_result(
            sql=sql,
            query_kind="AGGREGATE",
            columns=["ACCOUNT_COUNT"],
            rows=[[0]],
            params={"exposure_ind": "N", "mis_date": "2020-01-01"},
        )
    )
    assert suspicious is False
    assert reason is None


# ---------------------------------------------------------------------
# Test 8 — Does NOT flag legitimate non-zero aggregates
# ---------------------------------------------------------------------

def test_8_suspicious_detector_does_not_flag_nonzero_sum(guardian):
    agent = DataQueryAgent(
        schema_tools=_FakeSchemaToolsFixedBaseline(baseline=1000),
        redis_client=None,
        sql_guardian=guardian,
    )
    sql = (
        "SELECT SUM(N_EOP_BAL) FROM STG_PRODUCT_PROCESSOR "
        "WHERE V_LV_CODE = :lv_code "
        "AND FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')"
    )
    suspicious, _ = asyncio.run(
        agent._check_suspicious_result(
            sql=sql,
            query_kind="AGGREGATE",
            columns=["SUM"],
            rows=[[42_500.75]],
            params={"lv_code": "ABL", "mis_date": "2025-12-31"},
        )
    )
    assert suspicious is False


def test_8b_suspicious_detector_skips_date_only_filter(guardian):
    """A zero count under a date-only filter is just 'no rows that day'
    — not a data-type mismatch. The detector must let it through."""
    agent = DataQueryAgent(
        schema_tools=_FakeSchemaToolsFixedBaseline(baseline=1000),
        redis_client=None,
        sql_guardian=guardian,
    )
    sql = (
        "SELECT COUNT(*) FROM STG_PRODUCT_PROCESSOR "
        "WHERE FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')"
    )
    suspicious, _ = asyncio.run(
        agent._check_suspicious_result(
            sql=sql,
            query_kind="AGGREGATE",
            columns=["COUNT"],
            rows=[[0]],
            params={"mis_date": "2025-12-31"},
        )
    )
    assert suspicious is False


def test_8c_suspicious_detector_ignores_row_list(guardian):
    agent = DataQueryAgent(
        schema_tools=_FakeSchemaToolsFixedBaseline(baseline=1000),
        redis_client=None,
        sql_guardian=guardian,
    )
    sql = (
        "SELECT V_ACCOUNT_NUMBER FROM STG_PRODUCT_PROCESSOR "
        "WHERE F_EXPOSURE_ENABLED_IND = :x "
        "AND FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')"
    )
    suspicious, _ = asyncio.run(
        agent._check_suspicious_result(
            sql=sql,
            query_kind="ROW_LIST",
            columns=["V_ACCOUNT_NUMBER"],
            rows=[],  # empty row list — not an aggregate
            params={"x": "N", "mis_date": "2025-12-31"},
        )
    )
    assert suspicious is False


# ---------------------------------------------------------------------
# Test 9 — Prompt snapshot confirms the CHAR rule is present
# ---------------------------------------------------------------------

def test_9_system_prompt_contains_char_rule_and_rtrim_example():
    upper = SYSTEM_PROMPT.upper()
    assert "CHAR COLUMN HANDLING" in upper
    assert "RTRIM" in upper
    # The rule must name the risk explicitly — blank padding.
    assert "PAD" in upper or "PADDING" in upper
    # Positive example: F_EXPOSURE_ENABLED_IND wrapped in RTRIM. This is
    # the exact column that reproduced the W33 bug, so future prompt
    # edits must keep a CHAR column in the example.
    assert "RTRIM(F_EXPOSURE_ENABLED_IND)" in SYSTEM_PROMPT
