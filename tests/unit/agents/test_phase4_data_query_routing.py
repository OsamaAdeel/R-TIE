"""Phase 4 — DATA_QUERY agent multi-schema routing.

Covers the new code paths added to ``src.agents.data_query`` so that a
DATA_QUERY for an OFSERM-only table is routed to the OFSERM catalog and
emits schema-qualified SQL, while OFSMDM-only queries continue to use the
existing bare-table prompt and SQL shape.

Also exercises the table-ambiguous CLARIFICATION path that triggers when
a user-named table happens to live in two schemas at once.
"""

from __future__ import annotations

from src.agents.data_query import (
    DataQueryAgent,
    _build_table_ambiguous_response,
    _extract_user_query_tables,
    _resolve_target_schema,
)
from src.parsing.serializer import to_msgpack
from src.parsing.store import store_function_graph
from src.tools.sql_guardian import SQLGuardian


class _FakeRedis:
    def __init__(self, storage: dict[str, bytes] | None = None) -> None:
        self._storage: dict[str, bytes] = dict(storage or {})

    def keys(self, pattern: str) -> list[bytes]:
        if isinstance(pattern, bytes):
            pattern = pattern.decode()
        prefix = pattern.rstrip("*")
        return [
            k.encode() for k in self._storage.keys() if k.startswith(prefix)
        ]

    def get(self, key) -> bytes | None:
        if isinstance(key, bytes):
            key = key.decode()
        return self._storage.get(key)

    def set(self, key, value) -> None:
        if isinstance(key, bytes):
            key = key.decode()
        if isinstance(value, str):
            value = value.encode()
        self._storage[key] = value

    def scan(self, cursor: int = 0, match: str | None = None, count: int = 500):
        if match is None:
            return (0, [k.encode() for k in self._storage.keys()])
        if isinstance(match, bytes):
            match = match.decode()
        prefix = match.rstrip("*")
        matches = [
            k.encode() for k in self._storage.keys() if k.startswith(prefix)
        ]
        return (0, matches)


def _ofserm_graph(function_name: str, table: str, columns: list[str]) -> dict:
    return {
        "function": function_name,
        "nodes": [
            {
                "id": f"{function_name}_N1",
                "type": "INSERT",
                "target_table": table,
                "source_tables": [],
                "column_maps": {
                    "columns": list(columns),
                    "values": [f":{c.lower()}" for c in columns],
                    "mapping": {c: f":{c.lower()}" for c in columns},
                },
                "calculation": [],
                "conditions": [],
            }
        ],
        "edges": [],
    }


# ---------------------------------------------------------------------
# _extract_user_query_tables
# ---------------------------------------------------------------------


def test_extract_user_query_tables_picks_up_fct_and_stg():
    out = _extract_user_query_tables(
        "What is the total N_STD_ACCT_HEAD_AMT in FCT_STANDARD_ACCT_HEAD on "
        "2025-12-31 and STG_PRODUCT_PROCESSOR?"
    )
    assert "FCT_STANDARD_ACCT_HEAD" in out
    assert "STG_PRODUCT_PROCESSOR" in out


def test_extract_user_query_tables_skips_function_and_dim_dates():
    out = _extract_user_query_tables(
        "Explain FN_LOAD_OPS_RISK_DATA against DIM_DATES and TLX_PROV_AMT"
    )
    # FN_/TLX_/DIM_DATES are excluded by prefix or skip-set
    assert "FN_LOAD_OPS_RISK_DATA" not in out
    assert "TLX_PROV_AMT" not in out
    assert "DIM_DATES" not in out


def test_extract_user_query_tables_dedups_repeats():
    out = _extract_user_query_tables(
        "STG_FOO then STG_FOO again and STG_FOO."
    )
    assert out == ["STG_FOO"]


# ---------------------------------------------------------------------
# _resolve_target_schema
# ---------------------------------------------------------------------


def test_resolve_target_schema_pivots_to_ofserm_when_only_owner():
    fake = _FakeRedis()
    fake.set(
        "graph:OFSERM:CS_DEFERRED_TAX",
        to_msgpack(
            _ofserm_graph(
                "CS_DEFERRED_TAX",
                "FCT_STANDARD_ACCT_HEAD",
                ["N_STD_ACCT_HEAD_AMT"],
            )
        ),
    )

    schema, ambiguity = _resolve_target_schema(
        user_query="What is the total N_STD_ACCT_HEAD_AMT in "
        "FCT_STANDARD_ACCT_HEAD on 2025-12-31?",
        default_schema="OFSMDM",
        redis_client=fake,
    )
    assert schema == "OFSERM"
    assert ambiguity is None


def test_resolve_target_schema_keeps_default_when_no_table_named():
    fake = _FakeRedis()
    fake.set(
        "graph:OFSMDM:FN_X",
        to_msgpack(_ofserm_graph("FN_X", "STG_PRODUCT_PROCESSOR", ["N_EOP_BAL"])),
    )
    schema, ambiguity = _resolve_target_schema(
        user_query="What is the total N_EOP_BAL on 2025-12-31?",
        default_schema="OFSMDM",
        redis_client=fake,
    )
    assert schema == "OFSMDM"
    assert ambiguity is None


def test_resolve_target_schema_emits_clarification_when_ambiguous():
    fake = _FakeRedis()
    fake.set(
        "graph:OFSMDM:FN_A",
        to_msgpack(_ofserm_graph("FN_A", "STG_SHARED", ["V_X"])),
    )
    fake.set(
        "graph:OFSERM:FN_B",
        to_msgpack(_ofserm_graph("FN_B", "STG_SHARED", ["V_Y"])),
    )

    schema, ambiguity = _resolve_target_schema(
        user_query="show me STG_SHARED on 2025-12-31",
        default_schema="OFSMDM",
        redis_client=fake,
    )
    assert schema == "OFSMDM"  # falls back to default while caller emits CLAR
    assert ambiguity is not None
    assert ambiguity["table"] == "STG_SHARED"
    assert sorted(ambiguity["schemas"]) == ["OFSERM", "OFSMDM"]


def test_resolve_target_schema_unknown_table_falls_through_to_default():
    fake = _FakeRedis()
    fake.set(
        "graph:OFSMDM:FN_X",
        to_msgpack(_ofserm_graph("FN_X", "STG_PRODUCT_PROCESSOR", ["N_EOP_BAL"])),
    )
    schema, ambiguity = _resolve_target_schema(
        user_query="show me total in STG_NEVER_HEARD_OF on 2025-12-31",
        default_schema="OFSMDM",
        redis_client=fake,
    )
    # The table doesn't exist anywhere — fall back so the existing
    # "table not found" / Oracle ORA-00942 path takes over.
    assert schema == "OFSMDM"
    assert ambiguity is None


# ---------------------------------------------------------------------
# _build_table_ambiguous_response
# ---------------------------------------------------------------------


def test_build_table_ambiguous_response_has_per_schema_suggestions():
    response = _build_table_ambiguous_response(
        table="STG_SHARED",
        schemas=["OFSERM", "OFSMDM"],
        user_query="What is the total in STG_SHARED on 2025-12-31?",
    )
    assert response["type"] == "table_ambiguous"
    assert response["status"] == "table_ambiguous"
    assert response["table"] == "STG_SHARED"
    assert {c["schema"] for c in response["candidate_schemas"]} == {
        "OFSERM", "OFSMDM",
    }
    # Suggestions show schema-qualified rephrases.
    assert any("OFSERM.STG_SHARED" in s for s in response["suggestions"])
    assert any("OFSMDM.STG_SHARED" in s for s in response["suggestions"])
    assert "OFSERM.STG_SHARED" in response["message"]


# ---------------------------------------------------------------------
# _build_schema_catalog rendering — qualify flag
# ---------------------------------------------------------------------


def test_catalog_renders_qualified_table_names_for_ofserm():
    fake = _FakeRedis()
    store_function_graph(
        fake, "OFSERM", "CS_DEFERRED_TAX",
        _ofserm_graph(
            "CS_DEFERRED_TAX", "FCT_STANDARD_ACCT_HEAD",
            ["N_STD_ACCT_HEAD_AMT", "FIC_MIS_DATE"],
        ),
    )

    agent = DataQueryAgent(
        schema_tools=None,  # not used for catalog build
        redis_client=fake,
        sql_guardian=SQLGuardian(),
    )
    text, mapping, _types = agent._build_schema_catalog(
        "OFSERM", qualify_in_prompt=True
    )

    # Catalog dict keys remain BARE (so SQLGuardian residency check still
    # finds them after stripping the schema qualifier in FROM clauses).
    assert "FCT_STANDARD_ACCT_HEAD" in mapping
    # Rendered prompt text uses the qualified form.
    assert "Table: OFSERM.FCT_STANDARD_ACCT_HEAD" in text


def test_catalog_renders_bare_table_names_when_qualify_flag_false():
    """OFSMDM-default queries keep their existing prompt shape unchanged."""
    fake = _FakeRedis()
    store_function_graph(
        fake, "OFSMDM", "FN_OFSMDM",
        _ofserm_graph(
            "FN_OFSMDM", "STG_PRODUCT_PROCESSOR",
            ["V_LV_CODE", "N_EOP_BAL"],
        ),
    )

    agent = DataQueryAgent(
        schema_tools=None,
        redis_client=fake,
        sql_guardian=SQLGuardian(),
    )
    text, mapping, _types = agent._build_schema_catalog(
        "OFSMDM", qualify_in_prompt=False
    )
    assert "STG_PRODUCT_PROCESSOR" in mapping
    assert "Table: STG_PRODUCT_PROCESSOR" in text
    assert "Table: OFSMDM.STG_PRODUCT_PROCESSOR" not in text


# ---------------------------------------------------------------------
# Catalog fall-through: tables that the graph saw only as `source_tables`
# get their column set populated from the Oracle snapshot — without it
# the LLM aborts on read-only tables like OFSERM.DIM_DATES.
# ---------------------------------------------------------------------


import json  # noqa: E402  (placed here to keep the helper-area imports tight)


def _set_oracle_snapshot(fake: _FakeRedis, schema: str, tables: dict) -> None:
    """Write a `rtie:schema:snapshot:<schema>` payload mirroring what
    /refresh-schema produces from ALL_TAB_COLUMNS."""
    payload = {
        "tables": {
            tbl: {
                "columns": {
                    col: {
                        "data_type": meta.get("data_type", "VARCHAR2"),
                        "data_length": meta.get("data_length"),
                        "data_precision": meta.get("data_precision"),
                        "data_scale": meta.get("data_scale"),
                        "nullable": meta.get("nullable", "Y"),
                    }
                    for col, meta in cols.items()
                }
            }
            for tbl, cols in tables.items()
        }
    }
    fake.set(
        f"rtie:schema:snapshot:{schema}",
        json.dumps(payload).encode(),
    )


def test_catalog_falls_through_to_oracle_snapshot_for_read_only_table():
    """DIM_DATES is a `source_tables` reference in OFSERM functions but
    never an INSERT target. Pre-fix the catalog rendered
    `Columns: (none discovered in graph)` for it. The fall-through
    populates the column set from the Oracle snapshot."""
    fake = _FakeRedis()
    # OFSERM function reads DIM_DATES (no INSERT) and writes FCT_X.
    graph = {
        "function": "FN_USES_DIM_DATES",
        "nodes": [
            {
                "id": "FN_USES_DIM_DATES_N1",
                "type": "INSERT",
                "target_table": "FCT_X",
                "source_tables": ["DIM_DATES"],
                "column_maps": {
                    "columns": ["N_X"],
                    "values": [":x"],
                    "mapping": {"N_X": ":x"},
                },
            }
        ],
        "edges": [],
    }
    store_function_graph(fake, "OFSERM", "FN_USES_DIM_DATES", graph)
    _set_oracle_snapshot(
        fake,
        "OFSERM",
        {
            "DIM_DATES": {
                "N_DATE_SKEY": {"data_type": "NUMBER", "data_precision": 10},
                "D_CALENDAR_DATE": {"data_type": "DATE"},
            },
            "FCT_X": {
                "N_X": {"data_type": "NUMBER", "data_precision": 15},
            },
        },
    )

    agent = DataQueryAgent(
        schema_tools=None,
        redis_client=fake,
        sql_guardian=SQLGuardian(),
    )
    text, mapping, _types = agent._build_schema_catalog(
        "OFSERM", qualify_in_prompt=True
    )

    # The fall-through populated the column set from the Oracle snapshot,
    # so SQLGuardian residency checks see the real columns.
    assert "DIM_DATES" in mapping
    assert {"N_DATE_SKEY", "D_CALENDAR_DATE"} <= mapping["DIM_DATES"]
    # The rendered prompt shows the columns and (when types are present)
    # types — never `(none discovered in graph)` for DIM_DATES.
    assert "Columns: (none discovered in graph)" not in text
    assert "N_DATE_SKEY" in text
    assert "D_CALENDAR_DATE" in text


def test_catalog_keeps_none_discovered_when_oracle_snapshot_also_empty():
    """A table that's neither in the graph as a target nor in the
    snapshot still renders the `(none discovered in graph)` sentinel —
    the fall-through is purely additive, never invents columns."""
    fake = _FakeRedis()
    graph = {
        "function": "FN_X",
        "nodes": [
            {
                "id": "FN_X_N1",
                "type": "SELECT",
                "target_table": "FCT_OUTPUT",
                "source_tables": ["STG_NEVER_LOADED"],
                "column_maps": {},
            }
        ],
        "edges": [],
    }
    store_function_graph(fake, "OFSERM", "FN_X", graph)
    # Snapshot omits STG_NEVER_LOADED entirely.
    _set_oracle_snapshot(
        fake,
        "OFSERM",
        {
            "FCT_OUTPUT": {
                "N_VAL": {"data_type": "NUMBER"},
            },
        },
    )

    agent = DataQueryAgent(
        schema_tools=None,
        redis_client=fake,
        sql_guardian=SQLGuardian(),
    )
    text, mapping, _types = agent._build_schema_catalog(
        "OFSERM", qualify_in_prompt=True
    )

    assert "STG_NEVER_LOADED" in mapping
    assert mapping["STG_NEVER_LOADED"] == set()
    # The (none discovered in graph) sentinel still fires for tables
    # the snapshot doesn't know about either.
    assert "STG_NEVER_LOADED" in text
    assert "Columns: (none discovered in graph)" in text
