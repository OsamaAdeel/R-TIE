"""Phase 3 — schema-aware source retrieval and per-function schema resolution.

Three contracts (post-Phase-8 cache unification):

1. ``MetadataInterpreter.fetch_logic`` reads the loader-managed
   ``graph:source:<schema>:<fn>`` cache as the first and primary
   lookup. This is what closes W49 for OFSERM functions whose source
   IS retrievable.

2. ``MetadataInterpreter.fetch_multi_logic`` resolves the actual
   owning schema per result via ``schema_for_function`` and reads from
   that schema's keys. The orchestrator may have routed the request to
   OFSMDM (the default fallback); the function may live in OFSERM.

3. The fallback chain on a graph:source: miss is Oracle ALL_SOURCE →
   ``db/modules/`` on disk. The legacy ``rtie:logic:`` cache was retired
   in Phase 8 — ``cache_client.get_json`` must not be awaited from the
   source-retrieval path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.metadata_interpreter import MetadataInterpreter
from src.parsing.serializer import to_msgpack


class _FakeGraphRedis:
    """Sync Redis stand-in for the loader-cache + schema_for_function path.

    Stores ``graph:<schema>:<fn>`` and ``graph:source:<schema>:<fn>``
    keys. Honours patterns on ``keys()`` / ``scan()``.
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

    def scan(self, cursor: int = 0, match: str | None = None, count: int = 500):
        if match is None:
            return (0, [k.encode() for k in self._storage.keys()])
        if isinstance(match, bytes):
            match = match.decode()
        prefix = match.rstrip("*")
        return (
            0,
            [k.encode() for k in self._storage.keys() if k.startswith(prefix)],
        )


def _make_interpreter(graph_redis=None, cache_returns=None, oracle_returns=None):
    """Build a MetadataInterpreter with mock SchemaTools + CacheClient."""
    schema_tools = MagicMock()
    schema_tools.execute_query = AsyncMock(
        return_value=oracle_returns or []
    )

    cache_client = MagicMock()
    cache_client.get_json = AsyncMock(return_value=cache_returns)
    cache_client.set_json = AsyncMock(return_value=True)

    interpreter = MetadataInterpreter(
        schema_tools=schema_tools,
        cache_client=cache_client,
        default_schema="OFSMDM",
    )
    if graph_redis is not None:
        interpreter.set_graph_redis_client(graph_redis)
    return interpreter, schema_tools, cache_client


@pytest.mark.asyncio
async def test_fetch_logic_reads_loader_cache_first():
    """When graph:source:<schema>:<fn> exists, the chain stops there."""
    raw_lines = [
        "CREATE OR REPLACE FUNCTION OFSERM.CS_DEFERRED_TAX AS\n",
        "BEGIN\n",
        "  -- body\n",
        "  NULL;\n",
        "END;\n",
    ]
    fake = _FakeGraphRedis({
        "graph:OFSERM:CS_DEFERRED_TAX": to_msgpack({"function": "CS_DEFERRED_TAX"}),
        "graph:source:OFSERM:CS_DEFERRED_TAX": to_msgpack(raw_lines),
    })

    interpreter, schema_tools, cache_client = _make_interpreter(
        graph_redis=fake
    )

    state = {
        "schema": "OFSERM",
        "object_name": "CS_DEFERRED_TAX",
        "source_code": [],
        "cache_hit": False,
        "cache_stale": False,
    }
    result = await interpreter.fetch_logic(state)

    # Loader cache served the request. rtie:logic and Oracle were never
    # consulted.
    assert cache_client.get_json.await_count == 0
    assert schema_tools.execute_query.await_count == 0

    # Source code is in the {"line": N, "text": ...} shape downstream
    # consumers expect.
    src = result["source_code"]
    assert len(src) == 5
    assert src[0]["line"] == 1
    assert "CS_DEFERRED_TAX" in src[0]["text"]
    assert result["cache_hit"] is True


@pytest.mark.asyncio
async def test_fetch_logic_falls_through_to_oracle_when_loader_cache_empty():
    """No graph:source:<schema>:<fn> → fall through to Oracle (post-Phase-8).

    The legacy rtie:logic: cache was retired; the chain on miss is
    Oracle ALL_SOURCE → disk. The async ``cache_client.get_json`` must
    not be awaited from the source-retrieval path anymore.
    """
    fake = _FakeGraphRedis({})
    oracle_rows = [(1, "FROM_ORACLE\n")]
    interpreter, schema_tools, cache_client = _make_interpreter(
        graph_redis=fake, oracle_returns=oracle_rows
    )

    state = {
        "schema": "OFSMDM",
        "object_name": "TLX_PROV_AMT_FOR_CAP013",
        "source_code": [],
        "cache_hit": False,
        "cache_stale": False,
    }
    result = await interpreter.fetch_logic(state)

    # rtie:logic was retired — its async client must not be touched.
    assert cache_client.get_json.await_count == 0
    assert cache_client.set_json.await_count == 0
    schema_tools.execute_query.assert_awaited_once()
    assert result["source_code"][0]["text"] == "FROM_ORACLE\n"


@pytest.mark.asyncio
async def test_fetch_logic_falls_through_to_oracle_without_graph_redis():
    """No graph_redis → graph:source: lookup is skipped, Oracle is consulted."""
    oracle_rows = [(1, "FROM_ORACLE_NO_GRAPH_REDIS\n")]
    interpreter, schema_tools, cache_client = _make_interpreter(
        graph_redis=None, oracle_returns=oracle_rows
    )

    state = {
        "schema": "OFSMDM",
        "object_name": "ANY_FN",
        "source_code": [],
        "cache_hit": False,
        "cache_stale": False,
    }
    result = await interpreter.fetch_logic(state)

    assert cache_client.get_json.await_count == 0
    schema_tools.execute_query.assert_awaited_once()
    assert result["source_code"][0]["text"] == "FROM_ORACLE_NO_GRAPH_REDIS\n"


@pytest.mark.asyncio
async def test_fetch_logic_never_consults_rtie_logic():
    """rtie:logic retirement regression: get_json is not awaited from any
    path through fetch_logic, regardless of where the source ultimately
    resolves (loader-cache hit, Oracle hit, disk hit, or empty).
    """
    fake = _FakeGraphRedis({})
    interpreter, schema_tools, cache_client = _make_interpreter(
        graph_redis=fake, oracle_returns=[]
    )

    state = {
        "schema": "OFSMDM",
        "object_name": "DOES_NOT_EXIST_ANYWHERE",
        "source_code": [],
        "cache_hit": False,
        "cache_stale": False,
    }
    result = await interpreter.fetch_logic(state)

    assert cache_client.get_json.await_count == 0
    assert cache_client.set_json.await_count == 0
    assert result["source_code"] == []


@pytest.mark.asyncio
async def test_fetch_multi_logic_resolves_per_function_schema():
    """An OFSERM function name is read from OFSERM keys even when
    state["schema"] is OFSMDM (the request-routed default)."""
    ofserm_lines = ["CREATE OR REPLACE FUNCTION OFSERM.CS_DT AS BEGIN NULL; END;\n"]
    ofsmdm_lines = ["CREATE OR REPLACE FUNCTION OFSMDM.FN_OPS_RISK AS BEGIN NULL; END;\n"]
    fake = _FakeGraphRedis({
        "graph:OFSERM:CS_DT": to_msgpack({"function": "CS_DT"}),
        "graph:source:OFSERM:CS_DT": to_msgpack(ofserm_lines),
        "graph:OFSMDM:FN_OPS_RISK": to_msgpack({"function": "FN_OPS_RISK"}),
        "graph:source:OFSMDM:FN_OPS_RISK": to_msgpack(ofsmdm_lines),
    })

    interpreter, schema_tools, cache_client = _make_interpreter(
        graph_redis=fake
    )

    state = {
        "schema": "OFSMDM",  # request-routed default
        "search_results": [
            {"function_name": "CS_DT", "description": "deferred tax"},
            {"function_name": "FN_OPS_RISK", "description": "ops risk"},
        ],
    }
    result = await interpreter.fetch_multi_logic(state)
    multi = result["multi_source"]

    assert "CS_DT" in multi
    assert multi["CS_DT"]["schema"] == "OFSERM"
    assert (
        "OFSERM.CS_DT" in multi["CS_DT"]["source_code"][0]["text"]
    )

    assert "FN_OPS_RISK" in multi
    assert multi["FN_OPS_RISK"]["schema"] == "OFSMDM"
    assert (
        "OFSMDM.FN_OPS_RISK" in multi["FN_OPS_RISK"]["source_code"][0]["text"]
    )

    # Neither function went through Oracle or rtie:logic — both served
    # by the loader cache.
    assert schema_tools.execute_query.await_count == 0
    assert cache_client.get_json.await_count == 0


@pytest.mark.asyncio
async def test_fetch_multi_logic_falls_back_to_state_schema_when_unresolvable():
    """Function name absent from every loaded schema → request schema is
    used; on graph:source: miss the chain falls through to Oracle (post-
    Phase-8; rtie:logic: was retired).
    """
    fake = _FakeGraphRedis({
        # Only OFSMDM key present, but search names a function not in any schema.
        "graph:OFSMDM:KNOWN_FN": to_msgpack({"function": "KNOWN_FN"}),
    })

    oracle_rows = [(1, "FROM_ORACLE\n")]
    interpreter, schema_tools, cache_client = _make_interpreter(
        graph_redis=fake, oracle_returns=oracle_rows
    )

    state = {
        "schema": "OFSMDM",
        "search_results": [
            {"function_name": "MISSING_FN", "description": "missing"},
        ],
    }
    result = await interpreter.fetch_multi_logic(state)

    multi = result["multi_source"]
    assert "MISSING_FN" in multi
    # Resolution failed, so we fall back to the request schema.
    assert multi["MISSING_FN"]["schema"] == "OFSMDM"
    # graph:source: miss → Oracle path resolved the body.
    assert multi["MISSING_FN"]["source_code"][0]["text"] == "FROM_ORACLE\n"
    # rtie:logic was retired — its async client must not be awaited.
    assert cache_client.get_json.await_count == 0
