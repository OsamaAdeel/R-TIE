"""Phase 4 — multi-schema lookup helpers in src.parsing.schema_discovery.

Tests cover the three new helpers introduced for routing:

* ``schemas_for_table`` — which schemas hold a parsed graph node
  referencing a given table.
* ``schemas_for_column`` — which schemas' ``graph:index:<schema>``
  payloads list a given column.
* ``identifier_grounded_in_any_schema`` — whether any function's source
  body in any schema contains a given identifier substring.

DATA_QUERY routing pivots on the first; VARIABLE_TRACE routing pivots on
the second; the W45 detector's multi-schema backstop pivots on the third.
"""

from __future__ import annotations

from src.parsing.schema_discovery import (
    identifier_grounded_in_any_schema,
    schemas_for_column,
    schemas_for_table,
)
from src.parsing.serializer import to_msgpack


class _FakeRedis:
    """Minimal in-memory Redis stand-in supporting get / set / keys / scan.

    Schema-aware: ``keys`` and ``scan`` honour the pattern so a fixture
    holding mixed OFSMDM and OFSERM keys serves only the matching slice.
    """

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


def _graph_with_target_table(
    function_name: str, table: str, columns: list[str] | None = None
) -> dict:
    """Fabricate a parsed-graph dict with one INSERT node into *table*."""
    columns = columns or []
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
# schemas_for_table
# ---------------------------------------------------------------------


def test_schemas_for_table_finds_owner_when_table_lives_in_one_schema():
    fake = _FakeRedis()
    fake.set(
        "graph:OFSERM:CS_DEFERRED_TAX_FN",
        to_msgpack(
            _graph_with_target_table(
                "CS_DEFERRED_TAX_FN", "FCT_STANDARD_ACCT_HEAD",
                ["N_STD_ACCT_HEAD_AMT"],
            )
        ),
    )
    fake.set(
        "graph:OFSMDM:FN_LOAD_OPS_RISK_DATA",
        to_msgpack(
            _graph_with_target_table(
                "FN_LOAD_OPS_RISK_DATA", "STG_OPS_RISK_DATA",
                ["N_ANNUAL_GROSS_INCOME"],
            )
        ),
    )

    assert schemas_for_table("FCT_STANDARD_ACCT_HEAD", fake) == ["OFSERM"]
    assert schemas_for_table("STG_OPS_RISK_DATA", fake) == ["OFSMDM"]


def test_schemas_for_table_returns_both_when_ambiguous():
    """Ambiguous = same table referenced by graphs in two schemas."""
    fake = _FakeRedis()
    fake.set(
        "graph:OFSMDM:FN_A",
        to_msgpack(_graph_with_target_table("FN_A", "STG_SHARED")),
    )
    fake.set(
        "graph:OFSERM:FN_B",
        to_msgpack(_graph_with_target_table("FN_B", "STG_SHARED")),
    )

    result = schemas_for_table("STG_SHARED", fake)
    assert sorted(result) == ["OFSERM", "OFSMDM"]


def test_schemas_for_table_returns_empty_for_unknown_table():
    fake = _FakeRedis()
    fake.set(
        "graph:OFSMDM:FN_A",
        to_msgpack(_graph_with_target_table("FN_A", "STG_SOMETHING")),
    )
    assert schemas_for_table("STG_NONEXISTENT", fake) == []


def test_schemas_for_table_is_case_insensitive():
    fake = _FakeRedis()
    fake.set(
        "graph:OFSERM:FN",
        to_msgpack(_graph_with_target_table("FN", "FCT_STANDARD_ACCT_HEAD")),
    )
    assert schemas_for_table("fct_standard_acct_head", fake) == ["OFSERM"]
    assert schemas_for_table("Fct_Standard_Acct_Head", fake) == ["OFSERM"]


def test_schemas_for_table_handles_redis_none():
    assert schemas_for_table("STG_X", None) == []


def test_schemas_for_table_finds_table_in_source_tables_too():
    """A table referenced as a source (read-from) also counts as ownership."""
    fake = _FakeRedis()
    graph = {
        "function": "FN_READS_X",
        "nodes": [
            {
                "id": "FN_READS_X_N1",
                "type": "SELECT",
                "target_table": "STG_TARGET",
                "source_tables": ["STG_SOURCE"],
                "column_maps": {},
            }
        ],
        "edges": [],
    }
    fake.set("graph:OFSMDM:FN_READS_X", to_msgpack(graph))
    assert schemas_for_table("STG_SOURCE", fake) == ["OFSMDM"]


# ---------------------------------------------------------------------
# schemas_for_column
# ---------------------------------------------------------------------


def test_schemas_for_column_finds_owner_when_column_lives_in_one_schema():
    fake = _FakeRedis()
    # Need at least one graph:* per schema for discovered_schemas to see it.
    fake.set("graph:OFSMDM:FN_OFSMDM", to_msgpack({"function": "FN_OFSMDM"}))
    fake.set("graph:OFSERM:FN_OFSERM", to_msgpack({"function": "FN_OFSERM"}))
    fake.set(
        "graph:index:OFSMDM",
        to_msgpack({"N_EOP_BAL": ["FN_OFSMDM:FN_OFSMDM_N1"]}),
    )
    fake.set(
        "graph:index:OFSERM",
        to_msgpack(
            {"N_STD_ACCT_HEAD_AMT": ["CS_DEFERRED_TAX_FN:CS_DEFERRED_TAX_FN_N1"]}
        ),
    )

    assert schemas_for_column("N_STD_ACCT_HEAD_AMT", fake) == ["OFSERM"]
    assert schemas_for_column("N_EOP_BAL", fake) == ["OFSMDM"]


def test_schemas_for_column_returns_both_when_present_in_both():
    fake = _FakeRedis()
    fake.set("graph:OFSMDM:FN_A", to_msgpack({"function": "FN_A"}))
    fake.set("graph:OFSERM:FN_B", to_msgpack({"function": "FN_B"}))
    fake.set("graph:index:OFSMDM", to_msgpack({"V_LV_CODE": ["FN_A:FN_A_N1"]}))
    fake.set("graph:index:OFSERM", to_msgpack({"V_LV_CODE": ["FN_B:FN_B_N1"]}))

    result = schemas_for_column("V_LV_CODE", fake)
    assert sorted(result) == ["OFSERM", "OFSMDM"]


def test_schemas_for_column_returns_empty_for_unknown_column():
    fake = _FakeRedis()
    fake.set("graph:OFSMDM:FN_A", to_msgpack({"function": "FN_A"}))
    fake.set("graph:index:OFSMDM", to_msgpack({"V_KNOWN": ["FN_A:FN_A_N1"]}))
    assert schemas_for_column("V_UNKNOWN", fake) == []


def test_schemas_for_column_handles_empty_index():
    """An index that exists but is empty yields no matches."""
    fake = _FakeRedis()
    fake.set("graph:OFSMDM:FN_A", to_msgpack({"function": "FN_A"}))
    fake.set("graph:index:OFSMDM", to_msgpack({}))
    assert schemas_for_column("ANY_COLUMN", fake) == []


def test_schemas_for_column_handles_redis_none():
    assert schemas_for_column("V_X", None) == []


# ---------------------------------------------------------------------
# identifier_grounded_in_any_schema
# ---------------------------------------------------------------------


def test_identifier_grounded_finds_substring_in_source_body():
    fake = _FakeRedis()
    fake.set("graph:OFSERM:FN_X", to_msgpack({"function": "FN_X"}))
    fake.set(
        "graph:source:OFSERM:FN_X",
        to_msgpack([
            "BEGIN",
            "  -- Compute CAP943 from CAP309 - CAP863",
            "  cap943 := cap309 - cap863;",
            "END;",
        ]),
    )
    assert identifier_grounded_in_any_schema("CAP943", fake) is True
    # Case-insensitive match.
    assert identifier_grounded_in_any_schema("cap943", fake) is True


def test_identifier_grounded_returns_false_when_truly_absent():
    fake = _FakeRedis()
    fake.set("graph:OFSMDM:FN_A", to_msgpack({"function": "FN_A"}))
    fake.set(
        "graph:source:OFSMDM:FN_A",
        to_msgpack(["BEGIN", "  NULL;", "END;"]),
    )
    assert identifier_grounded_in_any_schema("CAP999", fake) is False


def test_identifier_grounded_scans_across_schemas():
    """Identifier in OFSERM source while OFSMDM source omits it — still True."""
    fake = _FakeRedis()
    fake.set("graph:OFSMDM:FN_A", to_msgpack({"function": "FN_A"}))
    fake.set("graph:source:OFSMDM:FN_A", to_msgpack(["BEGIN", "  NULL;", "END;"]))
    fake.set("graph:OFSERM:FN_B", to_msgpack({"function": "FN_B"}))
    fake.set(
        "graph:source:OFSERM:FN_B",
        to_msgpack(["WHERE V_STD_ACCT_HEAD_ID = 'CAP943'"]),
    )
    assert identifier_grounded_in_any_schema("CAP943", fake) is True


def test_identifier_grounded_handles_redis_none_and_empty():
    assert identifier_grounded_in_any_schema("CAP999", None) is False
    assert identifier_grounded_in_any_schema("", _FakeRedis()) is False
