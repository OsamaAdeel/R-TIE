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


def test_non_contiguous_orders_pass_w101(tmp_path):
    # W101: Order values reflect OFSAA runchart absolute position. Gaps
    # appear when TYPE3/TYPE2 rows are filtered out at manifest-authoring
    # time. As long as the orders are unique within the container, the
    # validator must accept them.
    manifest_yaml = """\
batch: DEMO_BATCH
schema: OFSMDM
processes:
  - name: P
    sub_processes:
      - name: S
        tasks:
          - order: 2
            name: FN_TWO
            type: T2T
            source_file: fn_two.sql
            active: true
          - order: 4
            name: FN_FOUR
            type: T2T
            source_file: fn_four.sql
            active: true
          - order: 5
            name: FN_FIVE
            type: T2T
            source_file: fn_five.sql
            active: true
          - order: 6
            name: FN_SIX
            type: T2T
            source_file: fn_six.sql
            active: true
          - order: 8
            name: FN_EIGHT
            type: T2T
            source_file: fn_eight.sql
            active: true
          - order: 16
            name: FN_SIXTEEN
            type: T2T
            source_file: fn_sixteen.sql
            active: true
"""
    module_dir = _write_module(
        tmp_path,
        manifest_yaml=manifest_yaml,
        sql_files={
            "fn_two.sql": _sql("FN_TWO"),
            "fn_four.sql": _sql("FN_FOUR"),
            "fn_five.sql": _sql("FN_FIVE"),
            "fn_six.sql": _sql("FN_SIX"),
            "fn_eight.sql": _sql("FN_EIGHT"),
            "fn_sixteen.sql": _sql("FN_SIXTEEN"),
        },
    )
    manifest = load_manifest(str(module_dir))
    assert manifest.active_task_count() == 6
    # Declaration order is preserved (this is the runchart order).
    assert [t.order for t in manifest.iter_active_tasks()] == [
        2, 4, 5, 6, 8, 16
    ]


def test_duplicate_orders_within_container_raise_w101(tmp_path):
    # W101: contiguity is relaxed, but uniqueness within a container is
    # still required. Two tasks at the same order is a real bug.
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
          - order: 1
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
    with pytest.raises(
        ManifestValidationError, match="duplicate task 'order' integers"
    ):
        load_manifest(str(module_dir))


def test_same_name_same_source_across_containers_passes_w101(tmp_path):
    # W101: OFSAA N:M — same function fires in multiple process contexts.
    # The 3 in-tree cases (BNK_UNDERLYING_EXPOSURES_DATA_POPULATION,
    # INV_UNDERLYING_EXPOSURES_DATA_POPULATION, FN_MITIGANT_ELIGIBILITY_CSTM)
    # all match this pattern: same source_file, different sub_processes.
    manifest_yaml = """\
batch: DEMO_BATCH
schema: OFSMDM
processes:
  - name: PROC_A
    sub_processes:
      - name: SUB_A
        tasks:
          - order: 1
            name: FN_SHARED
            type: T2T
            source_file: fn_shared.sql
            active: true
  - name: PROC_B
    sub_processes:
      - name: SUB_B
        tasks:
          - order: 2
            name: FN_SHARED
            type: T2T
            source_file: fn_shared.sql
            active: true
"""
    module_dir = _write_module(
        tmp_path,
        manifest_yaml=manifest_yaml,
        sql_files={"fn_shared.sql": _sql("FN_SHARED")},
    )
    manifest = load_manifest(str(module_dir))
    # Both task entries are recorded as active (the manifest is honest
    # about the duplication); _task_index points at one of them (the
    # first-seen binding) for deterministic navigation.
    assert manifest.active_task_count() == 2
    assert manifest.get_task("FN_SHARED") is not None
    assert manifest.get_task_by_file("fn_shared.sql").name == "FN_SHARED"


def test_same_name_different_source_across_containers_raises_w101(tmp_path):
    # W101: same active task name with a DIFFERENT source_file is a real
    # function-name collision and must still be rejected.
    manifest_yaml = """\
batch: DEMO_BATCH
schema: OFSMDM
processes:
  - name: PROC_A
    sub_processes:
      - name: SUB_A
        tasks:
          - order: 1
            name: FN_COLLIDE
            type: FUNCTION
            source_file: fn_one.sql
            active: true
  - name: PROC_B
    sub_processes:
      - name: SUB_B
        tasks:
          - order: 1
            name: FN_COLLIDE
            type: FUNCTION
            source_file: fn_two.sql
            active: true
"""
    module_dir = _write_module(
        tmp_path,
        manifest_yaml=manifest_yaml,
        sql_files={
            "fn_one.sql": _sql("FN_COLLIDE"),
            "fn_two.sql": _sql("FN_COLLIDE"),
        },
    )
    with pytest.raises(
        ManifestValidationError, match="different source files"
    ):
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
