"""Unit tests for src.parsing.manifest — YAML → BatchManifest parser."""

from pathlib import Path

import pytest

from src.parsing.manifest import (
    BatchManifest,
    ManifestValidationError,
    TaskEntry,
    load_manifest,
)


# ---------------------------------------------------------------------------
# Fixtures — build a minimal two-file module on disk per test
# ---------------------------------------------------------------------------

def _sql(function_name: str, schema: str = "OFSMDM") -> str:
    return (
        f"CREATE OR REPLACE FUNCTION {schema}.{function_name} (x NUMBER)\n"
        f"RETURN NUMBER IS\n"
        f"BEGIN\n"
        f"  RETURN 1;\n"
        f"END;\n"
    )


def _write_module(
    base: Path,
    *,
    batch_name: str = "DEMO_BATCH",
    manifest_yaml: str | None = None,
    sql_files: dict[str, str] | None = None,
) -> Path:
    module_dir = base / batch_name
    (module_dir / "functions").mkdir(parents=True)
    if manifest_yaml is not None:
        (module_dir / "manifest.yaml").write_text(manifest_yaml, encoding="utf-8")
    for filename, content in (sql_files or {}).items():
        (module_dir / "functions" / filename).write_text(content, encoding="utf-8")
    return module_dir


VALID_MANIFEST = """\
batch: DEMO_BATCH
schema: OFSMDM
description: "Demo batch"

processes:
  - name: PROC_A
    sub_processes:
      - name: SUB_A
        tasks:
          - order: 1
            name: FN_ONE
            type: FUNCTION
            source_file: fn_one.sql
            active: true
          - order: 2
            name: FN_TWO
            type: T2T
            source_file: fn_two.sql
            active: true
      - name: SUB_B
        tasks:
          - order: 1
            name: FN_THREE
            type: FUNCTION
            source_file: fn_three.sql
            active: false
            inactive_reason: "removed from production per W39"
"""


@pytest.fixture
def valid_module(tmp_path):
    return _write_module(
        tmp_path,
        manifest_yaml=VALID_MANIFEST,
        sql_files={
            "fn_one.sql": _sql("FN_ONE"),
            "fn_two.sql": _sql("FN_TWO"),
            "fn_three.sql": _sql("FN_THREE"),
        },
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_absent_manifest_returns_none(tmp_path):
    module_dir = _write_module(tmp_path, manifest_yaml=None)
    assert load_manifest(str(module_dir)) is None


def test_valid_manifest_parses_hierarchy(valid_module):
    manifest = load_manifest(str(valid_module))
    assert isinstance(manifest, BatchManifest)
    assert manifest.batch == "DEMO_BATCH"
    assert manifest.schema == "OFSMDM"
    assert manifest.process_count() == 1
    assert manifest.active_task_count() == 2
    assert manifest.inactive_task_count() == 1

    proc = manifest.processes[0]
    assert proc.name == "PROC_A"
    assert [sp.name for sp in proc.sub_processes] == ["SUB_A", "SUB_B"]


def test_get_task_finds_nested_tasks(valid_module):
    manifest = load_manifest(str(valid_module))
    task = manifest.get_task("FN_THREE")
    assert isinstance(task, TaskEntry)
    assert task.active is False
    assert task.process_name == "PROC_A"
    assert task.sub_process == "SUB_B"
    assert manifest.get_task("DOES_NOT_EXIST") is None


def test_iter_active_tasks_in_declaration_order(valid_module):
    manifest = load_manifest(str(valid_module))
    active_names = [t.name for t in manifest.iter_active_tasks()]
    assert active_names == ["FN_ONE", "FN_TWO"]
    inactive_names = [t.name for t in manifest.iter_inactive_tasks()]
    assert inactive_names == ["FN_THREE"]


def test_describe_hierarchy(valid_module):
    manifest = load_manifest(str(valid_module))
    assert manifest.describe_hierarchy("FN_ONE") == "DEMO_BATCH > PROC_A > SUB_A"
    assert manifest.describe_hierarchy("FN_THREE") == "DEMO_BATCH > PROC_A > SUB_B"
    assert manifest.describe_hierarchy("MISSING") == ""


def test_to_node_hierarchy_shape(valid_module):
    manifest = load_manifest(str(valid_module))
    task = manifest.get_task("FN_ONE")
    node_hierarchy = task.to_node_hierarchy()
    assert node_hierarchy["batch"] == "DEMO_BATCH"
    assert node_hierarchy["process"] == "PROC_A"
    assert node_hierarchy["sub_process"] == "SUB_A"
    assert node_hierarchy["task_order"] == 1
    assert node_hierarchy["task_type"] == "FUNCTION"
    assert node_hierarchy["active"] is True


def test_get_task_by_file_is_case_insensitive(valid_module):
    manifest = load_manifest(str(valid_module))
    assert manifest.get_task_by_file("FN_ONE.sql").name == "FN_ONE"
    assert manifest.get_task_by_file("fn_one.SQL").name == "FN_ONE"


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------

def test_missing_batch_field_raises(tmp_path):
    module_dir = _write_module(
        tmp_path,
        manifest_yaml="schema: OFSMDM\nprocesses: []\n",
    )
    with pytest.raises(ManifestValidationError, match="'batch' is required"):
        load_manifest(str(module_dir))


def test_unknown_schema_raises(tmp_path):
    module_dir = _write_module(
        tmp_path,
        manifest_yaml="batch: B\nschema: NOT_A_SCHEMA\nprocesses: []\n",
    )
    with pytest.raises(ManifestValidationError, match="unknown schema"):
        load_manifest(str(module_dir))


def test_missing_source_file_raises(tmp_path):
    manifest_yaml = """\
batch: DEMO_BATCH
schema: OFSMDM
processes:
  - name: P
    sub_processes:
      - name: S
        tasks:
          - order: 1
            name: FN_GHOST
            type: FUNCTION
            source_file: never_existed.sql
            active: true
"""
    module_dir = _write_module(tmp_path, manifest_yaml=manifest_yaml, sql_files={})
    with pytest.raises(ManifestValidationError, match="never_existed.sql"):
        load_manifest(str(module_dir))


def test_active_false_without_reason_raises(tmp_path):
    manifest_yaml = """\
batch: DEMO_BATCH
schema: OFSMDM
processes:
  - name: P
    sub_processes:
      - name: S
        tasks:
          - order: 1
            name: FN_ONE
            type: FUNCTION
            source_file: fn_one.sql
            active: false
"""
    module_dir = _write_module(
        tmp_path,
        manifest_yaml=manifest_yaml,
        sql_files={"fn_one.sql": _sql("FN_ONE")},
    )
    with pytest.raises(ManifestValidationError, match="inactive_reason"):
        load_manifest(str(module_dir))


def test_duplicate_task_names_raise(tmp_path):
    manifest_yaml = """\
batch: DEMO_BATCH
schema: OFSMDM
processes:
  - name: P
    sub_processes:
      - name: S
        tasks:
          - order: 1
            name: FN_DUP
            type: FUNCTION
            source_file: fn_one.sql
            active: true
          - order: 2
            name: FN_DUP
            type: FUNCTION
            source_file: fn_two.sql
            active: true
"""
    module_dir = _write_module(
        tmp_path,
        manifest_yaml=manifest_yaml,
        sql_files={
            "fn_one.sql": _sql("FN_DUP"),
            "fn_two.sql": _sql("FN_DUP"),
        },
    )
    with pytest.raises(ManifestValidationError, match="duplicate task name"):
        load_manifest(str(module_dir))


def test_non_contiguous_order_raises(tmp_path):
    manifest_yaml = """\
batch: DEMO_BATCH
schema: OFSMDM
processes:
  - name: P
    sub_processes:
      - name: S
        tasks:
          - order: 1
            name: FN_ONE
            type: FUNCTION
            source_file: fn_one.sql
            active: true
          - order: 3
            name: FN_TWO
            type: FUNCTION
            source_file: fn_two.sql
            active: true
"""
    module_dir = _write_module(
        tmp_path,
        manifest_yaml=manifest_yaml,
        sql_files={
            "fn_one.sql": _sql("FN_ONE"),
            "fn_two.sql": _sql("FN_TWO"),
        },
    )
    with pytest.raises(ManifestValidationError, match="non-contiguous"):
        load_manifest(str(module_dir))


def test_function_name_mismatch_raises(tmp_path):
    manifest_yaml = """\
batch: DEMO_BATCH
schema: OFSMDM
processes:
  - name: P
    sub_processes:
      - name: S
        tasks:
          - order: 1
            name: FN_EXPECTED
            type: FUNCTION
            source_file: fn_one.sql
            active: true
"""
    module_dir = _write_module(
        tmp_path,
        manifest_yaml=manifest_yaml,
        sql_files={"fn_one.sql": _sql("FN_DIFFERENT")},
    )
    with pytest.raises(ManifestValidationError, match="does not match the function"):
        load_manifest(str(module_dir))


def test_contiguous_orders_pass_with_inactive_task(tmp_path):
    # Inactive tasks still count toward the 1..N sequence.
    manifest_yaml = """\
batch: DEMO_BATCH
schema: OFSMDM
processes:
  - name: P
    sub_processes:
      - name: S
        tasks:
          - order: 1
            name: FN_ONE
            type: FUNCTION
            source_file: fn_one.sql
            active: true
          - order: 2
            name: FN_TWO
            type: FUNCTION
            source_file: fn_two.sql
            active: false
            inactive_reason: "test"
"""
    module_dir = _write_module(
        tmp_path,
        manifest_yaml=manifest_yaml,
        sql_files={
            "fn_one.sql": _sql("FN_ONE"),
            "fn_two.sql": _sql("FN_TWO"),
        },
    )
    manifest = load_manifest(str(module_dir))
    assert manifest.active_task_count() == 1
    assert manifest.inactive_task_count() == 1
