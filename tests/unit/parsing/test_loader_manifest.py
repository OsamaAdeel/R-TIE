"""Integration tests for loader.py ↔ manifest.py interaction.

Uses a real tmp_path filesystem (no mocks) and a FakeRedis stand-in to
verify that:
  * modules with a manifest.yaml produce graphs annotated with hierarchy,
  * source files not listed in the manifest are skipped with a warning,
  * modules without a manifest fall back to flat-directory behaviour.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest


@pytest.fixture
def rtie_caplog(caplog):
    """Attach caplog's handler directly to RTIE loggers.

    RTIE's ``get_logger`` factory installs rotating file handlers and sets
    ``propagate=False`` to keep the terminal quiet. That also hides records
    from pytest ``caplog``'s root handler, so we patch the specific named
    loggers to route a copy of each record to caplog.
    """
    targets = [
        "src.parsing.loader",
        "src.parsing.manifest",
        "src.parsing.store",
        "src.parsing.builder",
    ]
    for name in targets:
        lg = logging.getLogger(name)
        lg.addHandler(caplog.handler)
        lg.setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    try:
        yield caplog
    finally:
        for name in targets:
            logging.getLogger(name).removeHandler(caplog.handler)


# ---------------------------------------------------------------------------
# Minimal in-process Redis stub — enough for the loader's set/get/sadd calls
# ---------------------------------------------------------------------------

class FakeRedis:
    """Tiny subset of redis-py used by the parsing/store layer.

    Supports ``set``, ``get``, ``sadd``, ``smembers``, ``scan``, ``delete``,
    which is everything the loader + store modules touch during a load.
    """

    def __init__(self) -> None:
        self._kv: dict[str, bytes] = {}
        self._sets: dict[str, set[bytes]] = {}

    def set(self, key, value):
        k = key.decode() if isinstance(key, bytes) else key
        v = value if isinstance(value, (bytes, bytearray)) else str(value).encode()
        self._kv[k] = bytes(v)
        return True

    def get(self, key):
        k = key.decode() if isinstance(key, bytes) else key
        return self._kv.get(k)

    def sadd(self, key, *members):
        k = key.decode() if isinstance(key, bytes) else key
        bucket = self._sets.setdefault(k, set())
        before = len(bucket)
        for m in members:
            bucket.add(m.encode() if isinstance(m, str) else bytes(m))
        return len(bucket) - before

    def smembers(self, key):
        k = key.decode() if isinstance(key, bytes) else key
        return set(self._sets.get(k, set()))

    def scan(self, cursor=0, match=None, count=100):
        keys = list(self._kv.keys())
        if match:
            import fnmatch
            keys = [k for k in keys if fnmatch.fnmatch(k, match)]
        return 0, [k.encode() for k in keys]

    def delete(self, *keys):
        n = 0
        for k in keys:
            k2 = k.decode() if isinstance(k, bytes) else k
            n += int(self._kv.pop(k2, None) is not None)
        return n


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SQL_TEMPLATE = """\
CREATE OR REPLACE FUNCTION OFSMDM.{name} (x NUMBER)
RETURN NUMBER IS
BEGIN
  INSERT INTO FCT_TEST (N_VAL) SELECT 1 FROM DUAL;
  COMMIT;
  RETURN 1;
END;
"""


def _write_sql(path: Path, function_name: str) -> None:
    path.write_text(_SQL_TEMPLATE.format(name=function_name), encoding="utf-8")


@pytest.fixture
def module_with_manifest(tmp_path):
    mod = tmp_path / "DEMO_BATCH"
    fns = mod / "functions"
    fns.mkdir(parents=True)
    _write_sql(fns / "fn_alpha.sql", "FN_ALPHA")
    _write_sql(fns / "fn_beta.sql", "FN_BETA")
    # Orphan file, not listed in manifest — loader should warn + skip it.
    _write_sql(fns / "fn_orphan.sql", "FN_ORPHAN")

    (mod / "manifest.yaml").write_text(
        """\
batch: DEMO_BATCH
schema: OFSMDM
processes:
  - name: LOAD
    sub_processes:
      - name: SUB
        tasks:
          - order: 1
            name: FN_ALPHA
            type: FUNCTION
            source_file: fn_alpha.sql
            active: true
          - order: 2
            name: FN_BETA
            type: FUNCTION
            source_file: fn_beta.sql
            active: false
            inactive_reason: "test inactive handling"
""",
        encoding="utf-8",
    )
    return mod


@pytest.fixture
def module_without_manifest(tmp_path):
    mod = tmp_path / "BARE_BATCH"
    fns = mod / "functions"
    fns.mkdir(parents=True)
    _write_sql(fns / "fn_plain.sql", "FN_PLAIN")
    return mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_module_with_manifest_parses_tasks_in_order(module_with_manifest, rtie_caplog):
    from src.parsing.loader import load_all_functions

    redis = FakeRedis()
    functions_dir = str(module_with_manifest / "functions")

    result = load_all_functions(
        functions_dir=functions_dir,
        schema="OFSMDM",
        redis_client=redis,
        force_reparse=True,
    )

    assert result["functions_parsed"] == 2
    assert result["functions_failed"] == 0
    assert "manifest found" in rtie_caplog.text
    assert "1 processes" in rtie_caplog.text
    assert "1 active tasks" in rtie_caplog.text
    assert "1 inactive tasks" in rtie_caplog.text


def test_module_with_manifest_annotates_graph_with_hierarchy(module_with_manifest):
    from src.parsing.loader import load_all_functions
    from src.parsing.store import get_function_graph, get_batch_hierarchy

    redis = FakeRedis()
    load_all_functions(
        functions_dir=str(module_with_manifest / "functions"),
        schema="OFSMDM",
        redis_client=redis,
        force_reparse=True,
    )

    graph = get_function_graph(redis, "OFSMDM", "FN_ALPHA")
    assert graph is not None
    h = graph.get("hierarchy")
    assert h is not None
    assert h["batch"] == "DEMO_BATCH"
    assert h["process"] == "LOAD"
    assert h["sub_process"] == "SUB"
    assert h["task_order"] == 1
    assert h["active"] is True

    # Every node should carry the same hierarchy metadata.
    for node in graph["nodes"]:
        assert node["hierarchy"]["batch"] == "DEMO_BATCH"

    # Inactive task's graph is still built, but flagged.
    beta = get_function_graph(redis, "OFSMDM", "FN_BETA")
    assert beta["hierarchy"]["active"] is False
    assert beta["hierarchy"]["inactive_reason"] == "test inactive handling"

    # Manifest stored in Redis under hierarchy:<batch_name>
    stored = get_batch_hierarchy(redis, "DEMO_BATCH")
    assert stored is not None
    assert stored["batch"] == "DEMO_BATCH"


def test_orphan_sql_file_is_skipped_with_warning(module_with_manifest, rtie_caplog):
    from src.parsing.loader import load_all_functions
    from src.parsing.store import get_function_graph

    redis = FakeRedis()
    load_all_functions(
        functions_dir=str(module_with_manifest / "functions"),
        schema="OFSMDM",
        redis_client=redis,
        force_reparse=True,
    )

    assert get_function_graph(redis, "OFSMDM", "FN_ORPHAN") is None
    assert "fn_orphan.sql" in rtie_caplog.text
    assert "not referenced in manifest" in rtie_caplog.text


def test_module_without_manifest_uses_flat_structure(module_without_manifest, rtie_caplog):
    from src.parsing.loader import load_all_functions
    from src.parsing.store import get_function_graph

    redis = FakeRedis()
    result = load_all_functions(
        functions_dir=str(module_without_manifest / "functions"),
        schema="OFSMDM",
        redis_client=redis,
        force_reparse=True,
    )

    assert result["functions_parsed"] == 1
    assert "no manifest.yaml found" in rtie_caplog.text

    graph = get_function_graph(redis, "OFSMDM", "FN_PLAIN")
    assert graph is not None
    # Graphs without a manifest carry no hierarchy field.
    assert "hierarchy" not in graph
    for node in graph["nodes"]:
        assert "hierarchy" not in node


def test_malformed_manifest_raises_validation_error(tmp_path):
    mod = tmp_path / "BAD_BATCH"
    fns = mod / "functions"
    fns.mkdir(parents=True)
    _write_sql(fns / "fn_x.sql", "FN_X")
    (mod / "manifest.yaml").write_text(
        "batch: BAD_BATCH\nschema: NOT_A_SCHEMA\nprocesses: []\n",
        encoding="utf-8",
    )

    from src.parsing.loader import load_all_functions
    from src.parsing.manifest import ManifestValidationError

    with pytest.raises(ManifestValidationError, match="unknown schema"):
        load_all_functions(
            functions_dir=str(fns),
            schema="OFSMDM",
            redis_client=FakeRedis(),
            force_reparse=True,
        )
