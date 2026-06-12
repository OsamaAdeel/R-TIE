"""Unit tests for C11 — deterministic date→skey pre-resolution.

The C11 class: fact tables (98 of 295 cataloged OFSERM tables) that key
their MIS date by N_MIS_DATE_SKEY and have no FIC_MIS_DATE column. The
date is resolved through DIM_DATES.D_CALENDAR_DATE BEFORE SQL
generation and the resolved value injected, so the LLM never chooses a
date pivot column. The "obvious" DIM_DATES.FIC_MIS_DATE pivot is the
silent-empty trap — that column is entirely NULL in the data, so a
wrong-column pivot passes every validator and returns SUM=NULL over
zero rows.

Also covers Part 2 (activation-coupled): the suspicious-result baseline
in _check_suspicious_result must be skey-aware — its hardcoded
FIC_MIS_DATE baseline threw ORA-00904 on skey-keyed tables and silently
degraded to "not suspicious".
"""

from __future__ import annotations

import json
from typing import Any, Optional

import pytest

from src.agents.data_query import (
    DataQueryAgent,
    _extract_primary_from_table_qualified,
    _table_uses_date_skey,
    reset_date_skey_cache,
    resolve_date_skey,
)
from src.tools.sql_guardian import SQLGuardian


@pytest.fixture(autouse=True)
def _clean_skey_cache():
    """The date→skey cache is module-global (mirrors W88's _SKEY_CACHE);
    isolate every test from cross-test pollution."""
    reset_date_skey_cache()
    yield
    reset_date_skey_cache()


class _ScriptedSchemaTools:
    """execute_raw returns scripted responses in order; raises when the
    scripted entry is an Exception. Records every call."""

    def __init__(self, responses: list[Any]):
        self.calls: list[tuple[str, dict]] = []
        self._responses = list(responses)

    async def execute_raw(self, sql: str, params: Optional[dict] = None):
        self.calls.append((sql, dict(params or {})))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _RoutingSchemaTools:
    """Routes execute_raw by SQL content: DIM_DATES lookups get
    *skey_rows*, everything else gets *exec_rows*."""

    def __init__(self, skey_rows=None, exec_rows=None):
        self.calls: list[tuple[str, dict]] = []
        self._skey_rows = [(20251231,)] if skey_rows is None else skey_rows
        self._exec_rows = [(100.0,)] if exec_rows is None else exec_rows

    async def execute_raw(self, sql: str, params: Optional[dict] = None):
        self.calls.append((sql, dict(params or {})))
        if "DIM_DATES" in sql:
            return self._skey_rows
        return self._exec_rows

    def dim_dates_calls(self) -> list[tuple[str, dict]]:
        return [c for c in self.calls if "DIM_DATES" in c[0]]


async def _collect(stream):
    """Drain answer_stream; return (final_result, stage_messages)."""
    final = None
    stages = []
    async for event in stream:
        if event[0] == "result":
            final = event[1]
        elif event[0] == "stage":
            stages.append((event[1], event[2]))
    return final, stages


# ---------------------------------------------------------------------
# Gate — _table_uses_date_skey
# ---------------------------------------------------------------------

_CATALOG = {
    "FCT_STANDARD_ACCT_HEAD": {
        "N_STD_ACCT_HEAD_AMT", "N_MIS_DATE_SKEY", "N_ENTITY_SKEY",
    },
    "STG_PRODUCT_PROCESSOR": {"N_EOP_BAL", "V_LV_CODE", "FIC_MIS_DATE"},
    "FCT_BOTH_COLUMNS": {"N_MIS_DATE_SKEY", "FIC_MIS_DATE", "N_AMT"},
    "FCT_EMPTY": set(),
}


def test_gate_true_for_skey_only_table():
    assert _table_uses_date_skey("FCT_STANDARD_ACCT_HEAD", _CATALOG) is True


def test_gate_false_for_direct_date_table():
    assert _table_uses_date_skey("STG_PRODUCT_PROCESSOR", _CATALOG) is False


def test_gate_false_when_table_has_both_columns():
    # A table carrying FIC_MIS_DATE directly keeps its direct filter
    # even if it also has the skey column.
    assert _table_uses_date_skey("FCT_BOTH_COLUMNS", _CATALOG) is False


def test_gate_false_for_uncataloged_or_empty_table():
    assert _table_uses_date_skey("FCT_UNKNOWN", _CATALOG) is False
    assert _table_uses_date_skey("FCT_EMPTY", _CATALOG) is False


# ---------------------------------------------------------------------
# Resolver — cardinality safety
# ---------------------------------------------------------------------

async def test_resolver_single_row_resolves():
    tools = _ScriptedSchemaTools([[(20251231,)]])
    skey = await resolve_date_skey(tools, "OFSERM", "2025-12-31")
    assert skey == 20251231
    assert len(tools.calls) == 1
    sql, params = tools.calls[0]
    assert "DISTINCT N_DATE_SKEY" in sql
    assert "OFSERM.DIM_DATES" in sql
    assert "D_CALENDAR_DATE" in sql
    assert "FIC_MIS_DATE" not in sql  # the all-NULL trap column
    assert params == {"cal_date": "2025-12-31"}


async def test_resolver_duplicate_same_skey_rows_collapse_to_one():
    # Versioned DIM_DATES rows sharing the skey must not look ambiguous.
    tools = _ScriptedSchemaTools([[(101,), (101,), (101,)]])
    assert await resolve_date_skey(tools, "OFSERM", "2025-12-31") == 101


async def test_resolver_multi_skey_tiebreaks_on_latest_record():
    # Two DISTINCT skeys → re-query with the latest-record guard.
    tools = _ScriptedSchemaTools([[(101,), (102,)], [(102,)]])
    skey = await resolve_date_skey(tools, "OFSERM", "2025-12-31")
    assert skey == 102
    assert len(tools.calls) == 2
    assert "F_LATEST_RECORD_INDICATOR = 'Y'" in tools.calls[1][0]
    # The first probe must NOT carry the guard — the local DIM_DATES
    # row has a NULL indicator and a hard ='Y' would resolve nothing.
    assert "F_LATEST_RECORD_INDICATOR" not in tools.calls[0][0]


async def test_resolver_ambiguous_after_tiebreak_returns_none():
    # Never returns more than one — ambiguity declines, never guesses.
    tools = _ScriptedSchemaTools([[(101,), (102,)], [(101,), (102,)]])
    assert await resolve_date_skey(tools, "OFSERM", "2025-12-31") is None


# ---------------------------------------------------------------------
# Resolver — not-found / error / input hygiene / cache
# ---------------------------------------------------------------------

async def test_resolver_date_not_found_returns_none():
    tools = _ScriptedSchemaTools([[]])
    assert await resolve_date_skey(tools, "OFSERM", "2025-11-30") is None


async def test_resolver_oracle_error_returns_none():
    tools = _ScriptedSchemaTools([RuntimeError("ORA-12541: no listener")])
    assert await resolve_date_skey(tools, "OFSERM", "2025-12-31") is None


async def test_resolver_rejects_non_iso_date_without_probing():
    tools = _ScriptedSchemaTools([])
    assert await resolve_date_skey(tools, "OFSERM", "31-DEC-25") is None
    assert await resolve_date_skey(tools, "OFSERM", "") is None
    assert tools.calls == []


async def test_resolver_rejects_bad_schema_token_without_probing():
    tools = _ScriptedSchemaTools([])
    assert await resolve_date_skey(tools, "X; DROP", "2025-12-31") is None
    assert tools.calls == []


async def test_resolver_caches_success_and_failure():
    # Success cached — one probe for two calls.
    tools = _ScriptedSchemaTools([[(20251231,)]])
    assert await resolve_date_skey(tools, "OFSERM", "2025-12-31") == 20251231
    assert await resolve_date_skey(tools, "OFSERM", "2025-12-31") == 20251231
    assert len(tools.calls) == 1
    # Failure cached too (W88 pattern: don't re-probe a dead Oracle).
    tools2 = _ScriptedSchemaTools([[]])
    assert await resolve_date_skey(tools2, "OFSERM", "2025-11-30") is None
    assert await resolve_date_skey(tools2, "OFSERM", "2025-11-30") is None
    assert len(tools2.calls) == 1
    # reset clears both.
    reset_date_skey_cache()
    tools3 = _ScriptedSchemaTools([[(20251231,)]])
    assert await resolve_date_skey(tools3, "OFSERM", "2025-12-31") == 20251231
    assert len(tools3.calls) == 1


# ---------------------------------------------------------------------
# answer_stream wiring — injection, decline, direct-date untouched
# ---------------------------------------------------------------------

def _make_agent(schema_tools, catalog, monkeypatch, captured):
    agent = DataQueryAgent(
        schema_tools=schema_tools,
        redis_client=None,
        sql_guardian=SQLGuardian(),
    )
    monkeypatch.setattr(
        agent, "_build_schema_catalog",
        lambda schema, qualify_in_prompt=False: ("(stub)", catalog, {}),
    )

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        hint = kwargs.get("skey_hint")
        if hint:
            return {
                "query_kind": "AGGREGATE",
                "sql": (
                    "SELECT SUM(N_STD_ACCT_HEAD_AMT) AS TOTAL_AMT "
                    "FROM OFSERM.FCT_STANDARD_ACCT_HEAD "
                    "WHERE N_MIS_DATE_SKEY = :mis_date_skey"
                ),
                "params": {"mis_date_skey": hint["skey"]},
                "select_columns": ["TOTAL_AMT"],
                "count_sql": None,
            }
        return {
            "query_kind": "AGGREGATE",
            "sql": (
                "SELECT SUM(N_EOP_BAL) AS TOTAL FROM STG_PRODUCT_PROCESSOR "
                "WHERE FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')"
            ),
            "params": {"mis_date": "2025-12-31"},
            "select_columns": ["TOTAL"],
            "count_sql": None,
        }

    monkeypatch.setattr(agent, "_generate_sql", fake_generate)
    return agent


async def test_stream_injects_resolved_skey_for_skey_table(monkeypatch):
    captured: dict = {}
    tools = _RoutingSchemaTools(exec_rows=[(5142962608685.149,)])
    agent = _make_agent(tools, _CATALOG, monkeypatch, captured)

    result, stages = await _collect(agent.answer_stream(
        user_query=(
            "What is the total N_STD_ACCT_HEAD_AMT in "
            "FCT_STANDARD_ACCT_HEAD on 2025-12-31?"
        ),
        schema="OFSERM",
        filters={"mis_date": "2025-12-31"},
    ))

    assert result is not None and result["status"] == "answered"
    hint = captured["skey_hint"]
    assert hint["skey"] == 20251231
    assert hint["mis_date"] == "2025-12-31"
    assert hint["tables"] == ["FCT_STANDARD_ACCT_HEAD"]
    assert hint["drop_mis_date"] is True
    # Exactly one DIM_DATES probe, pivoting on D_CALENDAR_DATE.
    dim_calls = tools.dim_dates_calls()
    assert len(dim_calls) == 1
    assert "D_CALENDAR_DATE" in dim_calls[0][0]
    # A stage event narrates the resolution.
    assert any("date key" in msg for _, msg in stages)
    # Executed SQL is the plain skey predicate — no DIM_DATES subquery.
    assert "N_MIS_DATE_SKEY = :mis_date_skey" in result["sql"]
    assert "DIM_DATES" not in result["sql"]
    assert "FIC_MIS_DATE" not in result["sql"]


async def test_stream_date_not_found_declines_cleanly(monkeypatch):
    captured: dict = {}
    tools = _RoutingSchemaTools(skey_rows=[])
    agent = _make_agent(tools, _CATALOG, monkeypatch, captured)

    result, _ = await _collect(agent.answer_stream(
        user_query=(
            "What is the total N_STD_ACCT_HEAD_AMT in "
            "FCT_STANDARD_ACCT_HEAD on 2025-11-30?"
        ),
        schema="OFSERM",
        filters={"mis_date": "2025-11-30"},
    ))

    # Honest decline — never falls back to an LLM-improvised pivot.
    assert result is not None and result["status"] == "unsupported"
    assert "2025-11-30" in result["summary"]
    assert "not present in the warehouse calendar" in result["summary"]
    assert result["rows"] == [] and result["sql"] is None
    assert captured == {}, "LLM generation must not run on an unresolved date"
    # Only the DIM_DATES probe hit Oracle.
    assert [c for c in tools.calls if "DIM_DATES" not in c[0]] == []


async def test_stream_direct_date_table_untouched(monkeypatch):
    captured: dict = {}
    tools = _RoutingSchemaTools(exec_rows=[(-24179237139.63,)])
    agent = _make_agent(tools, _CATALOG, monkeypatch, captured)

    result, _ = await _collect(agent.answer_stream(
        user_query="total N_EOP_BAL in STG_PRODUCT_PROCESSOR on 2025-12-31",
        schema="OFSMDM",
        filters={"mis_date": "2025-12-31"},
    ))

    assert result is not None and result["status"] == "answered"
    # Gate is false → no resolution, no hint, direct date filter kept.
    assert captured["skey_hint"] is None
    assert tools.dim_dates_calls() == []
    assert "FIC_MIS_DATE = TO_DATE(:mis_date" in result["sql"]


# ---------------------------------------------------------------------
# _generate_sql prompt injection
# ---------------------------------------------------------------------

class _FakeLLM:
    def __init__(self):
        self.messages = None

    async def ainvoke(self, messages):
        self.messages = messages

        class _Response:
            content = json.dumps({
                "query_kind": "AGGREGATE",
                "sql": "SELECT 1 FROM DUAL",
                "params": {},
                "select_columns": [],
                "count_sql": None,
            })

        return _Response()


def _agent_with_fake_llm(monkeypatch):
    fake = _FakeLLM()
    monkeypatch.setattr(
        "src.agents.data_query.create_llm", lambda **kwargs: fake,
    )
    agent = DataQueryAgent(
        schema_tools=None, redis_client=None, sql_guardian=SQLGuardian(),
    )
    return agent, fake


async def test_generate_sql_prompt_carries_resolved_value(monkeypatch):
    agent, fake = _agent_with_fake_llm(monkeypatch)
    await agent._generate_sql(
        user_query="total N_STD_ACCT_HEAD_AMT in FCT_STANDARD_ACCT_HEAD on 2025-12-31",
        filters={"mis_date": "2025-12-31", "lv_code": None},
        catalog_text="(stub catalog)",
        provider=None,
        model=None,
        skey_hint={
            "skey": 20251231,
            "mis_date": "2025-12-31",
            "tables": ["FCT_STANDARD_ACCT_HEAD"],
            "drop_mis_date": True,
        },
    )
    prompt = fake.messages[1].content
    # The resolved VALUE is in the prompt — the LLM is told the answer,
    # not asked to find a pivot.
    assert "N_MIS_DATE_SKEY = 20251231" in prompt
    assert '"mis_date_skey": 20251231' in prompt
    assert "Do NOT join or subquery DIM_DATES" in prompt
    # The calendar date is dropped from the filters block so the LLM
    # can't bolt a FIC_MIS_DATE predicate back on.
    assert '"mis_date":' not in prompt


async def test_generate_sql_prompt_unchanged_without_hint(monkeypatch):
    agent, fake = _agent_with_fake_llm(monkeypatch)
    await agent._generate_sql(
        user_query="total N_EOP_BAL for V_LV_CODE='ABL' on 2025-12-31",
        filters={"mis_date": "2025-12-31", "lv_code": "ABL"},
        catalog_text="(stub catalog)",
        provider=None,
        model=None,
        skey_hint=None,
    )
    prompt = fake.messages[1].content
    # Direct-date path: byte-identical prompt shape — mis_date still in
    # the filters block, no skey machinery anywhere.
    assert '"mis_date": "2025-12-31"' in prompt
    assert "RESOLVED DATE KEY" not in prompt
    assert "mis_date_skey" not in prompt


async def test_generate_sql_mixed_join_keeps_both_date_filters(monkeypatch):
    agent, fake = _agent_with_fake_llm(monkeypatch)
    await agent._generate_sql(
        user_query="join FCT_STANDARD_ACCT_HEAD to STG_PRODUCT_PROCESSOR on 2025-12-31",
        filters={"mis_date": "2025-12-31"},
        catalog_text="(stub catalog)",
        provider=None,
        model=None,
        skey_hint={
            "skey": 20251231,
            "mis_date": "2025-12-31",
            "tables": ["FCT_STANDARD_ACCT_HEAD"],
            "drop_mis_date": False,  # a direct-date table is also named
        },
    )
    prompt = fake.messages[1].content
    assert '"mis_date": "2025-12-31"' in prompt
    assert '"mis_date_skey": 20251231' in prompt


# ---------------------------------------------------------------------
# Part 2 — skey-aware suspicious-result baseline
# ---------------------------------------------------------------------

def _suspicion_agent(schema_tools):
    return DataQueryAgent(
        schema_tools=schema_tools,
        redis_client=None,
        sql_guardian=SQLGuardian(),
    )


async def test_suspicious_baseline_uses_skey_for_skey_table():
    tools = _ScriptedSchemaTools([[(108,)]])
    agent = _suspicion_agent(tools)
    suspicious, reason = await agent._check_suspicious_result(
        sql=(
            "SELECT SUM(N_STD_ACCT_HEAD_AMT) AS TOTAL "
            "FROM OFSERM.FCT_STANDARD_ACCT_HEAD "
            "WHERE N_MIS_DATE_SKEY = :mis_date_skey "
            "AND RTRIM(V_LV_CODE) = :lv_code"
        ),
        query_kind="AGGREGATE",
        columns=["TOTAL"],
        rows=[(None,)],
        params={"mis_date_skey": 20251231, "lv_code": "ZZZ"},
    )
    assert suspicious is True
    assert "date key 20251231" in reason
    baseline_sql, baseline_params = tools.calls[0]
    # No ORA-00904: baseline filters the skey column, never FIC_MIS_DATE,
    # and keeps the schema qualifier (the connected user's default
    # schema does not own OFSERM fact tables).
    assert baseline_sql == (
        "SELECT COUNT(*) FROM OFSERM.FCT_STANDARD_ACCT_HEAD "
        "WHERE N_MIS_DATE_SKEY = :mis_date_skey"
    )
    assert baseline_params == {"mis_date_skey": 20251231}


async def test_suspicious_not_flagged_when_only_skey_filter():
    # N_MIS_DATE_SKEY is the date filter: zero with no other predicate
    # is "no data that day", same as a bare FIC_MIS_DATE filter.
    tools = _ScriptedSchemaTools([])
    agent = _suspicion_agent(tools)
    suspicious, reason = await agent._check_suspicious_result(
        sql=(
            "SELECT SUM(N_STD_ACCT_HEAD_AMT) AS TOTAL "
            "FROM OFSERM.FCT_STANDARD_ACCT_HEAD "
            "WHERE N_MIS_DATE_SKEY = :mis_date_skey"
        ),
        query_kind="AGGREGATE",
        columns=["TOTAL"],
        rows=[(None,)],
        params={"mis_date_skey": 20251231},
    )
    assert suspicious is False and reason is None
    assert tools.calls == []  # baseline never probed


async def test_suspicious_baseline_unchanged_for_direct_date_table():
    tools = _ScriptedSchemaTools([[(669,)]])
    agent = _suspicion_agent(tools)
    suspicious, reason = await agent._check_suspicious_result(
        sql=(
            "SELECT COUNT(V_ACCOUNT_NUMBER) AS CNT "
            "FROM STG_PRODUCT_PROCESSOR "
            "WHERE RTRIM(F_EXPOSURE_ENABLED_IND) = :ind "
            "AND FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')"
        ),
        query_kind="AGGREGATE",
        columns=["CNT"],
        rows=[(0,)],
        params={"ind": "X", "mis_date": "2025-12-31"},
    )
    assert suspicious is True
    assert "2025-12-31" in reason
    baseline_sql, baseline_params = tools.calls[0]
    # Byte-identical to the pre-C11 baseline.
    assert baseline_sql == (
        "SELECT COUNT(*) FROM STG_PRODUCT_PROCESSOR "
        "WHERE FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')"
    )
    assert baseline_params == {"mis_date": "2025-12-31"}


def test_extract_primary_from_table_qualified():
    assert _extract_primary_from_table_qualified(
        "SELECT 1 FROM OFSERM.FCT_STANDARD_ACCT_HEAD WHERE 1=1"
    ) == "OFSERM.FCT_STANDARD_ACCT_HEAD"
    assert _extract_primary_from_table_qualified(
        "SELECT 1 FROM STG_PRODUCT_PROCESSOR"
    ) == "STG_PRODUCT_PROCESSOR"
    assert _extract_primary_from_table_qualified("SELECT 1") is None
