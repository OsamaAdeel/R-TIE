"""Phase 8 — cache unification (rtie:logic: retirement).

Three contracts:

1. ``CacheManager`` slash-command handlers:
   - ``refresh_logic_cache`` / ``refresh_all_logic_cache`` / ``clear_cache_entry``
     return structured ``status="deprecated"`` payloads instructing the
     user to FLUSHDB + restart. They never touch Redis.
   - ``list_cached_objects`` enumerates ``graph:source:<schema>:*``.
   - ``get_cache_status(object_name, schema)`` reports per-function
     presence of ``graph:source:`` and ``graph:`` keys.
   - ``get_cache_status(None, schema)`` reports aggregate counts.

2. ``Validator.cache_validator`` is a no-op. It sets
   ``state["cache_stale"] = False`` unconditionally and never awaits
   the async cache_client. Drift detection is deferred to W27.

3. The Phase 8 startup-time cleanup pass scans ``rtie:logic:*`` and
   UNLINKs any matches without touching ``graph:source:*`` /
   ``graph:<schema>:*`` keys.
"""

from __future__ import annotations

from typing import Iterable
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.cache_manager import CacheManager
from src.agents.validator import Validator
from src.parsing.serializer import to_msgpack


class _FakeSyncRedis:
    """Minimal sync-Redis stand-in for cache_manager tests.

    Implements the subset of the sync API exercised by Phase 8:
    ``scan_iter(match=...)``, ``get(key)``, ``exists(key)``,
    ``unlink(*keys)``.
    """

    def __init__(self, storage: dict[str, bytes] | None = None) -> None:
        self._storage: dict[str, bytes] = dict(storage or {})

    def _decode(self, key) -> str:
        return key.decode() if isinstance(key, (bytes, bytearray)) else key

    def scan_iter(self, match: str | None = None, count: int = 500) -> Iterable[bytes]:
        if match is None:
            keys = list(self._storage.keys())
        else:
            prefix = match.rstrip("*")
            keys = [k for k in self._storage.keys() if k.startswith(prefix)]
        for k in keys:
            yield k.encode()

    def get(self, key) -> bytes | None:
        return self._storage.get(self._decode(key))

    def exists(self, key) -> int:
        return 1 if self._decode(key) in self._storage else 0

    def unlink(self, *keys) -> int:
        deleted = 0
        for k in keys:
            kk = self._decode(k)
            if kk in self._storage:
                del self._storage[kk]
                deleted += 1
        return deleted


def _make_cache_manager(graph_redis=None):
    schema_tools = MagicMock()
    cache_client = MagicMock()
    cache_client.get_json = AsyncMock(return_value=None)
    cache_client.set_json = AsyncMock(return_value=True)
    cache_client.list_keys = AsyncMock(return_value=[])
    cache_client.delete_key = AsyncMock(return_value=False)

    cm = CacheManager(schema_tools=schema_tools, cache_client=cache_client)
    if graph_redis is not None:
        cm.set_graph_redis_client(graph_redis)
    return cm, schema_tools, cache_client


# ---------------------------------------------------------------------------
# Deprecation stubs — refresh / refresh-all / clear
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_logic_cache_returns_deprecated_stub():
    cm, schema_tools, cache_client = _make_cache_manager()
    result = await cm.refresh_logic_cache("FN_X", "OFSERM")

    assert result["status"] == "deprecated"
    assert result["object_name"] == "FN_X"
    assert result["schema"] == "OFSERM"
    assert "FLUSHDB" in result["message"]

    schema_tools.execute_query.assert_not_called()
    cache_client.set_json.assert_not_awaited()
    cache_client.get_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_all_logic_cache_returns_deprecated_stub():
    cm, schema_tools, cache_client = _make_cache_manager()
    result = await cm.refresh_all_logic_cache("OFSERM")

    assert result["status"] == "deprecated"
    assert result["schema"] == "OFSERM"
    assert "FLUSHDB" in result["message"]

    schema_tools.execute_query.assert_not_called()
    cache_client.set_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_cache_entry_returns_deprecated_stub():
    cm, schema_tools, cache_client = _make_cache_manager()
    result = await cm.clear_cache_entry("FN_X", "OFSERM")

    assert result["status"] == "deprecated"
    assert "FLUSHDB" in result["message"]

    cache_client.delete_key.assert_not_awaited()


# ---------------------------------------------------------------------------
# list_cached_objects → graph:source:<schema>:*
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_cached_objects_enumerates_graph_source_keys():
    fake = _FakeSyncRedis({
        "graph:source:OFSERM:CS_A": to_msgpack(["line1\n"]),
        "graph:source:OFSERM:CS_B": to_msgpack(["line1\n"]),
        "graph:source:OFSMDM:FN_X": to_msgpack(["line1\n"]),
        "graph:OFSERM:CS_A": to_msgpack({"function": "CS_A"}),
        "rtie:logic:OFSERM:CS_LEGACY": b"junk",  # must be ignored
    })
    cm, _, cache_client = _make_cache_manager(graph_redis=fake)
    result = await cm.list_cached_objects("OFSERM")

    assert result["status"] == "ok"
    assert result["schema"] == "OFSERM"
    assert result["count"] == 2
    assert result["objects"] == ["CS_A", "CS_B"]

    # rtie:logic was retired — async cache client must not be touched.
    cache_client.list_keys.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_cached_objects_without_graph_redis_reports_unavailable():
    cm, _, cache_client = _make_cache_manager(graph_redis=None)
    result = await cm.list_cached_objects("OFSERM")

    assert result["status"] == "redis_unavailable"
    assert result["count"] == 0
    cache_client.list_keys.assert_not_awaited()


# ---------------------------------------------------------------------------
# get_cache_status — per-object and aggregate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_cache_status_per_object_reports_presence_and_lines():
    fake = _FakeSyncRedis({
        "graph:source:OFSERM:CS_A": to_msgpack(["a\n", "b\n", "c\n"]),
        "graph:OFSERM:CS_A": to_msgpack({"function": "CS_A"}),
    })
    cm, _, cache_client = _make_cache_manager(graph_redis=fake)
    result = await cm.get_cache_status("CS_A", "OFSERM")

    assert result["status"] == "ok"
    assert result["object_name"] == "CS_A"
    assert result["graph_source_present"] is True
    assert result["graph_source_lines"] == 3
    assert result["graph_present"] is True
    cache_client.get_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_cache_status_per_object_handles_missing():
    fake = _FakeSyncRedis({})
    cm, _, _ = _make_cache_manager(graph_redis=fake)
    result = await cm.get_cache_status("NOPE", "OFSERM")

    assert result["status"] == "ok"
    assert result["graph_source_present"] is False
    assert result["graph_source_lines"] == 0
    assert result["graph_present"] is False


@pytest.mark.asyncio
async def test_get_cache_status_aggregate_counts_namespaces():
    fake = _FakeSyncRedis({
        "graph:source:OFSERM:CS_A": to_msgpack(["a\n"]),
        "graph:source:OFSERM:CS_B": to_msgpack(["b\n"]),
        "graph:source:OFSMDM:FN_X": to_msgpack(["x\n"]),  # different schema
        "graph:OFSERM:CS_A": to_msgpack({}),
        "graph:OFSERM:CS_B": to_msgpack({}),
        "graph:OFSERM:CS_C": to_msgpack({}),
        "graph:full:OFSERM": to_msgpack({}),  # family key, must not match
        "graph:index:OFSERM": to_msgpack({}),  # family key, must not match
    })
    cm, _, _ = _make_cache_manager(graph_redis=fake)
    result = await cm.get_cache_status(None, "OFSERM")

    assert result["status"] == "ok"
    assert result["schema"] == "OFSERM"
    assert result["graph_source_count"] == 2
    # graph:OFSERM:* matches the three per-function keys. Family keys
    # like graph:full:OFSERM and graph:index:OFSERM have a different
    # second segment so they don't share the graph:OFSERM: prefix.
    assert result["graph_count"] == 3


@pytest.mark.asyncio
async def test_get_cache_status_without_graph_redis_reports_unavailable():
    cm, _, cache_client = _make_cache_manager(graph_redis=None)
    result = await cm.get_cache_status("CS_A", "OFSERM")

    assert result["status"] == "redis_unavailable"
    cache_client.get_json.assert_not_awaited()


# ---------------------------------------------------------------------------
# Validator.cache_validator — no-op contract
# ---------------------------------------------------------------------------


def _make_validator():
    schema_tools = MagicMock()
    schema_tools.execute_query = AsyncMock(return_value=[])
    cache_client = MagicMock()
    cache_client.get_json = AsyncMock(return_value={"oracle_last_ddl_time": "x"})
    return Validator(schema_tools=schema_tools, cache_client=cache_client), \
        schema_tools, cache_client


@pytest.mark.asyncio
async def test_cache_validator_is_noop_on_cache_hit():
    validator, schema_tools, cache_client = _make_validator()
    state = {
        "schema": "OFSERM",
        "object_name": "CS_A",
        "cache_hit": True,
        "cache_stale": True,  # pre-existing flag should be reset to False
    }
    result = await validator.cache_validator(state)

    assert result["cache_stale"] is False
    schema_tools.execute_query.assert_not_awaited()
    cache_client.get_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_validator_is_noop_on_cache_miss():
    validator, schema_tools, cache_client = _make_validator()
    state = {
        "schema": "OFSMDM",
        "object_name": "FN_X",
        "cache_hit": False,
    }
    result = await validator.cache_validator(state)

    assert result["cache_stale"] is False
    schema_tools.execute_query.assert_not_awaited()
    cache_client.get_json.assert_not_awaited()


# ---------------------------------------------------------------------------
# Startup-time legacy cleanup (rtie:logic:* UNLINK)
# ---------------------------------------------------------------------------


def test_startup_cleanup_unlinks_rtie_logic_keys_and_preserves_others():
    """The Phase 8 cleanup pattern: SCAN rtie:logic:* + UNLINK matches.

    Mirrors the inline block in src/main.py to guard against regressions
    (e.g. accidentally widening the SCAN pattern to delete graph: keys).
    """
    fake = _FakeSyncRedis({
        "rtie:logic:OFSMDM:FN_LEGACY_1": b"x",
        "rtie:logic:OFSERM:CS_LEGACY_2": b"x",
        "rtie:logic:OFSERM:CS_LEGACY_3": b"x",
        "rtie:schema:snapshot:OFSERM": b"keep_me",  # different namespace
        "graph:source:OFSERM:CS_KEEP": to_msgpack(["a\n"]),
        "graph:OFSERM:CS_KEEP": to_msgpack({}),
    })
    legacy = list(fake.scan_iter(match="rtie:logic:*"))
    assert len(legacy) == 3

    fake.unlink(*legacy)

    # Legacy keys gone.
    assert list(fake.scan_iter(match="rtie:logic:*")) == []
    # Schema snapshot, graph:source:, and graph: keys all preserved.
    assert fake.exists("rtie:schema:snapshot:OFSERM") == 1
    assert fake.exists("graph:source:OFSERM:CS_KEEP") == 1
    assert fake.exists("graph:OFSERM:CS_KEEP") == 1
