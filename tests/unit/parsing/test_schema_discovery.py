"""Unit tests for schema_discovery (W35 Phase 1 Step 3).

Locks in the contract:
- discovered_schemas() returns the schemas seen in graph:* keys, falling
  back to RECOGNIZED_SCHEMAS only when Redis is empty / unavailable.
- schema_for_function() resolves a function name to its owning schema by
  probing graph:<schema>:<FN_UPPER>.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.parsing.schema_discovery import (
    DEFAULT_FALLBACK_SCHEMA,
    discovered_schemas,
    fallback_to_default_schema,
    schema_for_function,
)
from src.parsing.manifest import RECOGNIZED_SCHEMAS
from src.parsing.serializer import to_msgpack


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_redis_with_keys(keys: list[str], graphs: dict | None = None):
    """Build a MagicMock Redis that returns *keys* from a single SCAN call.

    *graphs* maps full Redis key -> dict; get() returns msgpack-encoded
    bytes for known keys, None otherwise. Mirrors the production behavior
    used by store.get_function_graph.
    """
    storage = {k: to_msgpack(v) for k, v in (graphs or {}).items()}

    mock = MagicMock()
    # scan returns (cursor=0, keys) signalling we're done in one call.
    mock.scan.return_value = (0, keys)
    mock.get.side_effect = lambda k: storage.get(k)
    return mock


# ---------------------------------------------------------------------------
# discovered_schemas
# ---------------------------------------------------------------------------

class TestDiscoveredSchemas:
    def test_falls_back_when_redis_is_none(self):
        result = discovered_schemas(None)
        assert result == sorted(RECOGNIZED_SCHEMAS)

    def test_falls_back_when_redis_empty(self):
        mock = _make_redis_with_keys([])
        result = discovered_schemas(mock)
        assert result == sorted(RECOGNIZED_SCHEMAS)

    def test_falls_back_when_only_family_keys_present(self):
        # graph:full:* and graph:index:* are family keys — they should NOT
        # contribute schemas, because parse_graph_key rejects them.
        mock = _make_redis_with_keys([
            "graph:full:OFSMDM",
            "graph:index:OFSMDM",
            "graph:meta:OFSMDM:FN_X",
            "graph:source:OFSMDM:FN_X",
            "graph:aliases:OFSMDM",
        ])
        result = discovered_schemas(mock)
        # No per-function keys -> empty set -> bootstrap fallback.
        assert result == sorted(RECOGNIZED_SCHEMAS)

    def test_returns_schemas_from_per_function_keys(self):
        mock = _make_redis_with_keys([
            "graph:OFSMDM:FN_LOAD_OPS_RISK_DATA",
            "graph:OFSERM:CS_DEFERRED_TAX",
            "graph:OFSERM:CS_GOODWILL",
        ])
        result = discovered_schemas(mock)
        assert result == ["OFSERM", "OFSMDM"]

    def test_returns_sorted_deterministically(self):
        # Whatever order Redis returns keys in, the result is sorted.
        mock = _make_redis_with_keys([
            "graph:ZSCHEMA:FN_X",
            "graph:OFSMDM:FN_Y",
            "graph:ASCHEMA:FN_Z",
        ])
        assert discovered_schemas(mock) == ["ASCHEMA", "OFSMDM", "ZSCHEMA"]

    def test_dedupes_across_functions(self):
        mock = _make_redis_with_keys([
            "graph:OFSMDM:FN_A",
            "graph:OFSMDM:FN_B",
            "graph:OFSMDM:FN_C",
        ])
        assert discovered_schemas(mock) == ["OFSMDM"]

    def test_handles_bytes_keys_from_redis(self):
        # redis-py default returns bytes; our scan parser must accept them.
        mock = _make_redis_with_keys([
            b"graph:OFSMDM:FN_A",
            b"graph:OFSERM:FN_B",
        ])
        assert discovered_schemas(mock) == ["OFSERM", "OFSMDM"]

    def test_falls_back_on_scan_exception(self):
        mock = MagicMock()
        mock.scan.side_effect = ConnectionError("redis down")
        result = discovered_schemas(mock)
        assert result == sorted(RECOGNIZED_SCHEMAS)

    def test_pages_through_scan(self):
        # A real Redis returns keys across multiple SCAN cycles. Verify the
        # loop terminates only on cursor=0.
        mock = MagicMock()
        mock.scan.side_effect = [
            (123, ["graph:OFSMDM:FN_A"]),
            (456, [b"graph:OFSERM:FN_B"]),
            (0, ["graph:OFSMDM:FN_C"]),
        ]
        assert discovered_schemas(mock) == ["OFSERM", "OFSMDM"]


# ---------------------------------------------------------------------------
# schema_for_function
# ---------------------------------------------------------------------------

class TestSchemaForFunction:
    def test_finds_function_in_first_schema(self):
        mock = _make_redis_with_keys(
            ["graph:OFSMDM:FN_LOAD_OPS_RISK_DATA"],
            graphs={"graph:OFSMDM:FN_LOAD_OPS_RISK_DATA": {"function": "FN_LOAD"}},
        )
        assert schema_for_function("FN_LOAD_OPS_RISK_DATA", mock) == "OFSMDM"

    def test_finds_function_in_second_schema(self):
        mock = _make_redis_with_keys(
            ["graph:OFSMDM:OTHER", "graph:OFSERM:CS_DEFERRED_TAX"],
            graphs={
                "graph:OFSMDM:OTHER": {"function": "OTHER"},
                "graph:OFSERM:CS_DEFERRED_TAX": {"function": "CS_DEFERRED_TAX"},
            },
        )
        assert schema_for_function("CS_DEFERRED_TAX", mock) == "OFSERM"

    def test_returns_none_when_not_found(self):
        mock = _make_redis_with_keys(
            ["graph:OFSMDM:OTHER"],
            graphs={"graph:OFSMDM:OTHER": {"function": "OTHER"}},
        )
        assert schema_for_function("NOT_THERE", mock) is None

    def test_normalizes_function_name(self):
        # On-disk file name is mixed-case + spaces; Redis key is uppercased.
        mock = _make_redis_with_keys(
            ["graph:OFSERM:CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION"],
            graphs={
                "graph:OFSERM:CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION": {
                    "function": "x"
                }
            },
        )
        result = schema_for_function(
            "cs_deferred_tax_asset_net_of_dtl_calculation", mock
        )
        assert result == "OFSERM"

    def test_normalizes_spaces_in_function_name(self):
        # Phase 0 finding: "BASEL III..." (spaces) and "BASEL_III..."
        # (underscores) collapse to the same key.
        mock = _make_redis_with_keys(
            ["graph:OFSMDM:BASEL_III_CAPITAL"],
            graphs={"graph:OFSMDM:BASEL_III_CAPITAL": {"function": "x"}},
        )
        assert schema_for_function("BASEL III CAPITAL", mock) == "OFSMDM"

    def test_returns_none_for_empty_function_name(self):
        mock = _make_redis_with_keys([])
        assert schema_for_function("", mock) is None

    def test_returns_none_for_none_redis(self):
        assert schema_for_function("FN_X", None) is None

    def test_accepts_explicit_schemas_list(self):
        # When the caller passes schemas, schema_for_function should NOT
        # call scan() to discover them — it iterates the given list.
        graphs = {"graph:OFSMDM:FN_X": {"function": "x"}}
        storage = {k: to_msgpack(v) for k, v in graphs.items()}
        mock = MagicMock()
        mock.get.side_effect = lambda k: storage.get(k)
        # If scan were called, MagicMock would return a MagicMock cursor
        # tuple and the test would still pass — but explicit non-call is
        # the design intent. Assert via call_count.

        result = schema_for_function(
            "FN_X", mock, schemas=["OFSMDM", "OFSERM"]
        )
        assert result == "OFSMDM"
        assert mock.scan.call_count == 0


# ---------------------------------------------------------------------------
# fallback_to_default_schema
# ---------------------------------------------------------------------------

class TestFallbackToDefaultSchema:
    def test_returns_default(self):
        assert fallback_to_default_schema("test.callsite") == DEFAULT_FALLBACK_SCHEMA
        assert DEFAULT_FALLBACK_SCHEMA == "OFSMDM"

    def test_logs_warning(self):
        # The project's get_logger returns non-propagating loggers, which
        # caplog cannot capture. Patch the module-level logger directly.
        with patch("src.parsing.schema_discovery.logger") as mock_logger:
            fallback_to_default_schema("main.semantic_search", correlation_id="abc-123")
        mock_logger.warning.assert_called_once()
        # Render the warning message with the format args to assert content.
        call_args = mock_logger.warning.call_args.args
        rendered = call_args[0] % call_args[1:]
        assert "schema not resolved upstream" in rendered
        assert "main.semantic_search" in rendered
        assert "abc-123" in rendered
        assert DEFAULT_FALLBACK_SCHEMA in rendered

    def test_correlation_id_optional(self):
        with patch("src.parsing.schema_discovery.logger") as mock_logger:
            fallback_to_default_schema("test.callsite")
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args.args
        rendered = call_args[0] % call_args[1:]
        assert "test.callsite" in rendered
        assert "?" in rendered  # missing correlation_id is rendered as "?"

    def test_used_as_or_rhs_does_not_fire_when_lhs_truthy(self):
        # The intended idiom: `schema = state["schema"] or fallback_to_default_schema(...)`.
        # When the lhs is truthy, the helper must NOT be called and no
        # warning must be emitted. Python `or` is short-circuit so this is
        # automatic, but we lock it in.
        with patch("src.parsing.schema_discovery.logger") as mock_logger:
            schema = "OFSERM" or fallback_to_default_schema("test.notcalled")
        assert schema == "OFSERM"
        mock_logger.warning.assert_not_called()
