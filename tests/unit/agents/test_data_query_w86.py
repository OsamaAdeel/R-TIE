"""Unit tests for W86 — all-metric-columns-null suspicion detector.

Covers the stakeholder test 2026-05-12 failure class where DATA_QUERY
returned VERIFIED despite every metric column being NULL across every
row (aggregate returning one row of nulls, or row-list with the
measure column empty for the requested filter). W33's Layer-4
detector does not catch these — different gates.

Most tests target the module-level helpers directly so the suite stays
fast and deterministic. Two streaming tests verify the wiring through
``answer_stream`` (warnings list grows, ``suspicious`` flips on).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.agents.data_query import (
    DataQueryAgent,
    _detect_all_null_metric_columns,
    _format_all_null_message,
    _is_metric_column,
    _split_select_list,
)
from src.tools.sql_guardian import SQLGuardian


# ---------------------------------------------------------------------
# _is_metric_column — column classification
# ---------------------------------------------------------------------

def test_aggregate_select_is_metric():
    assert _is_metric_column(
        col_name="TOTAL_ALPHA_PERCENT",
        col_type=None,
        select_item="SUM(N_ALPHA_PERCENT) AS TOTAL_ALPHA_PERCENT",
    ) is True


def test_count_star_aggregate_is_not_metric():
    # COUNT(*) = 0 is a real answer to "how many accounts have X",
    # not a missing metric. Must not fire W86.
    assert _is_metric_column(
        col_name="CNT",
        col_type=None,
        select_item="COUNT(*) AS CNT",
    ) is False


def test_count_literal_aggregate_is_not_metric():
    # COUNT(1) behaves identically to COUNT(*).
    assert _is_metric_column(
        col_name="CNT",
        col_type=None,
        select_item="COUNT(1) AS CNT",
    ) is False


def test_count_column_aggregate_is_metric():
    # COUNT(N_AMOUNT) counts non-null measure rows — NULL still
    # signals missing data.
    assert _is_metric_column(
        col_name="N",
        col_type=None,
        select_item="COUNT(N_AMOUNT) AS N",
    ) is True


def test_n_prefix_numeric_measure_is_metric():
    assert _is_metric_column(
        col_name="N_EOP_BAL_NPL",
        col_type="NUMBER",
        select_item="N_EOP_BAL_NPL",
    ) is True


def test_number_column_no_naming_hint_is_metric():
    assert _is_metric_column(
        col_name="AMOUNT",
        col_type="NUMBER",
        select_item="AMOUNT",
    ) is True


def test_fic_mis_date_dimension_is_not_metric():
    assert _is_metric_column(
        col_name="FIC_MIS_DATE",
        col_type="DATE",
        select_item="FIC_MIS_DATE",
    ) is False


def test_skey_dimension_is_not_metric():
    assert _is_metric_column(
        col_name="N_PRODUCT_SKEY",
        col_type="NUMBER",
        select_item="N_PRODUCT_SKEY",
    ) is False


def test_v_prefix_dimension_is_not_metric_without_type():
    assert _is_metric_column(
        col_name="V_LV_CODE",
        col_type=None,
        select_item="V_LV_CODE",
    ) is False


def test_empty_name_is_not_metric():
    assert _is_metric_column(
        col_name="",
        col_type="NUMBER",
        select_item=None,
    ) is False


# ---------------------------------------------------------------------
# _split_select_list — SELECT-item parsing
# ---------------------------------------------------------------------

def test_split_select_list_respects_parens():
    sql = (
        "SELECT SUM(N_X), COALESCE(N_Y, 0) AS Y_VAL, COUNT(*) "
        "FROM TBL WHERE 1=1"
    )
    items = _split_select_list(sql)
    assert items == [
        "SUM(N_X)",
        "COALESCE(N_Y, 0) AS Y_VAL",
        "COUNT(*)",
    ]


def test_split_select_list_no_from_returns_empty():
    assert _split_select_list("not a real select") == []


# ---------------------------------------------------------------------
# _detect_all_null_metric_columns — positive cases (W86 fires)
# ---------------------------------------------------------------------

def test_aggregate_returns_one_row_all_null():
    """Q1-style: BIA at future date — aggregate returns one row of nulls."""
    sql = (
        "SELECT SUM(N_ALPHA_PERCENT) AS TOTAL_ALPHA_PERCENT, "
        "SUM(N_BETA_FACTOR) AS TOTAL_BETA_FACTOR "
        "FROM STG_OP_RISK_BIA WHERE FIC_MIS_DATE = :mis_date"
    )
    cols = _detect_all_null_metric_columns(
        sql=sql,
        columns=["TOTAL_ALPHA_PERCENT", "TOTAL_BETA_FACTOR"],
        rows=[(None, None)],
        column_types_for_table={},
    )
    assert cols == ["TOTAL_ALPHA_PERCENT", "TOTAL_BETA_FACTOR"]


def test_aggregate_returns_one_row_single_null_metric():
    sql = "SELECT SUM(N_AMOUNT) AS SUM_AMOUNT FROM STG_X WHERE FIC_MIS_DATE = :d"
    cols = _detect_all_null_metric_columns(
        sql=sql,
        columns=["SUM_AMOUNT"],
        rows=[(None,)],
        column_types_for_table={},
    )
    assert cols == ["SUM_AMOUNT"]


def test_row_list_all_rows_null_in_metric():
    """Q5-style: 100 rows returned, N_EOP_BAL_NPL is NULL on every row."""
    sql = (
        "SELECT FIC_MIS_DATE, V_ACCOUNT_NUMBER, N_EOP_BAL_NPL "
        "FROM STG_PRODUCT_PROCESSOR WHERE FIC_MIS_DATE = :d"
    )
    rows = [("2025-12-31", f"AC{i:05d}", None) for i in range(100)]
    cols = _detect_all_null_metric_columns(
        sql=sql,
        columns=["FIC_MIS_DATE", "V_ACCOUNT_NUMBER", "N_EOP_BAL_NPL"],
        rows=rows,
        column_types_for_table={
            "N_EOP_BAL_NPL": {"data_type": "NUMBER"},
            "FIC_MIS_DATE": {"data_type": "DATE"},
            "V_ACCOUNT_NUMBER": {"data_type": "VARCHAR2"},
        },
    )
    assert cols == ["N_EOP_BAL_NPL"]


def test_time_series_all_null_metric_across_dates():
    sql = (
        "SELECT FIC_MIS_DATE, SUM(N_EOP_BAL) AS TOT FROM STG_X "
        "WHERE FIC_MIS_DATE IN (:d1, :d2, :d3) GROUP BY FIC_MIS_DATE"
    )
    rows = [("2025-12-29", None), ("2025-12-30", None), ("2025-12-31", None)]
    cols = _detect_all_null_metric_columns(
        sql=sql,
        columns=["FIC_MIS_DATE", "TOT"],
        rows=rows,
        column_types_for_table={},
    )
    assert cols == ["TOT"]


# ---------------------------------------------------------------------
# _detect_all_null_metric_columns — negative cases (W86 must NOT fire)
# ---------------------------------------------------------------------

def test_no_fire_on_zero_rows():
    """Empty result is W33's territory; W86 must not double-fire."""
    sql = "SELECT SUM(N_AMOUNT) FROM STG_X WHERE FIC_MIS_DATE = :d"
    assert _detect_all_null_metric_columns(
        sql=sql,
        columns=["SUM_AMOUNT"],
        rows=[],
        column_types_for_table={},
    ) == []


def test_no_fire_when_metric_has_some_values():
    """Q7-style partial — explicit v1 scope boundary. One metric has a
    value, one is null → not all-null → no warning."""
    sql = (
        "SELECT SUM(N_ALPHA_PERCENT) AS TOTAL_ALPHA, "
        "SUM(N_BETA_FACTOR) AS TOTAL_BETA FROM STG_OP_RISK_BIA"
    )
    assert _detect_all_null_metric_columns(
        sql=sql,
        columns=["TOTAL_ALPHA", "TOTAL_BETA"],
        rows=[(1.35, None)],
        column_types_for_table={},
    ) == []


def test_no_fire_on_dimension_column_only():
    """A null V_DESCRIPTION column should never trigger W86 — that's a
    dimension column, not a metric."""
    sql = (
        "SELECT V_DESCRIPTION, N_EOP_BAL FROM STG_X "
        "WHERE FIC_MIS_DATE = :d"
    )
    rows = [(None, 100.0), (None, 200.0)]
    assert _detect_all_null_metric_columns(
        sql=sql,
        columns=["V_DESCRIPTION", "N_EOP_BAL"],
        rows=rows,
        column_types_for_table={
            "V_DESCRIPTION": {"data_type": "VARCHAR2"},
            "N_EOP_BAL": {"data_type": "NUMBER"},
        },
    ) == []


def test_no_fire_on_count_star_zero():
    """COUNT(*) returning 0 is a legitimate answer for 'how many'.
    Must not fire W86 — 0 is real data, not a null."""
    sql = "SELECT COUNT(*) AS CNT FROM STG_X WHERE V_LV_CODE = :code"
    assert _detect_all_null_metric_columns(
        sql=sql,
        columns=["CNT"],
        rows=[(0,)],
        column_types_for_table={},
    ) == []


def test_no_fire_on_skey_column_all_null():
    """A null surrogate key should not be classified as a metric."""
    sql = "SELECT N_PRODUCT_SKEY, N_EOP_BAL FROM STG_X"
    rows = [(None, 100.0), (None, 200.0)]
    assert _detect_all_null_metric_columns(
        sql=sql,
        columns=["N_PRODUCT_SKEY", "N_EOP_BAL"],
        rows=rows,
        column_types_for_table={
            "N_PRODUCT_SKEY": {"data_type": "NUMBER"},
            "N_EOP_BAL": {"data_type": "NUMBER"},
        },
    ) == []


# ---------------------------------------------------------------------
# _format_all_null_message — message rendering
# ---------------------------------------------------------------------

def test_message_lists_single_column():
    msg = _format_all_null_message(columns=["N_EOP_BAL_NPL"], row_count=100)
    assert "'N_EOP_BAL_NPL'" in msg
    assert "all 100 returned rows" in msg


def test_message_lists_two_columns():
    msg = _format_all_null_message(
        columns=["TOTAL_ALPHA_PERCENT", "TOTAL_BETA_FACTOR"], row_count=1,
    )
    assert "'TOTAL_ALPHA_PERCENT'" in msg
    assert "'TOTAL_BETA_FACTOR'" in msg
    assert "the single returned row" in msg
    assert "columns" in msg


def test_message_caps_at_three_columns_with_more():
    msg = _format_all_null_message(
        columns=["A", "B", "C", "D", "E"], row_count=10,
    )
    assert "'A'" in msg and "'B'" in msg and "'C'" in msg
    assert "'D'" not in msg and "'E'" not in msg
    assert "+2 more" in msg


def test_message_empty_columns_returns_empty():
    assert _format_all_null_message(columns=[], row_count=5) == ""


# ---------------------------------------------------------------------
# Streaming wiring — verify warnings + suspicious flip on
# ---------------------------------------------------------------------

class _FakeSchemaTools:
    def __init__(self, rows):
        self._rows = rows

    async def execute_raw(self, sql, params):
        return self._rows


async def _collect_result(stream):
    final = None
    async for kind, *payload in stream:
        if kind == "result":
            final = payload[0]
    return final


def test_stream_fires_w86_on_all_null_aggregate(monkeypatch):
    """Wiring test: aggregate returns one row of nulls → final payload
    has suspicious=True + a sanity_warnings entry tagged
    suspicious_metric_all_null."""
    agent = DataQueryAgent(
        schema_tools=_FakeSchemaTools([(None, None)]),
        redis_client=None,
        sql_guardian=SQLGuardian(),
    )
    catalog = {
        "STG_OP_RISK_BIA": {
            "N_ALPHA_PERCENT", "N_BETA_FACTOR", "FIC_MIS_DATE",
        },
    }
    monkeypatch.setattr(
        agent, "_build_schema_catalog",
        lambda schema, qualify_in_prompt=False: ("(stub)", catalog, {}),
    )

    async def fake_generate(*args, **kwargs):
        return {
            "query_kind": "AGGREGATE",
            "sql": (
                "SELECT SUM(N_ALPHA_PERCENT) AS TOTAL_ALPHA_PERCENT, "
                "SUM(N_BETA_FACTOR) AS TOTAL_BETA_FACTOR "
                "FROM STG_OP_RISK_BIA WHERE FIC_MIS_DATE = :mis_date"
            ),
            "params": {"mis_date": "2026-12-31"},
            "select_columns": ["TOTAL_ALPHA_PERCENT", "TOTAL_BETA_FACTOR"],
            "count_sql": None,
        }

    monkeypatch.setattr(agent, "_generate_sql", fake_generate)

    result = asyncio.run(_collect_result(agent.answer_stream(
        user_query="BIA op risk on 2026-12-31",
        schema="OFSMDM",
        filters={"mis_date": "2026-12-31"},
    )))

    assert result is not None
    assert result["suspicious"] is True
    warnings = result["sanity_warnings"]
    assert any(
        w.startswith("suspicious_metric_all_null:") for w in warnings
    ), f"expected W86 warning, got: {warnings}"
    assert "TOTAL_ALPHA_PERCENT" in result["suspicion_reason"]


def test_stream_no_w86_when_standard_aggregate_has_value(monkeypatch):
    """No-false-positive guard: a healthy aggregate result must NOT
    trigger W86 — sanity_warnings stays empty, suspicious stays False."""
    agent = DataQueryAgent(
        schema_tools=_FakeSchemaTools([(123456.78,)]),
        redis_client=None,
        sql_guardian=SQLGuardian(),
    )
    catalog = {
        "STG_PRODUCT_PROCESSOR": {
            "N_EOP_BAL", "V_LV_CODE", "FIC_MIS_DATE",
        },
    }
    monkeypatch.setattr(
        agent, "_build_schema_catalog",
        lambda schema, qualify_in_prompt=False: ("(stub)", catalog, {}),
    )

    async def fake_generate(*args, **kwargs):
        return {
            "query_kind": "AGGREGATE",
            "sql": (
                "SELECT SUM(N_EOP_BAL) AS TOTAL FROM STG_PRODUCT_PROCESSOR "
                "WHERE V_LV_CODE = :code AND FIC_MIS_DATE = :mis_date"
            ),
            "params": {"code": "ABL", "mis_date": "2025-12-31"},
            "select_columns": ["TOTAL"],
            "count_sql": None,
        }

    monkeypatch.setattr(agent, "_generate_sql", fake_generate)

    result = asyncio.run(_collect_result(agent.answer_stream(
        user_query="total N_EOP_BAL for V_LV_CODE='ABL' on 2025-12-31",
        schema="OFSMDM",
        filters={"mis_date": "2025-12-31"},
    )))

    assert result is not None
    assert result["suspicious"] is False
    assert not any(
        w.startswith("suspicious_metric_all_null:")
        for w in (result["sanity_warnings"] or [])
    )
