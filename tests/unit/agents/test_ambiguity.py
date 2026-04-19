"""Unit tests for src.agents.ambiguity and its integration with the
DataQueryAgent / ValueTracerAgent short-circuit paths.

Covers the feature/identifier-disambiguation PR: when a target column
exists on multiple tables and the user supplies only a bare identifier,
RTIE must return an informative response that teaches the user how to
rephrase, instead of silently guessing a table.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.agents.ambiguity import (
    IDENTIFIER_AMBIGUOUS_TYPE,
    build_identifier_ambiguous_response,
    detect_identifier_ambiguity,
    generate_suggestions,
    natural_word,
    render_message,
)


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture
def catalog_v_prod_code_in_both() -> dict[str, set[str]]:
    """Schema where V_PROD_CODE lives on both tables — the classic
    ambiguity case from the Q&A failure report."""
    return {
        "STG_GL_DATA": {
            "V_GL_CODE", "V_LV_CODE", "V_BRANCH_CODE", "V_PROD_CODE",
            "N_AMOUNT_LCY", "N_AMOUNT_ACY", "FIC_MIS_DATE",
        },
        "STG_PRODUCT_PROCESSOR": {
            "V_ACCOUNT_NUMBER", "V_GL_CODE", "V_LV_CODE", "V_BRANCH_CODE",
            "V_PROD_CODE", "N_EOP_BAL", "N_LCY_AMT",
            "F_EXPOSURE_ENABLED_IND", "FIC_MIS_DATE",
        },
    }


@pytest.fixture
def catalog_single_table_target() -> dict[str, set[str]]:
    """Schema where the target column (F_EXPOSURE_ENABLED_IND) only exists
    on one table — no ambiguity possible."""
    return {
        "STG_GL_DATA": {"V_GL_CODE", "N_AMOUNT_LCY", "FIC_MIS_DATE"},
        "STG_PRODUCT_PROCESSOR": {
            "V_ACCOUNT_NUMBER", "V_GL_CODE", "N_EOP_BAL",
            "F_EXPOSURE_ENABLED_IND", "FIC_MIS_DATE",
        },
    }


# ---------------------------------------------------------------------
# Detection tests
# ---------------------------------------------------------------------

def test_detect_returns_none_when_target_in_single_table(
    catalog_single_table_target,
):
    result = detect_identifier_ambiguity(
        target_column="F_EXPOSURE_ENABLED_IND",
        filters={"account_number": "ACC123", "mis_date": "2025-12-31"},
        tables_to_columns=catalog_single_table_target,
        user_query="How many accounts have F_EXPOSURE_ENABLED_IND='N' on 2025-12-31?",
    )
    assert result is None


def test_detect_returns_candidates_when_target_in_multiple_tables(
    catalog_v_prod_code_in_both,
):
    result = detect_identifier_ambiguity(
        target_column="V_PROD_CODE",
        filters={"account_number": "601013101-8604", "mis_date": "2025-12-31"},
        tables_to_columns=catalog_v_prod_code_in_both,
        user_query="what's the v_prod_code of 601013101-8604 on 2025-12-31?",
    )
    assert result is not None
    assert len(result) == 2
    tables = {c["table"] for c in result}
    assert tables == {"STG_GL_DATA", "STG_PRODUCT_PROCESSOR"}
    filter_cols = {c["filter_column"] for c in result}
    assert filter_cols == {"V_GL_CODE", "V_ACCOUNT_NUMBER"}


def test_detect_returns_none_when_no_identifier_populated(
    catalog_v_prod_code_in_both,
):
    """Without a populated identifier filter, there's nothing ambiguous."""
    result = detect_identifier_ambiguity(
        target_column="V_PROD_CODE",
        filters={"mis_date": "2025-12-31"},
        tables_to_columns=catalog_v_prod_code_in_both,
        user_query="show me v_prod_code on 2025-12-31",
    )
    assert result is None


def test_detect_returns_none_when_query_names_v_gl_code(
    catalog_v_prod_code_in_both,
):
    """User explicitly typed 'v_gl_code' — disambiguation already done."""
    result = detect_identifier_ambiguity(
        target_column="V_PROD_CODE",
        filters={"account_number": "601013101-8604", "mis_date": "2025-12-31"},
        tables_to_columns=catalog_v_prod_code_in_both,
        user_query="what's the v_prod_code of v_gl_code 601013101-8604 on 2025-12-31?",
    )
    assert result is None


def test_detect_returns_none_when_query_names_account(
    catalog_v_prod_code_in_both,
):
    """User explicitly typed 'account' — disambiguation already done."""
    result = detect_identifier_ambiguity(
        target_column="V_PROD_CODE",
        filters={"account_number": "601013101-8604", "mis_date": "2025-12-31"},
        tables_to_columns=catalog_v_prod_code_in_both,
        user_query="what's the v_prod_code for account 601013101-8604 on 2025-12-31?",
    )
    assert result is None


def test_detect_returns_none_when_target_column_missing():
    result = detect_identifier_ambiguity(
        target_column=None,
        filters={"account_number": "X"},
        tables_to_columns={"A": {"X"}, "B": {"X"}},
        user_query="hi",
    )
    assert result is None


def test_detect_returns_none_when_catalog_empty():
    result = detect_identifier_ambiguity(
        target_column="V_PROD_CODE",
        filters={"account_number": "X"},
        tables_to_columns={},
        user_query="what's the v_prod_code of X",
    )
    assert result is None


def test_detect_returns_none_when_candidates_share_filter_column():
    """If all candidate tables would use the same filter column, the
    identifier is not actually ambiguous — both tables would be queried
    the same way."""
    catalog = {
        "STG_A": {"V_GL_CODE", "V_PROD_CODE"},
        "STG_B": {"V_GL_CODE", "V_PROD_CODE"},
    }
    result = detect_identifier_ambiguity(
        target_column="V_PROD_CODE",
        filters={"gl_code": "G1"},
        tables_to_columns=catalog,
        user_query="v_prod_code of G1",
    )
    assert result is None


# ---------------------------------------------------------------------
# Suggestion generation tests
# ---------------------------------------------------------------------

def test_suggestions_match_expected_rephrasings(catalog_v_prod_code_in_both):
    candidates = detect_identifier_ambiguity(
        target_column="V_PROD_CODE",
        filters={"account_number": "601013101-8604", "mis_date": "2025-12-31"},
        tables_to_columns=catalog_v_prod_code_in_both,
        user_query="what's the v_prod_code of 601013101-8604 on 2025-12-31?",
    )
    suggestions = generate_suggestions(
        user_query="what's the v_prod_code of 601013101-8604 on 2025-12-31?",
        identifier="601013101-8604",
        candidates=candidates,
    )
    assert (
        "what's the v_prod_code of v_gl_code 601013101-8604 on 2025-12-31?"
        in suggestions
    )
    assert (
        "what's the v_prod_code for account 601013101-8604 on 2025-12-31?"
        in suggestions
    )


def test_natural_word_maps_known_columns():
    assert natural_word("V_ACCOUNT_NUMBER") == "account"
    assert natural_word("V_GL_CODE") == "v_gl_code"
    assert natural_word("V_BRANCH_CODE") == "branch"


def test_natural_word_falls_back_to_lowercased_column_name():
    """Unknown columns fall back to their technical name."""
    assert natural_word("V_SOMETHING_UNKNOWN") == "v_something_unknown"


# ---------------------------------------------------------------------
# Response shape tests
# ---------------------------------------------------------------------

def test_response_has_expected_shape(catalog_v_prod_code_in_both):
    candidates = detect_identifier_ambiguity(
        target_column="V_PROD_CODE",
        filters={"account_number": "601013101-8604", "mis_date": "2025-12-31"},
        tables_to_columns=catalog_v_prod_code_in_both,
        user_query="what's the v_prod_code of 601013101-8604 on 2025-12-31?",
    )
    response = build_identifier_ambiguous_response(
        target_column="V_PROD_CODE",
        filters={"account_number": "601013101-8604", "mis_date": "2025-12-31"},
        user_query="what's the v_prod_code of 601013101-8604 on 2025-12-31?",
        candidates=candidates,
    )

    assert response["type"] == IDENTIFIER_AMBIGUOUS_TYPE
    assert response["status"] == IDENTIFIER_AMBIGUOUS_TYPE
    assert response["target_column"] == "V_PROD_CODE"
    assert response["identifier"] == "601013101-8604"
    assert len(response["candidate_tables"]) == 2
    assert len(response["suggestions"]) == 2
    assert "V_PROD_CODE" in response["message"]
    assert "601013101-8604" in response["message"]
    assert "STG_GL_DATA" in response["message"]
    assert "STG_PRODUCT_PROCESSOR" in response["message"]
    # Each candidate entry has the documented keys.
    for entry in response["candidate_tables"]:
        assert set(entry.keys()) == {"table", "filter_column", "label"}


def test_response_is_json_serialisable(catalog_v_prod_code_in_both):
    """SSE serialisation (`json.dumps(..., default=str)`) must not crash
    on the identifier_ambiguous payload."""
    candidates = detect_identifier_ambiguity(
        target_column="V_PROD_CODE",
        filters={"account_number": "601013101-8604"},
        tables_to_columns=catalog_v_prod_code_in_both,
        user_query="what's the v_prod_code of 601013101-8604",
    )
    response = build_identifier_ambiguous_response(
        target_column="V_PROD_CODE",
        filters={"account_number": "601013101-8604"},
        user_query="what's the v_prod_code of 601013101-8604",
        candidates=candidates,
    )
    serialised = json.dumps(response, default=str)
    decoded = json.loads(serialised)
    assert decoded["type"] == IDENTIFIER_AMBIGUOUS_TYPE


def test_rendered_message_matches_documented_template(
    catalog_v_prod_code_in_both,
):
    candidates = detect_identifier_ambiguity(
        target_column="V_PROD_CODE",
        filters={"account_number": "601013101-8604", "mis_date": "2025-12-31"},
        tables_to_columns=catalog_v_prod_code_in_both,
        user_query="what's the v_prod_code of 601013101-8604 on 2025-12-31?",
    )
    suggestions = generate_suggestions(
        user_query="what's the v_prod_code of 601013101-8604 on 2025-12-31?",
        identifier="601013101-8604",
        candidates=candidates,
    )
    message = render_message(
        target_column="V_PROD_CODE",
        identifier="601013101-8604",
        candidates=candidates,
        suggestions=suggestions,
    )
    # Documented template opener.
    assert message.startswith(
        "I couldn't tell which table to query for V_PROD_CODE because "
        "601013101-8604 could be either:"
    )
    assert "- A V_GL_CODE in STG_GL_DATA" in message
    assert "- A V_ACCOUNT_NUMBER in STG_PRODUCT_PROCESSOR" in message
    assert "Try rephrasing:" in message


# ---------------------------------------------------------------------
# DataQueryAgent integration — short-circuits before SQL generation
# ---------------------------------------------------------------------

def test_data_query_short_circuits_on_ambiguity(monkeypatch):
    """When the target column is ambiguous, answer() must return an
    identifier_ambiguous response without calling the LLM or the DB."""
    from src.agents.data_query import DataQueryAgent
    from src.tools.sql_guardian import SQLGuardian

    agent = DataQueryAgent(
        schema_tools=MagicMock(),
        redis_client=None,
        sql_guardian=SQLGuardian(),
    )

    # Feed the agent a pre-built ambiguous catalog by patching the
    # catalog builder method.
    catalog = {
        "STG_GL_DATA": {
            "V_GL_CODE", "V_PROD_CODE", "N_AMOUNT_LCY", "FIC_MIS_DATE",
        },
        "STG_PRODUCT_PROCESSOR": {
            "V_ACCOUNT_NUMBER", "V_GL_CODE", "V_PROD_CODE",
            "N_EOP_BAL", "FIC_MIS_DATE",
        },
    }
    monkeypatch.setattr(
        agent,
        "_build_schema_catalog",
        lambda schema: ("(stub)", catalog),
    )

    async def fail_generate(*args, **kwargs):
        raise AssertionError(
            "SQL generation must not be called when the query is ambiguous"
        )
    monkeypatch.setattr(agent, "_generate_sql", fail_generate)

    result = asyncio.run(
        agent.answer(
            user_query="what's the v_prod_code of 601013101-8604 on 2025-12-31?",
            schema="OFSMDM",
            filters={
                "account_number": "601013101-8604",
                "mis_date": "2025-12-31",
            },
            target_variable="V_PROD_CODE",
        )
    )
    assert result["type"] == IDENTIFIER_AMBIGUOUS_TYPE
    assert result["target_column"] == "V_PROD_CODE"
    assert result["identifier"] == "601013101-8604"
    assert any(
        "v_gl_code" in s for s in result["suggestions"]
    )
    assert any(
        "account" in s for s in result["suggestions"]
    )


def test_data_query_does_not_trigger_when_target_unambiguous(monkeypatch):
    """Target column that exists on only one table must not trigger the
    ambiguity path — normal SQL generation should run."""
    from src.agents.data_query import DataQueryAgent
    from src.tools.sql_guardian import SQLGuardian

    agent = DataQueryAgent(
        schema_tools=MagicMock(),
        redis_client=None,
        sql_guardian=SQLGuardian(),
    )
    catalog = {
        "STG_PRODUCT_PROCESSOR": {
            "V_ACCOUNT_NUMBER", "N_EOP_BAL",
            "F_EXPOSURE_ENABLED_IND", "FIC_MIS_DATE",
        },
    }
    monkeypatch.setattr(
        agent,
        "_build_schema_catalog",
        lambda schema: ("(stub)", catalog),
    )

    generate_called: dict[str, bool] = {"value": False}

    async def fake_generate(*args, **kwargs):
        generate_called["value"] = True
        return {"unsupported": True, "reason": "stub"}

    monkeypatch.setattr(agent, "_generate_sql", fake_generate)

    result = asyncio.run(
        agent.answer(
            user_query="How many accounts have F_EXPOSURE_ENABLED_IND='N' on 2025-12-31?",
            schema="OFSMDM",
            filters={"mis_date": "2025-12-31"},
            target_variable="F_EXPOSURE_ENABLED_IND",
        )
    )
    assert generate_called["value"] is True
    assert result.get("type") != IDENTIFIER_AMBIGUOUS_TYPE


# ---------------------------------------------------------------------
# ValueTracerAgent integration — short-circuits before table selection
# ---------------------------------------------------------------------

def test_value_tracer_short_circuits_on_ambiguity(monkeypatch):
    """When the target column is ambiguous, trace_value() must return an
    identifier_ambiguous response without calling the RowInspector or
    any Phase 2 stage."""
    from src.agents import value_tracer as vt_module
    from src.agents.value_tracer import ValueTracerAgent
    from src.tools.sql_guardian import SQLGuardian

    agent = ValueTracerAgent(
        schema_tools=MagicMock(),
        redis_client=MagicMock(),
        sql_guardian=SQLGuardian(),
    )
    catalog = {
        "STG_GL_DATA": {"V_GL_CODE", "V_PROD_CODE", "N_AMOUNT_LCY"},
        "STG_PRODUCT_PROCESSOR": {
            "V_ACCOUNT_NUMBER", "V_GL_CODE", "V_PROD_CODE", "N_EOP_BAL",
        },
    }
    monkeypatch.setattr(
        vt_module,
        "build_tables_to_columns",
        lambda redis_client, schema: catalog,
    )

    async def fail_fetch(*args, **kwargs):
        raise AssertionError(
            "RowInspector must not be called when the query is ambiguous"
        )
    agent._row_inspector.fetch_target_row = fail_fetch  # type: ignore[assignment]

    result = asyncio.run(
        agent.trace_value(
            target_variable="V_PROD_CODE",
            filters={
                "account_number": "601013101-8604",
                "mis_date": "2025-12-31",
            },
            schema="OFSMDM",
            user_query="what's the v_prod_code of 601013101-8604 on 2025-12-31?",
        )
    )
    assert result["type"] == IDENTIFIER_AMBIGUOUS_TYPE
    assert result["target_column"] == "V_PROD_CODE"
    assert len(result["candidate_tables"]) == 2
