"""Phase 2 — multi-schema catalog visibility for DataQuery helpers.

Covers two contracts:

1. ``load_column_types(redis, schema=None)`` aggregates the
   ``rtie:schema:snapshot:<schema>`` payloads across every schema
   discovered in Redis. Existing single-schema callers are unaffected.

2. ``build_tables_to_columns(redis, schema=None)`` aggregates the per-
   schema graph scans into a single table → column map covering every
   discovered schema.

DATA_QUERY routing remains OFSMDM-default — these tests only verify
that the catalog data structure has cross-schema visibility. The
agent's choice of which tables to surface to the LLM is a Phase 4
question (see [docs/w35_phase2_summary.md](../../../docs/w35_phase2_summary.md)).
"""

from __future__ import annotations

import json

from src.parsing.serializer import to_msgpack
from src.agents.data_query import (
    build_tables_to_columns,
    load_column_types,
)


class _FakeRedis:
    """In-memory Redis stand-in supporting get/set/keys/scan.

    Schema-aware: ``keys`` and ``scan`` honour the pattern so a fake
    holding a mix of OFSMDM and OFSERM keys serves only the requested
    slice. ``scan`` is the contract used by ``schema_discovery``.
    """

    def __init__(self, storage: dict[str, bytes] | None = None) -> None:
        self._storage: dict[str, bytes] = dict(storage or {})

    def keys(self, pattern: str) -> list[bytes]:
        if isinstance(pattern, bytes):
            pattern = pattern.decode()
        prefix = pattern.rstrip("*")
        return [
            k.encode()
            for k in self._storage.keys()
            if k.startswith(prefix)
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


def _snapshot_payload(tables: dict[str, dict[str, dict]]) -> str:
    """Wrap a column-types fixture in the schema-snapshot envelope."""
    return json.dumps({"tables": tables})


def _graph_with_target_table(function_name: str, table: str, columns: list[str]) -> dict:
    """Fabricate a parsed-graph dict with one INSERT node into *table*."""
    return {
        "function": function_name,
        "nodes": [
            {
                "id": f"{function_name}_N1",
                "type": "INSERT",
                "target_table": table,
                "column_maps": {
                    "columns": list(columns),
                    "values": [f":{c.lower()}" for c in columns],
                    "mapping": {c: f":{c.lower()}" for c in columns},
                },
                "calculation": [],
                "conditions": [],
                "source_tables": [],
            }
        ],
        "edges": [],
    }


# ---------------------------------------------------------------------
# load_column_types — multi-schema aggregation
# ---------------------------------------------------------------------

def test_load_column_types_single_schema_unchanged():
    """Passing schema explicitly preserves Phase-1 behaviour."""
    fake = _FakeRedis()
    fake.set(
        "rtie:schema:snapshot:OFSMDM",
        _snapshot_payload({
            "STG_PRODUCT_PROCESSOR": {
                "columns": {
                    "V_LV_CODE": {
                        "data_type": "VARCHAR2", "data_length": 20,
                        "data_precision": None, "data_scale": None,
                    },
                },
            },
        }),
    )
    fake.set(
        "rtie:schema:snapshot:OFSERM",
        _snapshot_payload({
            "FCT_STANDARD_ACCT_HEAD": {
                "columns": {
                    "N_STD_ACCT_HEAD_AMT": {
                        "data_type": "NUMBER", "data_length": 22,
                        "data_precision": 15, "data_scale": 2,
                    },
                },
            },
        }),
    )

    loaded = load_column_types(fake, schema="OFSMDM")
    assert "STG_PRODUCT_PROCESSOR" in loaded
    assert "FCT_STANDARD_ACCT_HEAD" not in loaded


def test_load_column_types_aggregates_all_schemas_when_none():
    """schema=None pulls the OFSMDM and OFSERM snapshots together."""
    fake = _FakeRedis()
    fake.set(
        "rtie:schema:snapshot:OFSMDM",
        _snapshot_payload({
            "STG_PRODUCT_PROCESSOR": {
                "columns": {
                    "V_LV_CODE": {
                        "data_type": "VARCHAR2", "data_length": 20,
                        "data_precision": None, "data_scale": None,
                    },
                },
            },
        }),
    )
    fake.set(
        "rtie:schema:snapshot:OFSERM",
        _snapshot_payload({
            "FCT_STANDARD_ACCT_HEAD": {
                "columns": {
                    "N_STD_ACCT_HEAD_AMT": {
                        "data_type": "NUMBER", "data_length": 22,
                        "data_precision": 15, "data_scale": 2,
                    },
                },
            },
        }),
    )
    # discovered_schemas needs at least one graph:* key per schema to
    # see them.
    fake.set("graph:OFSMDM:FN_OFSMDM", to_msgpack({"function": "FN_OFSMDM"}))
    fake.set("graph:OFSERM:FN_OFSERM", to_msgpack({"function": "FN_OFSERM"}))

    loaded = load_column_types(fake)

    assert "STG_PRODUCT_PROCESSOR" in loaded
    assert loaded["STG_PRODUCT_PROCESSOR"]["V_LV_CODE"]["data_type"] == "VARCHAR2"

    # The OFSERM table now appears in the aggregate catalog. The agent
    # using this map can look up an OFSERM column by name even though
    # routing remains OFSMDM-default (Phase 4).
    assert "FCT_STANDARD_ACCT_HEAD" in loaded
    assert (
        loaded["FCT_STANDARD_ACCT_HEAD"]["N_STD_ACCT_HEAD_AMT"]["data_type"]
        == "NUMBER"
    )


def test_load_column_types_returns_empty_when_redis_unreachable():
    """Defensive: a None client must not raise."""
    assert load_column_types(None) == {}
    assert load_column_types(None, schema="OFSMDM") == {}


# ---------------------------------------------------------------------
# build_tables_to_columns — multi-schema aggregation
# ---------------------------------------------------------------------

def test_build_tables_to_columns_single_schema_unchanged():
    """Passing schema preserves Phase-1 single-schema scan behaviour."""
    fake = _FakeRedis()
    fake.set(
        "graph:OFSMDM:FN_OFSMDM",
        to_msgpack(_graph_with_target_table(
            "FN_OFSMDM", "STG_PRODUCT_PROCESSOR", ["V_LV_CODE", "N_EOP_BAL"]
        )),
    )
    fake.set(
        "graph:OFSERM:FN_OFSERM",
        to_msgpack(_graph_with_target_table(
            "FN_OFSERM", "FCT_STANDARD_ACCT_HEAD", ["N_STD_ACCT_HEAD_AMT"]
        )),
    )

    result = build_tables_to_columns(fake, schema="OFSMDM")
    assert "STG_PRODUCT_PROCESSOR" in result
    assert "FCT_STANDARD_ACCT_HEAD" not in result
    assert {"V_LV_CODE", "N_EOP_BAL"}.issubset(result["STG_PRODUCT_PROCESSOR"])


def test_build_tables_to_columns_aggregates_all_schemas_when_none():
    """schema=None merges the OFSMDM and OFSERM catalogs."""
    fake = _FakeRedis()
    fake.set(
        "graph:OFSMDM:FN_OFSMDM",
        to_msgpack(_graph_with_target_table(
            "FN_OFSMDM", "STG_PRODUCT_PROCESSOR", ["V_LV_CODE", "N_EOP_BAL"]
        )),
    )
    fake.set(
        "graph:OFSERM:FN_OFSERM",
        to_msgpack(_graph_with_target_table(
            "FN_OFSERM", "FCT_STANDARD_ACCT_HEAD", ["N_STD_ACCT_HEAD_AMT"]
        )),
    )

    result = build_tables_to_columns(fake)
    assert "STG_PRODUCT_PROCESSOR" in result
    assert "FCT_STANDARD_ACCT_HEAD" in result
    assert "N_STD_ACCT_HEAD_AMT" in result["FCT_STANDARD_ACCT_HEAD"]


# ---------------------------------------------------------------------
# Oracle templates remain :schema-bound (defensive, no template change)
# ---------------------------------------------------------------------

def test_oracle_templates_use_schema_bind_only():
    """Phase 2 forbids hardcoded OFSMDM in the SQL templates.

    All four templates (TMPL_FETCH_SOURCE, TMPL_OBJECT_EXISTS,
    TMPL_SCHEMA_SNAPSHOT, TMPL_BATCH_RUN_ID_LOOKUP) must drive every
    schema-scoped predicate via the ``:schema`` bind variable. A literal
    ``OFSMDM`` here would re-introduce the Phase-0 single-schema bug.
    """
    import os
    import yaml

    template_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "..", "src", "templates", "sql_templates.yaml",
    )
    with open(os.path.abspath(template_path), "r", encoding="utf-8") as f:
        templates = yaml.safe_load(f)

    for name, tmpl in templates.items():
        sql = tmpl["sql"]
        assert "OFSMDM" not in sql.upper(), (
            f"Template {name} hardcodes OFSMDM: {sql!r}"
        )
        assert "OFSERM" not in sql.upper(), (
            f"Template {name} hardcodes OFSERM: {sql!r}"
        )
