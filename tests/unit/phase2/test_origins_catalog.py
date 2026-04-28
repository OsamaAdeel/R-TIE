"""Unit tests for src.phase2.origins_catalog.

Two distinct contracts are exercised:

1. The atomic-swap invariant for the Phase-1 single-schema API. The
   per-schema entry in ``_catalogs`` must NOT be left in a half-initialised
   state when a build fails. These tests lock in that guarantee — they are
   the reason the zombie-worker bug surfaced as UNKNOWN classifications,
   and they prevent it from silently returning.

2. The Phase-2 multi-schema contract: ``build_catalog(redis)`` (no schema
   arg) iterates every discovered schema, ``to_redis`` persists a per-schema
   snapshot under ``graph:origins:<schema>:*`` keys, and reader functions
   accept an optional ``schema`` argument that scopes lookups.
"""

from __future__ import annotations

import pytest

from src.parsing.keyspace import SchemaAwareKeyspace
from src.parsing.serializer import from_msgpack
from src.phase2 import origins_catalog as oc
from src.phase2.origins_catalog import (
    BOOTSTRAP_ETL_ORIGINS,
    CatalogBuildError,
    OriginsCatalog,
    build_catalog,
    classify_origin,
    get_catalog,
    get_eop_override,
    get_known_schemas,
    is_gl_blocked,
)


# ---------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------

class _FakeRedis:
    """Minimal stand-in for a redis.Redis client.

    ``keys(pattern)`` filters the provided graph keys against the schema
    embedded in the pattern (so a multi-schema fake can serve different
    keys for OFSMDM vs OFSERM scans). ``get()`` returns msgpack-encoded
    graph payloads. ``set()`` records writes so ``to_redis`` can be
    asserted. ``fail_after`` trips an exception when the Nth ``get()``
    call is made — used to simulate a Redis outage mid-scan.
    """

    def __init__(
        self,
        graph_keys: list[str],
        graphs_by_function: dict[str, dict],
        fail_after: int | None = None,
    ):
        self._graph_keys = [k.encode() for k in graph_keys]
        self._graphs = graphs_by_function
        self._fail_after = fail_after
        self._get_count = 0
        self.writes: dict[str, bytes] = {}

    def keys(self, pattern: str):
        # The catalog scans with patterns like "graph:OFSMDM:*"; honour
        # the pattern so a fake holding a mix of schemas serves only the
        # requested slice.
        if isinstance(pattern, bytes):
            pattern = pattern.decode()
        prefix = pattern.rstrip("*")
        return [k for k in self._graph_keys if k.decode().startswith(prefix)]

    def get(self, key):
        self._get_count += 1
        if self._fail_after is not None and self._get_count > self._fail_after:
            raise ConnectionError("simulated Redis outage mid-scan")
        if isinstance(key, bytes):
            key = key.decode()
        # key looks like "graph:OFSMDM:FN_NAME"; serve only function-graph
        # keys through get_function_graph (which itself calls redis.get).
        if not key.startswith("graph:"):
            return None
        parts = key.split(":")
        if len(parts) < 3:
            return None
        # parts = ["graph", "<schema>", "<function>"] — function name in [2]
        function_name = parts[2]
        graph = self._graphs.get(function_name)
        if graph is None:
            return None
        from src.parsing.serializer import to_msgpack
        return to_msgpack(graph)

    def set(self, key, value):
        if isinstance(key, bytes):
            key = key.decode()
        self.writes[key] = value
        return True

    def scan(self, cursor: int = 0, match: str | None = None, count: int = 500):
        # Used by schema_discovery.discovered_schemas. Honour the pattern
        # like keys() does, returning every match in a single page.
        if match is None:
            return (0, list(self._graph_keys))
        prefix = match.rstrip("*")
        matches = [k for k in self._graph_keys if k.decode().startswith(prefix)]
        return (0, matches)


def _graph_with_v_data_origin(function_name: str, origin_literal: str) -> dict:
    """Fabricate a parsed-graph dict that will produce one PL/SQL origin."""
    return {
        "function": function_name,
        "nodes": [
            {
                "id": f"{function_name}_N1",
                "type": "INSERT",
                "target_table": "STG_PRODUCT_PROCESSOR",
                "column_maps": {
                    "columns": ["V_DATA_ORIGIN"],
                    "values": [f"'{origin_literal}' V_DATA_ORIGIN"],
                    "mapping": {"V_DATA_ORIGIN": f"'{origin_literal}'"},
                },
                "calculation": [],
                "conditions": [],
                "source_tables": [],
            }
        ],
        "edges": [],
    }


def _empty_graph(function_name: str) -> dict:
    return {
        "function": function_name,
        "nodes": [],
        "edges": [],
    }


def _reset_module_catalog():
    """Drop any registry state left over from a prior test."""
    oc._catalogs.clear()


@pytest.fixture(autouse=True)
def _clean_catalog():
    _reset_module_catalog()
    yield
    _reset_module_catalog()


# ---------------------------------------------------------------------
# TEST 1 — Happy path
# ---------------------------------------------------------------------

def test_build_populates_catalog_and_lookups_work():
    fake = _FakeRedis(
        graph_keys=["graph:OFSMDM:FN_ONE", "graph:OFSMDM:FN_TWO"],
        graphs_by_function={
            "FN_ONE": _graph_with_v_data_origin("FN_ONE", "MANUAL-ADVANCES"),
            "FN_TWO": _graph_with_v_data_origin("FN_TWO", "MANUAL-CBB"),
        },
    )

    catalog = build_catalog(fake, schema="OFSMDM")

    # Catalog contents
    assert "MANUAL-ADVANCES" in catalog.plsql_origins
    assert "MANUAL-CBB" in catalog.plsql_origins
    assert set(catalog.etl_origins.keys()) >= set(BOOTSTRAP_ETL_ORIGINS.keys())
    assert catalog.known_functions == {"FN_ONE", "FN_TWO"}

    # Module registry points at this instance
    assert get_catalog() is catalog
    assert get_catalog("OFSMDM") is catalog
    assert get_known_schemas() == ["OFSMDM"]

    # Public lookups work — both schema-scoped and unscoped
    assert classify_origin("T24")["category"] == "ETL"
    assert classify_origin("MANUAL-ADVANCES")["category"] == "PLSQL"
    assert classify_origin("MANUAL-ADVANCES", schema="OFSMDM")["category"] == "PLSQL"
    assert classify_origin("SOMETHING_NEW")["category"] == "UNKNOWN"


# ---------------------------------------------------------------------
# TEST 2 — build() raises mid-scan
# ---------------------------------------------------------------------

def test_build_failure_leaves_catalog_unset():
    # Five graph keys but Redis dies on the 3rd get() — per-function graph
    # loads fail and those functions never get processed. The completeness
    # validator spots the mismatch between Redis keys and processed
    # functions and refuses the build.
    fake = _FakeRedis(
        graph_keys=[f"graph:OFSMDM:FN_{i}" for i in range(5)],
        graphs_by_function={
            f"FN_{i}": _graph_with_v_data_origin(f"FN_{i}", "MANUAL-ADVANCES")
            for i in range(5)
        },
        fail_after=2,
    )

    with pytest.raises(CatalogBuildError, match="function graph"):
        build_catalog(fake, schema="OFSMDM")

    # Critical invariant: registry is still empty, not holding a broken instance.
    assert oc._catalogs == {}

    # get_catalog() fails loudly — better than returning a half-catalog.
    with pytest.raises(RuntimeError, match="not built"):
        get_catalog()


# ---------------------------------------------------------------------
# TEST 3 — Previous catalog survives a failed rebuild
# ---------------------------------------------------------------------

def test_failed_rebuild_preserves_previous_working_catalog():
    # First build succeeds.
    good = _FakeRedis(
        graph_keys=["graph:OFSMDM:FN_A"],
        graphs_by_function={"FN_A": _graph_with_v_data_origin("FN_A", "MANUAL-ADVANCES")},
    )
    good_catalog = build_catalog(good, schema="OFSMDM")
    assert get_catalog() is good_catalog

    # Second build blows up partway through. The previous catalog must
    # continue serving.
    broken = _FakeRedis(
        graph_keys=["graph:OFSMDM:FN_B", "graph:OFSMDM:FN_C"],
        graphs_by_function={
            "FN_B": _graph_with_v_data_origin("FN_B", "MANUAL-OTHASSETS"),
            "FN_C": _graph_with_v_data_origin("FN_C", "MANUAL-INVESTMENTS"),
        },
        fail_after=0,
    )
    with pytest.raises(CatalogBuildError):
        build_catalog(broken, schema="OFSMDM")

    # The OFSMDM entry still points at the original working catalog.
    assert get_catalog() is good_catalog
    assert get_catalog("OFSMDM") is good_catalog
    assert "MANUAL-ADVANCES" in get_catalog().plsql_origins


# ---------------------------------------------------------------------
# TEST 4 — Missing BOOTSTRAP_ETL_ORIGINS fails validation
# ---------------------------------------------------------------------

def test_missing_bootstrap_seeding_fails_validation(monkeypatch):
    fake = _FakeRedis(
        graph_keys=["graph:OFSMDM:FN_ONE"],
        graphs_by_function={
            "FN_ONE": _graph_with_v_data_origin("FN_ONE", "MANUAL-ADVANCES"),
        },
    )

    # Simulate a bug where the bootstrap seeding assignment is skipped:
    # patch build() so that after the scan it leaves etl_origins empty.
    real_build = OriginsCatalog.build

    def build_without_bootstrap(self):
        # Replicate the scan loop outcome but skip the BOOTSTRAP assignment.
        result = real_build(self)
        self.etl_origins = {}   # force the seeding to be undone
        # Re-invoke the validator so the test triggers its failure path
        # (replaces what build() would have done at the seeding step).
        self._validate_completeness(self.known_functions)
        return result

    monkeypatch.setattr(OriginsCatalog, "build", build_without_bootstrap)

    with pytest.raises(CatalogBuildError, match="bootstrap ETL"):
        build_catalog(fake, schema="OFSMDM")

    # Registry stayed empty — a broken catalog never got swapped in.
    assert oc._catalogs == {}


# ---------------------------------------------------------------------
# TEST 5 — Startup log only emits on success
# ---------------------------------------------------------------------

def test_log_emits_only_after_successful_swap(monkeypatch):
    # The module logger has propagate=False and its own file handlers, so
    # caplog can't see it. Spy on logger.info directly via monkeypatch.
    calls: list[str] = []

    def _spy_info(msg, *args, **kwargs):
        # Format args into msg like logging does, so assertions can search
        # for the rendered message.
        calls.append(msg % args if args else msg)

    monkeypatch.setattr(oc.logger, "info", _spy_info)

    # Successful build emits the "OriginsCatalog.build summary" line.
    fake_good = _FakeRedis(
        graph_keys=["graph:OFSMDM:FN_ONE"],
        graphs_by_function={
            "FN_ONE": _graph_with_v_data_origin("FN_ONE", "MANUAL-ADVANCES"),
        },
    )
    build_catalog(fake_good, schema="OFSMDM")
    assert any("OriginsCatalog.build summary" in m for m in calls), (
        "Successful build must log the summary line after the swap."
    )

    # Reset between sub-scenarios.
    calls.clear()
    _reset_module_catalog()

    # Failing build emits NO summary line.
    fake_bad = _FakeRedis(
        graph_keys=["graph:OFSMDM:FN_ONE", "graph:OFSMDM:FN_TWO"],
        graphs_by_function={
            "FN_ONE": _graph_with_v_data_origin("FN_ONE", "MANUAL-ADVANCES"),
            "FN_TWO": _graph_with_v_data_origin("FN_TWO", "MANUAL-CBB"),
        },
        fail_after=0,
    )
    with pytest.raises(CatalogBuildError):
        build_catalog(fake_bad, schema="OFSMDM")
    assert not any("OriginsCatalog.build summary" in m for m in calls), (
        "Failed build must NOT log the summary line."
    )


# ---------------------------------------------------------------------
# Ancillary: lookups raise cleanly when no catalog exists
# ---------------------------------------------------------------------

def test_lookups_raise_runtime_error_when_no_catalog():
    assert oc._catalogs == {}
    with pytest.raises(RuntimeError, match="not built"):
        is_gl_blocked("401020114-0000")
    with pytest.raises(RuntimeError, match="not built"):
        classify_origin("T24")


# ---------------------------------------------------------------------
# Phase 2 — multi-schema build
# ---------------------------------------------------------------------

def test_build_iterates_all_discovered_schemas():
    """Calling build_catalog without a schema arg builds OFSMDM AND OFSERM.

    The OFSMDM corpus carries V_DATA_ORIGIN literals (so plsql_origins is
    populated). The OFSERM corpus has no V_DATA_ORIGIN literals, only
    functions — its plsql_origins stays empty, which is fine because the
    completeness check is OFSMDM-scoped.
    """
    fake = _FakeRedis(
        graph_keys=[
            "graph:OFSMDM:FN_OFSMDM_ONE",
            "graph:OFSERM:FN_OFSERM_ONE",
        ],
        graphs_by_function={
            "FN_OFSMDM_ONE": _graph_with_v_data_origin(
                "FN_OFSMDM_ONE", "MANUAL-ADVANCES"
            ),
            # OFSERM function exists but has no V_DATA_ORIGIN literal.
            "FN_OFSERM_ONE": _empty_graph("FN_OFSERM_ONE"),
        },
    )

    result = build_catalog(fake)

    # Both schemas built into a dict
    assert isinstance(result, dict)
    assert set(result.keys()) == {"OFSMDM", "OFSERM"}
    assert get_known_schemas() == ["OFSERM", "OFSMDM"]

    # OFSMDM has the V_DATA_ORIGIN literal
    assert "MANUAL-ADVANCES" in result["OFSMDM"].plsql_origins
    # OFSERM has no V_DATA_ORIGIN literals but the catalog still built;
    # bootstrap ETL origins are present on every schema.
    assert result["OFSERM"].plsql_origins == {}
    assert "T24" in result["OFSERM"].etl_origins

    # Schema-scoped lookups isolate per-schema state
    assert classify_origin("MANUAL-ADVANCES", schema="OFSMDM")["category"] == "PLSQL"
    assert classify_origin("MANUAL-ADVANCES", schema="OFSERM")["category"] == "UNKNOWN"
    # Unscoped lookup still finds the literal in OFSMDM
    assert classify_origin("MANUAL-ADVANCES")["category"] == "PLSQL"


def test_build_writes_per_schema_redis_snapshot():
    """to_redis() writes graph:origins:<schema>:* keys for inspectability."""
    fake = _FakeRedis(
        graph_keys=["graph:OFSMDM:FN_ONE"],
        graphs_by_function={
            "FN_ONE": _graph_with_v_data_origin("FN_ONE", "MANUAL-ADVANCES"),
        },
    )

    build_catalog(fake, schema="OFSMDM")

    expected_facets = {"plsql", "etl", "gl_blocked", "eop_overrides", "meta"}
    written_keys = set(fake.writes.keys())
    expected_keys = {
        SchemaAwareKeyspace.origins_key("OFSMDM", facet)
        for facet in expected_facets
    }
    assert expected_keys.issubset(written_keys)

    # Round-trip the meta snapshot — confirms the payload encoding works
    # and the counts match the in-memory catalog.
    meta_payload = from_msgpack(
        fake.writes[SchemaAwareKeyspace.origins_key("OFSMDM", "meta")]
    )
    assert meta_payload["schema"] == "OFSMDM"
    assert meta_payload["function_count"] == 1
    assert meta_payload["plsql_origin_count"] == 1


def test_per_schema_build_failure_does_not_block_other_schemas():
    """When one schema fails to build, other schemas still get a catalog."""
    # OFSMDM succeeds; OFSERM has a key in Redis but its graph payload is
    # missing, which will produce a completeness mismatch. Build should
    # raise per-schema but NOT roll back OFSMDM's good catalog.
    fake = _FakeRedis(
        graph_keys=[
            "graph:OFSMDM:FN_GOOD",
            "graph:OFSERM:FN_DOOMED",
        ],
        graphs_by_function={
            "FN_GOOD": _graph_with_v_data_origin("FN_GOOD", "MANUAL-ADVANCES"),
            # No FN_DOOMED entry → get_function_graph returns None →
            # known_functions stays empty → completeness check fires
            # because expected_functions has FN_DOOMED.
        },
    )

    result = build_catalog(fake)

    assert "OFSMDM" in result
    assert "OFSERM" not in result
    assert get_known_schemas() == ["OFSMDM"]
    assert classify_origin("MANUAL-ADVANCES")["category"] == "PLSQL"


def test_eop_override_lookup_iterates_schemas():
    """Schema-less get_eop_override scans every built catalog."""
    # Build two schemas; manually inject an override into the OFSERM
    # catalog after build to simulate the OFSERM-side override pattern.
    fake = _FakeRedis(
        graph_keys=[
            "graph:OFSMDM:FN_OFSMDM",
            "graph:OFSERM:FN_OFSERM",
        ],
        graphs_by_function={
            "FN_OFSMDM": _graph_with_v_data_origin("FN_OFSMDM", "MANUAL-ADVANCES"),
            "FN_OFSERM": _empty_graph("FN_OFSERM"),
        },
    )
    build_catalog(fake)

    oc._catalogs["OFSERM"].gl_eop_overrides["CAP943"] = {
        "function": "CS_DEFERRED_TAX",
        "node_id": "CS_DEFERRED_TAX:N1",
        "line": 42,
        "reason": "CAP-merged override",
    }

    # Unscoped lookup falls through to OFSERM
    assert get_eop_override("CAP943") is not None
    # Schema-scoped lookup respects the boundary
    assert get_eop_override("CAP943", schema="OFSMDM") is None
    assert get_eop_override("CAP943", schema="OFSERM") is not None
