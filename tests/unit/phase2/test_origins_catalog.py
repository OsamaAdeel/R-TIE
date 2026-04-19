"""Unit tests for src.phase2.origins_catalog.

Focus: the module-global ``_catalog`` MUST NOT be left in a half-initialised
state when a build fails. These tests lock in that guarantee — they are the
reason the zombie-worker bug surfaced as UNKNOWN classifications, and they
prevent it from silently returning.
"""

from __future__ import annotations

import pytest

from src.phase2 import origins_catalog as oc
from src.phase2.origins_catalog import (
    BOOTSTRAP_ETL_ORIGINS,
    CatalogBuildError,
    OriginsCatalog,
    build_catalog,
    classify_origin,
    get_catalog,
    is_gl_blocked,
)


# ---------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------

class _FakeRedis:
    """Minimal stand-in for a redis.Redis client.

    ``keys()`` returns the provided list of function-graph keys.
    ``get()`` returns msgpack-encoded graph payloads. ``fail_after`` trips
    an exception when the Nth ``get()`` call is made — used to simulate a
    Redis outage mid-scan.
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

    def keys(self, pattern: str):
        return list(self._graph_keys)

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
        function_name = parts[2]
        graph = self._graphs.get(function_name)
        if graph is None:
            return None
        from src.parsing.serializer import to_msgpack
        return to_msgpack(graph)


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
    """Drop any _catalog state left over from a prior test."""
    oc._catalog = None


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

    # Module global points at this instance
    assert get_catalog() is catalog

    # Public lookups work
    assert classify_origin("T24")["category"] == "ETL"
    assert classify_origin("MANUAL-ADVANCES")["category"] == "PLSQL"
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

    # Critical invariant: module global is still None, not a broken instance.
    assert oc._catalog is None

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

    # The module global still points at the original working catalog.
    assert get_catalog() is good_catalog
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

    # Module global stayed None — a broken catalog never got swapped in.
    assert oc._catalog is None


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
# Ancillary: is_gl_blocked falls through cleanly when catalog not built
# ---------------------------------------------------------------------

def test_lookups_raise_runtime_error_when_no_catalog():
    assert oc._catalog is None
    with pytest.raises(RuntimeError, match="not built"):
        is_gl_blocked("401020114-0000")
    with pytest.raises(RuntimeError, match="not built"):
        classify_origin("T24")
