"""
SchemaAwareKeyspace — centralized Redis key construction for RTIE.

Every Redis key construction in RTIE flows through this helper so that
the `<schema>` segment is never forgotten or hardcoded. Phase 1 does NOT
change the existing Redis layout — the strings produced here mirror what
`src/parsing/store.py:REDIS_KEYS` and `src/tools/cache_tools.py:CacheClient`
already build via f-strings; this module just gives them a single source
of truth.

Usage:

    from src.parsing.keyspace import SchemaAwareKeyspace as K

    K.graph_key("OFSERM", "CS_DEFERRED_TAX")     # "graph:OFSERM:CS_DEFERRED_TAX"
    K.graph_index_key("OFSERM")                   # "graph:index:OFSERM"
    K.parse_graph_key("graph:OFSERM:CS_X")        # ("OFSERM", "CS_X")
    K.parse_graph_key("graph:meta:OFSERM:CS_X")   # None (not the per-fn key)
    K.normalize_function_name("CS_Deferred_Tax")  # "CS_DEFERRED_TAX"
"""

from __future__ import annotations

import re
from typing import Optional


_WHITESPACE_RE = re.compile(r"\s+")

# Reserved second segments under the `graph:*` namespace. These disambiguate
# the per-function key `graph:<schema>:<fn>` from family keys like
# `graph:meta:<schema>:<fn>`, `graph:full:<schema>`, `graph:source:<schema>:<fn>`,
# etc. `parse_graph_key` uses this set to reject non-per-function keys.
#   - meta     : graph:meta:<schema>:<fn>      (parse metadata)
#   - full     : graph:full:<schema>           (aggregated cross-fn graph)
#   - index    : graph:index:<schema>          (column -> fn index)
#   - aliases  : graph:aliases:<schema>        (alias map)
#   - source   : graph:source:<schema>:<fn>    (raw SQL lines)
#   - origins  : graph:origins:<schema>[:..]   (Phase 2: origins catalog)
#   - literal  : graph:literal:<schema>:<id>   (Phase 5: business identifier index)
_RESERVED_GRAPH_SUBKEYS = frozenset(
    {"meta", "full", "index", "aliases", "source", "origins", "literal"}
)


def _require_nonempty(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string (got {value!r})")
    return value


class SchemaAwareKeyspace:
    """All Redis key construction goes through this helper.

    No string formatting of Redis keys outside this class.

    Defaults for `schema` are deliberately FORBIDDEN at this layer — every
    method requires the caller to pass `schema` explicitly. Callers that
    "don't know" the schema must resolve it upstream (see schema_discovery
    in Step 3) rather than silently falling back to a default.
    """

    @staticmethod
    def graph_key(schema: str, function_name: str) -> str:
        """Per-function graph key. e.g. ``graph:OFSERM:CS_DEFERRED_TAX_...``"""
        _require_nonempty(schema, "schema")
        _require_nonempty(function_name, "function_name")
        return f"graph:{schema}:{function_name}"

    @staticmethod
    def graph_index_key(schema: str) -> str:
        """Column -> function index. e.g. ``graph:index:OFSERM``"""
        _require_nonempty(schema, "schema")
        return f"graph:index:{schema}"

    @staticmethod
    def graph_full_key(schema: str) -> str:
        """Aggregated cross-function graph. e.g. ``graph:full:OFSERM``"""
        _require_nonempty(schema, "schema")
        return f"graph:full:{schema}"

    @staticmethod
    def source_key(schema: str, function_name: str) -> str:
        """Raw SQL source key. e.g. ``graph:source:OFSERM:CS_DEFERRED_TAX_...``"""
        _require_nonempty(schema, "schema")
        _require_nonempty(function_name, "function_name")
        return f"graph:source:{schema}:{function_name}"

    @staticmethod
    def graph_aliases_key(schema: str) -> str:
        """Alias map key. e.g. ``graph:aliases:OFSERM``"""
        _require_nonempty(schema, "schema")
        return f"graph:aliases:{schema}"

    @staticmethod
    def graph_prefix(schema: str) -> str:
        """Per-function graph key prefix (no trailing wildcard).

        e.g. ``graph:OFSERM:`` — used when iterating SCAN results to strip
        the prefix off raw keys and recover the function name. For the SCAN
        pattern itself (with trailing ``*``), use :meth:`graph_scan_pattern`.
        """
        _require_nonempty(schema, "schema")
        return f"graph:{schema}:"

    @staticmethod
    def graph_scan_pattern(schema: str) -> str:
        """SCAN/glob pattern for per-function graph keys.

        e.g. ``graph:OFSERM:*`` — passed to ``redis_client.keys()`` or
        ``redis_client.scan(match=...)`` to enumerate all per-function graph
        keys for one schema. Family keys with a different second segment
        (``graph:meta:<schema>:...``, ``graph:full:<schema>``,
        ``graph:index:<schema>``, ``graph:aliases:<schema>``,
        ``graph:source:<schema>:...``) do NOT match this pattern.
        """
        _require_nonempty(schema, "schema")
        return f"graph:{schema}:*"

    @staticmethod
    def origins_key(schema: str, *parts: str) -> str:
        """Per-schema origins catalog key.

        Layout: ``graph:origins:<schema>[:<part>...]``. Phase 1 establishes
        the namespace; Phase 2 owns the actual content shape. Today the
        origins catalog lives in-process only (see `phase2/origins_catalog.py`),
        so no caller exists yet — but introducing the namespace now keeps
        future writes consistent with the rest of the `graph:*` family.
        """
        _require_nonempty(schema, "schema")
        for part in parts:
            _require_nonempty(part, "origins-key part")
        base = f"graph:origins:{schema}"
        if parts:
            return base + ":" + ":".join(parts)
        return base

    @staticmethod
    def logic_cache_key(schema: str, function_name: str) -> str:
        """Source-cache key written by `MetadataInterpreter.fetch_logic`.

        e.g. ``rtie:logic:OFSERM:CS_DEFERRED_TAX_...``. The ``rtie`` prefix
        comes from `CacheClient(key_prefix="rtie")` and is the live convention
        in `src/tools/cache_tools.py`.
        """
        _require_nonempty(schema, "schema")
        _require_nonempty(function_name, "function_name")
        return f"rtie:logic:{schema}:{function_name}"

    @staticmethod
    def parse_graph_key(key: str) -> Optional[tuple[str, str]]:
        """Reverse parser for the per-function graph key.

        ``graph:OFSERM:CS_X`` -> ``("OFSERM", "CS_X")``. Returns ``None``
        for any non-graph key, for the family keys (``graph:meta:*``,
        ``graph:full:*``, ``graph:index:*``, ``graph:source:*``,
        ``graph:aliases:*``, ``graph:origins:*``, ``graph:literal:*``),
        and for malformed inputs.
        """
        if not isinstance(key, str) or not key.startswith("graph:"):
            return None
        # split into at most 3 parts so a function name containing ':' (none
        # do today, but defensive) ends up as the trailing segment intact.
        parts = key.split(":", 2)
        if len(parts) != 3:
            return None
        _, schema_seg, fn_seg = parts
        if not schema_seg or not fn_seg:
            return None
        if schema_seg in _RESERVED_GRAPH_SUBKEYS:
            return None
        return (schema_seg, fn_seg)

    @staticmethod
    def normalize_function_name(name: str) -> str:
        """Canonical Redis-key form for a function name.

        Strip surrounding whitespace, collapse internal whitespace runs to
        a single ``_``, then uppercase. Centralizes the space-vs-underscore
        duplicate finding from Phase 0 (diagnostic Section 2.5 issue #2):
        ``"BASEL III CAPITAL"`` and ``"BASEL_III_CAPITAL"`` MUST collapse
        to the same key, otherwise the loader writes both variants and the
        column index ends up looking up the wrong one.

        Raises ``ValueError`` for non-strings and for empty / whitespace-only
        names — the loader should never see those, and silently producing
        an empty key would corrupt Redis.
        """
        if not isinstance(name, str):
            raise ValueError(
                f"function name must be a string (got {type(name).__name__})"
            )
        stripped = name.strip()
        if not stripped:
            raise ValueError("function name must be a non-empty string")
        return _WHITESPACE_RE.sub("_", stripped).upper()
