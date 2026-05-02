"""Unit tests for src.agents.data_query + src.tools.sql_guardian.

Covers the fix for Q9's twin bugs:
  * Bug A — LLM column hallucination (column referenced against wrong
    table).
  * Bug B — tenacity RetryError hiding the real ORA-XXXXX code.

Test letters (A–G) map to the plan in the PR prompt.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from tenacity import RetryError

import oracledb

from src.agents.data_query import (
    DataQueryAgent,
    _extract_oracle_error,
    _sanitize_oracle_error,
    _unwrap_retry_error,
)
from src.tools.sql_guardian import (
    ColumnResidencyError,
    GuardianRejectionError,
    SQLGuardian,
)


# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

@pytest.fixture
def catalog() -> dict[str, set[str]]:
    """The minimal two-table schema that reproduces the Q9 bug shape."""
    return {
        "STG_GL_DATA": {
            "V_GL_CODE", "V_LV_CODE", "V_BRANCH_CODE",
            "N_AMOUNT_LCY", "N_AMOUNT_ACY", "V_DATA_ORIGIN",
            "FIC_MIS_DATE",
        },
        "STG_PRODUCT_PROCESSOR": {
            "V_ACCOUNT_NUMBER", "V_GL_CODE", "V_LV_CODE",
            "V_BRANCH_CODE", "N_EOP_BAL", "N_LCY_AMT",
            "F_EXPOSURE_ENABLED_IND", "V_DATA_ORIGIN",
            "FIC_MIS_DATE",
        },
    }


@pytest.fixture
def guardian() -> SQLGuardian:
    return SQLGuardian()


@pytest.fixture
def sample_graph() -> dict[str, Any]:
    """A function graph with two INSERT nodes — one per table — so
    `_build_schema_catalog` has something to attribute."""
    return {
        "function": "FN_FIXTURE",
        "nodes": [
            {
                "id": "FN_FIXTURE_N1",
                "type": "INSERT",
                "target_table": "STG_GL_DATA",
                "source_tables": [],
                "column_maps": {
                    "columns": [
                        "V_GL_CODE", "V_LV_CODE", "V_BRANCH_CODE",
                        "N_AMOUNT_LCY", "N_AMOUNT_ACY", "V_DATA_ORIGIN",
                        "FIC_MIS_DATE",
                    ],
                },
            },
            {
                "id": "FN_FIXTURE_N2",
                "type": "INSERT",
                "target_table": "STG_PRODUCT_PROCESSOR",
                "source_tables": [],
                "column_maps": {
                    "columns": [
                        "V_ACCOUNT_NUMBER", "V_GL_CODE", "V_LV_CODE",
                        "V_BRANCH_CODE", "N_EOP_BAL", "N_LCY_AMT",
                        "F_EXPOSURE_ENABLED_IND", "V_DATA_ORIGIN",
                        "FIC_MIS_DATE",
                    ],
                },
            },
        ],
        "edges": [],
    }


class _FakeRedis:
    """Minimal fake that simulates the two Redis methods the catalog
    builder touches: `keys(pattern)` and `get(key)`."""

    def __init__(self, storage: dict[bytes, bytes]) -> None:
        self._storage = storage

    def keys(self, pattern: str) -> list[bytes]:
        # Pattern is `graph:SCHEMA:*`; translate the trailing `*` to any-suffix.
        prefix = pattern[:-1] if pattern.endswith("*") else pattern
        return [k for k in self._storage.keys() if k.decode().startswith(prefix)]

    def get(self, key) -> bytes | None:
        if isinstance(key, str):
            key = key.encode()
        return self._storage.get(key)

    def set(self, key, value) -> None:
        if isinstance(key, str):
            key = key.encode()
        self._storage[key] = value


@pytest.fixture
def agent_with_graph(guardian, sample_graph) -> DataQueryAgent:
    """A DataQueryAgent backed by a fake Redis containing the two-function
    fixture graph for schema OFSMDM."""
    from src.parsing.store import store_function_graph

    storage: dict[bytes, bytes] = {}
    fake = _FakeRedis(storage)
    store_function_graph(fake, "OFSMDM", "FN_FIXTURE", sample_graph)

    return DataQueryAgent(
        schema_tools=MagicMock(),
        redis_client=fake,
        sql_guardian=guardian,
    )


# ---------------------------------------------------------------------
# TEST A — Schema catalog attributes columns per table
# ---------------------------------------------------------------------

def test_A_schema_catalog_attributes_columns_per_table(agent_with_graph):
    text, mapping, _column_types = agent_with_graph._build_schema_catalog("OFSMDM")

    assert set(mapping.keys()) == {"STG_GL_DATA", "STG_PRODUCT_PROCESSOR"}

    # Bug A was: V_ACCOUNT_NUMBER leaked into STG_GL_DATA's bucket. After
    # the fix it MUST only appear under STG_PRODUCT_PROCESSOR.
    assert "V_ACCOUNT_NUMBER" in mapping["STG_PRODUCT_PROCESSOR"]
    assert "V_ACCOUNT_NUMBER" not in mapping["STG_GL_DATA"]
    assert "N_AMOUNT_LCY" in mapping["STG_GL_DATA"]
    assert "N_AMOUNT_LCY" not in mapping["STG_PRODUCT_PROCESSOR"]

    # Rendered text must contain both tables' blocks.
    assert "Table: STG_GL_DATA" in text
    assert "Table: STG_PRODUCT_PROCESSOR" in text


# ---------------------------------------------------------------------
# TEST B — Rendered prompt carries table-attributed schema, not a flat dump
# ---------------------------------------------------------------------

def test_B_prompt_has_per_table_blocks_not_flat_dump(agent_with_graph):
    text, _mapping, _column_types = agent_with_graph._build_schema_catalog("OFSMDM")

    # The flat "Known columns in schema" header was the hallucination
    # accelerant — it must not appear in the new catalog.
    assert "Known columns in schema" not in text

    # Every table block is of the shape `Table: X\nColumns: ...`.
    assert "Table: STG_GL_DATA" in text
    assert "Table: STG_PRODUCT_PROCESSOR" in text
    # Columns line immediately follows the table line.
    lines = text.splitlines()
    gl_idx = lines.index("Table: STG_GL_DATA")
    assert lines[gl_idx + 1].startswith("Columns: ")
    assert "V_ACCOUNT_NUMBER" not in lines[gl_idx + 1]


# ---------------------------------------------------------------------
# TEST C — Column residency rejects wrong-table SQL
# ---------------------------------------------------------------------

def test_C_validator_rejects_column_on_wrong_table(guardian, catalog):
    # The exact hallucination from Q9 Test 1.
    bad_sql = (
        "SELECT FIC_MIS_DATE, V_ACCOUNT_NUMBER FROM STG_GL_DATA "
        "WHERE V_GL_CODE = :gl_code AND FIC_MIS_DATE = "
        "TO_DATE(:mis_date, 'YYYY-MM-DD')"
    )
    with pytest.raises(ColumnResidencyError) as exc_info:
        guardian.validate_column_residency(bad_sql, catalog)
    assert exc_info.value.column == "V_ACCOUNT_NUMBER"
    assert "STG_GL_DATA" in exc_info.value.table


def test_C2_validator_rejects_qualified_wrong_table(guardian, catalog):
    # Qualified-but-wrong: PP.N_AMOUNT_LCY (N_AMOUNT_LCY lives on GL_DATA).
    bad = (
        "SELECT PP.V_ACCOUNT_NUMBER, PP.N_AMOUNT_LCY "
        "FROM STG_PRODUCT_PROCESSOR PP WHERE PP.V_GL_CODE = :gl_code"
    )
    with pytest.raises(ColumnResidencyError) as exc_info:
        guardian.validate_column_residency(bad, catalog)
    assert exc_info.value.column == "N_AMOUNT_LCY"
    assert exc_info.value.table == "STG_PRODUCT_PROCESSOR"


# ---------------------------------------------------------------------
# TEST D — Column residency passes for valid SQL
# ---------------------------------------------------------------------

def test_D_validator_accepts_correct_single_table_sql(guardian, catalog):
    good = (
        "SELECT SUM(N_AMOUNT_LCY) FROM STG_GL_DATA "
        "WHERE V_GL_CODE = :gl_code AND V_LV_CODE = :lv_code "
        "AND FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')"
    )
    assert guardian.validate_column_residency(good, catalog) is True


def test_D2_validator_accepts_correct_join(guardian, catalog):
    joined = (
        "SELECT PP.V_ACCOUNT_NUMBER, PP.N_EOP_BAL "
        "FROM STG_PRODUCT_PROCESSOR PP "
        "JOIN STG_GL_DATA G ON PP.V_GL_CODE = G.V_GL_CODE "
        "WHERE G.V_GL_CODE = :gl_code AND PP.FIC_MIS_DATE = "
        "TO_DATE(:mis_date, 'YYYY-MM-DD')"
    )
    assert guardian.validate_column_residency(joined, catalog) is True


def test_D3_validator_is_a_noop_when_catalog_empty(guardian):
    """Empty catalog means we can't prove anything — must not reject."""
    sql = "SELECT FOO FROM SOMETABLE"
    assert guardian.validate_column_residency(sql, {}) is True


def test_D4_string_literals_are_not_scanned_for_column_names(
    guardian, catalog,
):
    """A column name appearing inside a string literal must not trigger a
    residency violation."""
    sql = (
        "SELECT V_GL_CODE FROM STG_GL_DATA "
        "WHERE V_DATA_ORIGIN = 'V_ACCOUNT_NUMBER'"
    )
    assert guardian.validate_column_residency(sql, catalog) is True


# ---------------------------------------------------------------------
# TEST E — RetryError unwrapping
# ---------------------------------------------------------------------

def _make_retry_error(inner_exc: BaseException) -> RetryError:
    class _Attempt:
        def exception(self_inner):
            return inner_exc

    return RetryError(_Attempt())


def test_E_unwraps_retry_error_to_inner_database_error():
    class _OErr:
        full_code = "ORA-00904"
        message = '"V_ACCOUNT_NUMBER": invalid identifier'
        code = 904
        offset = 0

    db_err = oracledb.DatabaseError(_OErr())
    wrapped = _make_retry_error(db_err)

    unwrapped = _unwrap_retry_error(wrapped)
    assert isinstance(unwrapped, oracledb.DatabaseError)

    full_code, message = _extract_oracle_error(unwrapped)
    assert full_code == "ORA-00904"
    assert "V_ACCOUNT_NUMBER" in message


def test_E2_unwrap_passes_non_retry_errors_through():
    plain = RuntimeError("not a retry error")
    assert _unwrap_retry_error(plain) is plain


# ---------------------------------------------------------------------
# TEST F — Sanitization maps Oracle codes to user messages
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "code,expected_reason,must_contain",
    [
        ("ORA-00904", "column_not_found", "column"),
        ("ORA-00942", "table_not_found", "table"),
        ("ORA-01722", "type_mismatch", "data type"),
        ("ORA-01861", "type_mismatch", "data type"),
        ("ORA-12345", "other_oracle_error", "rephrasing"),
        (None, "other_oracle_error", "rephrasing"),
    ],
)
def test_F_sanitize_maps_codes_and_never_leaks_python_repr(
    code, expected_reason, must_contain,
):
    reason, user_message, suggestion = _sanitize_oracle_error(code)
    assert reason == expected_reason
    assert must_contain.lower() in user_message.lower()
    # The original Q9 failure mode was a Python Future repr bleeding into
    # the user text. None of that ever belongs in a sanitized message.
    for forbidden in ("RetryError", "Future at 0x", "state=finished",
                      "DatabaseError", "ORA-"):
        assert forbidden not in user_message
        assert forbidden not in suggestion


# ---------------------------------------------------------------------
# TEST G — Oracle error logs contain raw ORA code + message
# ---------------------------------------------------------------------

def _make_schema_tools_that_raises(exc: BaseException):
    class _Tools:
        async def execute_raw(self, sql, params):
            raise exc

    return _Tools()


def test_G_oracle_error_path_logs_raw_code_and_message(
    guardian, catalog, caplog, monkeypatch,
):
    """End-to-end: feed `answer()` a pre-validated plan that hits Oracle
    and raises a wrapped ORA-00904. Assert the raw code and message are
    in the logs AND the user-facing explanation is sanitized."""

    # Build a DatabaseError wrapped exactly the way tenacity does it.
    class _OErr:
        full_code = "ORA-00904"
        message = '"V_ACCOUNT_NUMBER": invalid identifier'
        code = 904
        offset = 0

    db_err = oracledb.DatabaseError(_OErr())
    wrapped = _make_retry_error(db_err)

    agent = DataQueryAgent(
        schema_tools=_make_schema_tools_that_raises(wrapped),
        redis_client=None,
        sql_guardian=guardian,
    )

    async def fake_generate(*args, **kwargs):
        return {
            "query_kind": "AGGREGATE",
            "sql": (
                "SELECT COUNT(*) FROM STG_GL_DATA WHERE V_GL_CODE = :gl_code "
                "AND FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')"
            ),
            "params": {"gl_code": "108012501-1107", "mis_date": "2025-12-31"},
            "select_columns": ["COUNT(*)"],
            "count_sql": None,
        }

    monkeypatch.setattr(agent, "_generate_sql", fake_generate)

    # The project's get_logger sets propagate=False; caplog hooks into
    # the root logger, so temporarily re-enable propagation and attach
    # caplog's handler directly.
    module_logger = logging.getLogger("src.agents.data_query")
    module_logger.addHandler(caplog.handler)
    module_logger.setLevel(logging.ERROR)
    caplog.set_level(logging.ERROR, logger="src.agents.data_query")

    result = asyncio.run(
        agent.answer(
            user_query="Is there any account for GL 108012501-1107?",
            schema="OFSMDM",
        )
    )

    module_logger.removeHandler(caplog.handler)

    assert result["status"] == "query_generation_error"
    assert result["reason"] == "column_not_found"
    # User-facing text is sanitized — no Python internals.
    for forbidden in ("RetryError", "Future at 0x", "ORA-"):
        assert forbidden not in result["user_message"]

    # Raw Oracle detail is in the logs for operators.
    full_log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "ORA-00904" in full_log_text
    assert "V_ACCOUNT_NUMBER" in full_log_text


# ---------------------------------------------------------------------
# W34a — DATA_QUERY progressive streaming
# ---------------------------------------------------------------------
#
# These tests verify that ``answer_stream`` emits ``("stage", ...)``
# tuples at the TRUE start of each sub-stage, and that the final
# ``("result", ...)`` payload preserves the pre-W34a return shape
# regardless of whether the path succeeds, is rejected by Guardian, or
# fails inside Oracle. The pre-W34a wrapper ``answer()`` must continue
# to return a plain dict so the existing call sites (and the tests
# above) keep working.


def _make_schema_tools_returning(rows):
    class _Tools:
        async def execute_raw(self, sql, params):
            return rows

    return _Tools()


async def _collect_stream(stream) -> list[tuple]:
    """Drain an async iterator into a list. Used to capture stage +
    result events from answer_stream() in test order."""
    out: list[tuple] = []
    async for ev in stream:
        out.append(ev)
    return out


def test_w34a_stream_emits_stage_events_in_order(guardian, monkeypatch):
    """Happy-path AGGREGATE query: answer_stream must emit, in order,
    the stage events that announce TRUE sub-stage boundaries — search,
    generating_sql, validating, fetch, explain — followed by the final
    result. Each stage event must precede the work it announces, and
    the final result preserves the pre-W34a shape."""
    agent = DataQueryAgent(
        schema_tools=_make_schema_tools_returning([(42,)]),
        redis_client=None,
        sql_guardian=guardian,
    )
    catalog = {
        "STG_GL_DATA": {"V_GL_CODE", "N_AMOUNT_LCY", "FIC_MIS_DATE"},
    }
    monkeypatch.setattr(
        agent,
        "_build_schema_catalog",
        lambda schema, qualify_in_prompt=False: ("(stub catalog)", catalog, {}),
    )

    sql_generate_called = {"before": [], "value": False}

    async def fake_generate(*args, **kwargs):
        sql_generate_called["value"] = True
        # Snapshot which stages had been yielded by the time generation runs.
        sql_generate_called["before"] = list(events)
        return {
            "query_kind": "AGGREGATE",
            "sql": "SELECT COUNT(*) FROM STG_GL_DATA WHERE V_GL_CODE = :gl",
            "params": {"gl": "X"},
            "select_columns": ["COUNT(*)"],
            "count_sql": None,
        }

    # Patch _generate_sql AFTER agent is constructed so it can capture
    # `events` lexically. We also need to capture stage events as they
    # arrive — easiest is to drain the generator with introspection.
    monkeypatch.setattr(agent, "_generate_sql", fake_generate)

    events: list[tuple] = []

    async def runner():
        async for ev in agent.answer_stream(
            user_query="how many accounts",
            schema="OFSMDM",
            filters={"mis_date": "2025-12-31"},
        ):
            events.append(ev)

    asyncio.run(runner())

    stage_names = [e[1] for e in events if e[0] == "stage"]
    # Stage order MUST be: search → generating_sql → validating → fetch
    # → explain. checking_size is ROW_LIST-only and must NOT appear here.
    assert stage_names == [
        "search",
        "generating_sql",
        "validating",
        "fetch",
        "explain",
    ], f"unexpected stage order: {stage_names}"

    # generating_sql must precede the actual SQL hop (the W34a fix).
    assert sql_generate_called["value"] is True
    pre_gen_stages = [
        e[1] for e in sql_generate_called["before"] if e[0] == "stage"
    ]
    assert "generating_sql" in pre_gen_stages, (
        "generating_sql stage event must fire BEFORE _generate_sql is called; "
        f"saw stages={pre_gen_stages}"
    )

    # Exactly one terminal result, last in the list, with pre-W34a shape.
    results = [e for e in events if e[0] == "result"]
    assert len(results) == 1
    assert events[-1][0] == "result"
    payload = results[0][1]
    assert payload["status"] == "answered"
    assert payload["query_kind"] == "AGGREGATE"
    assert "explanation" in payload
    assert payload["row_count"] == 1


def test_w34a_stream_emits_validating_before_guardian(guardian, monkeypatch):
    """Guardian-rejection path: the validating stage event must fire
    BEFORE the Guardian runs (so the user sees status while the work
    is happening), and the terminal result must still be the
    pre-W34a validation_error shape — DECLINED, no rows, no execution."""
    agent = DataQueryAgent(
        schema_tools=MagicMock(),
        redis_client=None,
        sql_guardian=guardian,
    )
    monkeypatch.setattr(
        agent,
        "_build_schema_catalog",
        lambda schema, qualify_in_prompt=False: ("(stub)", {}, {}),
    )

    async def fake_generate(*args, **kwargs):
        # Return SQL that the real Guardian will reject for being a DML.
        return {
            "query_kind": "AGGREGATE",
            "sql": "DELETE FROM STG_GL_DATA WHERE V_GL_CODE = :gl",
            "params": {"gl": "X"},
            "select_columns": [],
            "count_sql": None,
        }

    monkeypatch.setattr(agent, "_generate_sql", fake_generate)

    events: list[tuple] = []

    async def runner():
        async for ev in agent.answer_stream(
            user_query="how many",
            schema="OFSMDM",
            filters={"mis_date": "2025-12-31"},
        ):
            events.append(ev)

    asyncio.run(runner())

    stage_names = [e[1] for e in events if e[0] == "stage"]
    # validating must appear, AND must come after generating_sql but
    # before the terminal result. fetch / explain MUST NOT fire because
    # the Guardian rejected before execution.
    assert "validating" in stage_names
    assert "fetch" not in stage_names
    assert "explain" not in stage_names

    results = [e[1] for e in events if e[0] == "result"]
    assert len(results) == 1
    # Pre-W34a shape preserved: validation_error → DECLINED.
    assert results[0]["status"] == "validation_error"


def test_w34a_stream_oracle_error_still_declines(guardian, monkeypatch):
    """Oracle ORA- error inside execute: the fetch stage event must
    have fired (the user got progress feedback), and the terminal
    result must still be the pre-W34a query_generation_error shape
    with a sanitized user message."""
    class _OErr:
        full_code = "ORA-00904"
        message = '"V_ACCOUNT_NUMBER": invalid identifier'
        code = 904
        offset = 0

    db_err = oracledb.DatabaseError(_OErr())
    wrapped = _make_retry_error(db_err)

    agent = DataQueryAgent(
        schema_tools=_make_schema_tools_that_raises(wrapped),
        redis_client=None,
        sql_guardian=guardian,
    )
    monkeypatch.setattr(
        agent,
        "_build_schema_catalog",
        lambda schema, qualify_in_prompt=False: ("(stub)", {}, {}),
    )

    async def fake_generate(*args, **kwargs):
        return {
            "query_kind": "AGGREGATE",
            "sql": (
                "SELECT COUNT(*) FROM STG_GL_DATA WHERE V_GL_CODE = :gl "
                "AND FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')"
            ),
            "params": {"gl": "X", "mis_date": "2025-12-31"},
            "select_columns": ["COUNT(*)"],
            "count_sql": None,
        }

    monkeypatch.setattr(agent, "_generate_sql", fake_generate)

    events: list[tuple] = []

    async def runner():
        async for ev in agent.answer_stream(
            user_query="how many",
            schema="OFSMDM",
            filters={"mis_date": "2025-12-31"},
        ):
            events.append(ev)

    asyncio.run(runner())

    stage_names = [e[1] for e in events if e[0] == "stage"]
    assert "validating" in stage_names
    assert "fetch" in stage_names  # fired before Oracle was called
    assert "explain" not in stage_names  # explanation not built on error

    results = [e[1] for e in events if e[0] == "result"]
    assert len(results) == 1
    # Pre-W34a shape preserved.
    assert results[0]["status"] == "query_generation_error"
    # User-facing message is still sanitized — no ORA codes, no Python
    # internals.
    for forbidden in ("RetryError", "Future at 0x", "ORA-"):
        assert forbidden not in results[0].get("user_message", "")


def test_w34a_answer_wrapper_returns_plain_dict(guardian, monkeypatch):
    """The backward-compat ``answer()`` wrapper must drive the generator
    to completion and return a plain dict matching the pre-W34a shape.
    This is what the existing call sites (and tests A–G above) rely on."""
    agent = DataQueryAgent(
        schema_tools=_make_schema_tools_returning([(7,)]),
        redis_client=None,
        sql_guardian=guardian,
    )
    monkeypatch.setattr(
        agent,
        "_build_schema_catalog",
        lambda schema, qualify_in_prompt=False: ("(stub)", {}, {}),
    )

    async def fake_generate(*args, **kwargs):
        return {
            "query_kind": "AGGREGATE",
            "sql": "SELECT COUNT(*) FROM STG_GL_DATA",
            "params": {},
            "select_columns": ["COUNT(*)"],
            "count_sql": None,
        }

    monkeypatch.setattr(agent, "_generate_sql", fake_generate)

    result = asyncio.run(
        agent.answer(user_query="how many", schema="OFSMDM")
    )

    assert isinstance(result, dict)
    assert result["status"] == "answered"
    assert result["query_kind"] == "AGGREGATE"
    assert "explanation" in result
