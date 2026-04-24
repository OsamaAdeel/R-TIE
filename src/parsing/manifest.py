"""
Batch/Process/Sub-process/Task manifest parser.

Reads a module's ``manifest.yaml`` (sibling to the ``functions/`` folder) and
returns a validated :class:`BatchManifest` capturing the four-level OFSAA
hierarchy:

    Batch > Process > Sub-process > Task

The manifest is hand-authored by developers from OFSAA batch exports. The
loader uses it to (1) annotate graph nodes with hierarchy metadata,
(2) enforce task execution order, and (3) mark inactive tasks so they can
be excluded from cross-function edge traversal.

When no ``manifest.yaml`` exists for a module the loader falls back to the
legacy flat-directory behaviour, so the manifest is strictly additive.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Iterator, Optional

import yaml

from src.parsing.parser import PATTERNS
from src.logger import get_logger

logger = get_logger(__name__, concern="app")


# Schemas the parsing/loader layer understands. Extend cautiously — the
# loader's schema prefix detection and Redis key layout must stay in sync.
RECOGNIZED_SCHEMAS: frozenset[str] = frozenset({"OFSMDM", "OFSERM"})

# OFSAA task types. FUNCTION covers hand-written PL/SQL functions; the
# others mirror OFSAA batch metadata categories.
RECOGNIZED_TASK_TYPES: frozenset[str] = frozenset(
    {"T2T", "TYPE3", "CSTM_DT", "CSTM_T2T", "FUNCTION"}
)

MANIFEST_FILENAME = "manifest.yaml"


class ManifestValidationError(Exception):
    """Raised when a manifest.yaml is malformed or inconsistent.

    The message should always identify the offending batch/task so the
    developer can locate it in the YAML file.
    """


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TaskEntry:
    """One leaf-level task in the hierarchy — a parsable ``.sql`` file."""

    order: int
    name: str
    type: str
    source_file: str
    active: bool
    task_id: Optional[str] = None
    inactive_reason: Optional[str] = None
    description: Optional[str] = None
    # Populated during parse so callers can navigate upward without
    # walking the tree.
    batch: str = ""
    process_name: str = ""
    # Full path of sub-process names from outermost to innermost. The
    # innermost (last element) is the one that directly contains the task.
    sub_process_path: tuple[str, ...] = field(default_factory=tuple)

    @property
    def sub_process(self) -> str:
        """Innermost sub-process — the one that directly contains the task."""
        return self.sub_process_path[-1] if self.sub_process_path else ""

    def to_node_hierarchy(self) -> dict:
        """Produce the dict that is attached to each graph node."""
        return {
            "batch": self.batch,
            "process": self.process_name,
            "sub_process": self.sub_process,
            "sub_process_path": list(self.sub_process_path),
            "task_order": self.order,
            "task_name": self.name,
            "task_id": self.task_id,
            "task_type": self.type,
            "active": self.active,
            "inactive_reason": self.inactive_reason,
        }


@dataclass
class SubProcess:
    """A sub-process node. Holds either further sub-processes or leaf tasks."""

    name: str
    description: Optional[str] = None
    sub_processes: list["SubProcess"] = field(default_factory=list)
    tasks: list[TaskEntry] = field(default_factory=list)


@dataclass
class Process:
    """Top-level process under a batch."""

    name: str
    description: Optional[str] = None
    sub_processes: list[SubProcess] = field(default_factory=list)


@dataclass
class BatchManifest:
    """Parsed batch manifest with convenience lookups."""

    batch: str
    schema: str
    description: Optional[str] = None
    processes: list[Process] = field(default_factory=list)
    # Filled by the parser — maps UPPER(task_name) → TaskEntry for O(1)
    # lookups from query-engine code paths.
    _task_index: dict[str, TaskEntry] = field(default_factory=dict, repr=False)
    # Filled by the parser — maps UPPER(filename without .sql) → TaskEntry
    # so the loader can align manifest tasks with its filename-derived
    # graph keys.
    _file_index: dict[str, TaskEntry] = field(default_factory=dict, repr=False)

    # --- navigation helpers -------------------------------------------------

    def get_task(self, function_name: str) -> Optional[TaskEntry]:
        """Look up a task by its declared ``name`` (case-insensitive)."""
        return self._task_index.get(function_name.strip().upper())

    def get_task_by_file(self, source_file: str) -> Optional[TaskEntry]:
        """Look up a task by its ``source_file`` basename (case-insensitive).

        Returns the task whose ``source_file`` matches the given filename
        when stripped of directories and ``.sql`` extension.
        """
        base = os.path.splitext(os.path.basename(source_file))[0].upper()
        return self._file_index.get(base)

    def iter_all_tasks(self) -> Iterator[TaskEntry]:
        yield from self._walk_tasks()

    def iter_active_tasks(self) -> Iterator[TaskEntry]:
        for task in self._walk_tasks():
            if task.active:
                yield task

    def iter_inactive_tasks(self) -> Iterator[TaskEntry]:
        for task in self._walk_tasks():
            if not task.active:
                yield task

    def _walk_tasks(self) -> Iterator[TaskEntry]:
        """Yield every task in declaration order across all processes."""
        for process in self.processes:
            for sub_process in process.sub_processes:
                yield from _walk_sub_process(sub_process)

    # --- formatting helpers -------------------------------------------------

    def describe_hierarchy(self, function_name: str) -> str:
        """Return ``"Batch > Process > Sub-process"`` for a task, or ``""``."""
        task = self.get_task(function_name)
        if task is None:
            return ""
        parts = [self.batch, task.process_name]
        parts.extend(task.sub_process_path)
        return " > ".join(p for p in parts if p)

    # --- serialisation ------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialise the manifest for storage (e.g. Redis)."""
        return {
            "batch": self.batch,
            "schema": self.schema,
            "description": self.description,
            "processes": [_process_to_dict(p) for p in self.processes],
        }

    def process_count(self) -> int:
        return len(self.processes)

    def active_task_count(self) -> int:
        return sum(1 for _ in self.iter_active_tasks())

    def inactive_task_count(self) -> int:
        return sum(1 for _ in self.iter_inactive_tasks())


def _walk_sub_process(sp: SubProcess) -> Iterator[TaskEntry]:
    for task in sp.tasks:
        yield task
    for child in sp.sub_processes:
        yield from _walk_sub_process(child)


def _process_to_dict(p: Process) -> dict:
    return {
        "name": p.name,
        "description": p.description,
        "sub_processes": [_sub_process_to_dict(sp) for sp in p.sub_processes],
    }


def _sub_process_to_dict(sp: SubProcess) -> dict:
    return {
        "name": sp.name,
        "description": sp.description,
        "sub_processes": [_sub_process_to_dict(c) for c in sp.sub_processes],
        "tasks": [_task_to_dict(t) for t in sp.tasks],
    }


def _task_to_dict(t: TaskEntry) -> dict:
    return {
        "order": t.order,
        "name": t.name,
        "type": t.type,
        "source_file": t.source_file,
        "active": t.active,
        "task_id": t.task_id,
        "inactive_reason": t.inactive_reason,
        "description": t.description,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def load_manifest(module_dir: str) -> Optional[BatchManifest]:
    """Load and validate ``<module_dir>/manifest.yaml``.

    Returns ``None`` when the manifest file is absent (backward-compat path).
    Raises :class:`ManifestValidationError` with a developer-actionable
    message when the manifest exists but fails validation.

    Validation steps:
      * Required top-level fields are present (``batch``, ``schema``,
        ``processes``).
      * ``schema`` is one of :data:`RECOGNIZED_SCHEMAS`.
      * Each task ``source_file`` resolves to an existing file under
        ``<module_dir>/functions/``.
      * Each task ``name`` matches the ``CREATE OR REPLACE FUNCTION``
        identifier in the referenced source (case-insensitive).
      * Task ``order`` values are unique and contiguous (1..N) within
        each sub-process.
      * ``active: false`` entries declare a non-empty ``inactive_reason``.
      * Task ``name`` is unique across the manifest.
    """
    module_dir = os.path.abspath(module_dir)
    manifest_path = os.path.join(module_dir, MANIFEST_FILENAME)
    if not os.path.isfile(manifest_path):
        return None

    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ManifestValidationError(
            f"manifest.yaml in {module_dir} is not valid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise ManifestValidationError(
            f"manifest.yaml in {module_dir} must be a YAML mapping at the top "
            f"level (got {type(raw).__name__})"
        )

    batch = raw.get("batch")
    schema = raw.get("schema")
    description = raw.get("description")
    processes_raw = raw.get("processes")

    if not batch or not isinstance(batch, str):
        raise ManifestValidationError(
            f"{manifest_path}: top-level 'batch' is required and must be a string"
        )
    if not schema or not isinstance(schema, str):
        raise ManifestValidationError(
            f"{manifest_path}: top-level 'schema' is required and must be a string"
        )
    schema_upper = schema.strip().upper()
    if schema_upper not in RECOGNIZED_SCHEMAS:
        raise ManifestValidationError(
            f"{manifest_path}: unknown schema '{schema}'. "
            f"Recognized schemas: {sorted(RECOGNIZED_SCHEMAS)}"
        )
    if processes_raw is None or not isinstance(processes_raw, list):
        raise ManifestValidationError(
            f"{manifest_path}: top-level 'processes' is required and must be a list"
        )

    # Warn-only when batch name disagrees with folder name, so developers can
    # draft manifests before renaming the module folder.
    module_basename = os.path.basename(os.path.normpath(module_dir))
    if batch != module_basename:
        logger.warning(
            "Manifest batch '%s' does not match module folder name '%s' (in %s)",
            batch, module_basename, manifest_path,
        )

    functions_dir = os.path.join(module_dir, "functions")
    manifest = BatchManifest(
        batch=batch,
        schema=schema_upper,
        description=description,
    )

    # Pass 1: structural parse (build the dataclass tree).
    for idx, proc_raw in enumerate(processes_raw):
        manifest.processes.append(
            _parse_process(proc_raw, manifest_path=manifest_path, batch=batch, proc_index=idx)
        )

    # Pass 2: validate each task (source_file existence, function-name match,
    # order contiguity, etc.) and populate lookup indices.
    _validate_and_index(manifest, manifest_path=manifest_path, functions_dir=functions_dir)

    return manifest


# ---------------------------------------------------------------------------
# Structural parsing — raw YAML dicts → dataclass tree
# ---------------------------------------------------------------------------

def _parse_process(raw: dict, *, manifest_path: str, batch: str, proc_index: int) -> Process:
    if not isinstance(raw, dict):
        raise ManifestValidationError(
            f"{manifest_path}: processes[{proc_index}] must be a mapping"
        )
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ManifestValidationError(
            f"{manifest_path}: processes[{proc_index}] is missing a 'name'"
        )
    description = raw.get("description")

    sub_processes_raw = raw.get("sub_processes") or []
    if not isinstance(sub_processes_raw, list):
        raise ManifestValidationError(
            f"{manifest_path}: process '{name}' has non-list 'sub_processes'"
        )

    sub_processes = [
        _parse_sub_process(
            sp_raw,
            manifest_path=manifest_path,
            batch=batch,
            process_name=name,
            parent_path=(),
            sp_index=i,
        )
        for i, sp_raw in enumerate(sub_processes_raw)
    ]
    return Process(name=name, description=description, sub_processes=sub_processes)


def _parse_sub_process(
    raw: dict,
    *,
    manifest_path: str,
    batch: str,
    process_name: str,
    parent_path: tuple[str, ...],
    sp_index: int,
) -> SubProcess:
    if not isinstance(raw, dict):
        raise ManifestValidationError(
            f"{manifest_path}: sub_processes[{sp_index}] under process "
            f"'{process_name}' must be a mapping"
        )
    name = raw.get("name")
    if not name or not isinstance(name, str):
        raise ManifestValidationError(
            f"{manifest_path}: sub_processes[{sp_index}] under process "
            f"'{process_name}' is missing a 'name'"
        )
    description = raw.get("description")
    current_path = parent_path + (name,)

    nested_raw = raw.get("sub_processes") or []
    if not isinstance(nested_raw, list):
        raise ManifestValidationError(
            f"{manifest_path}: sub_process '{name}' has non-list 'sub_processes'"
        )
    nested = [
        _parse_sub_process(
            n_raw,
            manifest_path=manifest_path,
            batch=batch,
            process_name=process_name,
            parent_path=current_path,
            sp_index=i,
        )
        for i, n_raw in enumerate(nested_raw)
    ]

    tasks_raw = raw.get("tasks") or []
    if not isinstance(tasks_raw, list):
        raise ManifestValidationError(
            f"{manifest_path}: sub_process '{name}' has non-list 'tasks'"
        )
    tasks = [
        _parse_task(
            t_raw,
            manifest_path=manifest_path,
            batch=batch,
            process_name=process_name,
            sub_process_path=current_path,
            task_index=i,
        )
        for i, t_raw in enumerate(tasks_raw)
    ]

    return SubProcess(
        name=name,
        description=description,
        sub_processes=nested,
        tasks=tasks,
    )


def _parse_task(
    raw: dict,
    *,
    manifest_path: str,
    batch: str,
    process_name: str,
    sub_process_path: tuple[str, ...],
    task_index: int,
) -> TaskEntry:
    sp_label = sub_process_path[-1] if sub_process_path else "?"
    if not isinstance(raw, dict):
        raise ManifestValidationError(
            f"{manifest_path}: tasks[{task_index}] under sub_process "
            f"'{sp_label}' must be a mapping"
        )

    def _require(key: str):
        if key not in raw or raw[key] in ("", None):
            raise ManifestValidationError(
                f"{manifest_path}: task at index {task_index} under sub_process "
                f"'{sp_label}' is missing required field '{key}'"
            )
        return raw[key]

    order = _require("order")
    if not isinstance(order, int) or order < 1:
        raise ManifestValidationError(
            f"{manifest_path}: task '{raw.get('name', '?')}' under '{sp_label}' "
            f"has invalid 'order' ({order!r}); must be a positive integer"
        )

    name = _require("name")
    if not isinstance(name, str):
        raise ManifestValidationError(
            f"{manifest_path}: task at index {task_index} under '{sp_label}' "
            f"has non-string 'name'"
        )

    task_type = _require("type")
    if task_type not in RECOGNIZED_TASK_TYPES:
        raise ManifestValidationError(
            f"{manifest_path}: task '{name}' has unknown type '{task_type}'. "
            f"Recognized types: {sorted(RECOGNIZED_TASK_TYPES)}"
        )

    active = raw.get("active")
    if not isinstance(active, bool):
        raise ManifestValidationError(
            f"{manifest_path}: task '{name}' is missing required boolean 'active'"
        )

    inactive_reason = raw.get("inactive_reason")
    if not active:
        if not inactive_reason or not isinstance(inactive_reason, str):
            raise ManifestValidationError(
                f"{manifest_path}: task '{name}' has active=false but no "
                f"non-empty 'inactive_reason'"
            )

    # source_file is required for active tasks (we need to parse the SQL).
    # For inactive tasks it is optional — the file may have been removed
    # when the task was dropped from the batch run chart.
    raw_source_file = raw.get("source_file")
    if active:
        if not raw_source_file:
            raise ManifestValidationError(
                f"{manifest_path}: task at index {task_index} under sub_process "
                f"'{sp_label}' is missing required field 'source_file'"
            )
    source_file = raw_source_file or ""
    if source_file and not isinstance(source_file, str):
        raise ManifestValidationError(
            f"{manifest_path}: task '{name}' has non-string 'source_file'"
        )

    return TaskEntry(
        order=order,
        name=name,
        type=task_type,
        source_file=source_file,
        active=active,
        task_id=raw.get("task_id"),
        inactive_reason=inactive_reason,
        description=raw.get("description"),
        batch=batch,
        process_name=process_name,
        sub_process_path=sub_process_path,
    )


# ---------------------------------------------------------------------------
# Cross-file validation + index building
# ---------------------------------------------------------------------------

def _validate_and_index(
    manifest: BatchManifest,
    *,
    manifest_path: str,
    functions_dir: str,
) -> None:
    """Run cross-task validation (order, uniqueness, source existence,
    function-name match) and populate ``_task_index`` / ``_file_index``.
    """
    seen_names: dict[str, str] = {}  # UPPER(name) → sub_process label

    def _visit_sub_process(sp: SubProcess) -> None:
        # --- order contiguity check (direct tasks only, per spec) -----------
        if sp.tasks:
            orders = [t.order for t in sp.tasks]
            expected = list(range(1, len(sp.tasks) + 1))
            if sorted(orders) != expected:
                raise ManifestValidationError(
                    f"{manifest_path}: sub_process '{sp.name}' has non-contiguous "
                    f"task orders {sorted(orders)} (expected {expected})"
                )
            if len(orders) != len(set(orders)):
                raise ManifestValidationError(
                    f"{manifest_path}: sub_process '{sp.name}' has duplicate "
                    f"task 'order' integers"
                )

            # --- per-sub-process duplicate-name check -----------------------
            # Only active tasks must be unique. Inactive entries are audit-only
            # (e.g. the same logical task retired once as TYPE3 and left as a
            # record alongside a never-built replacement) and may repeat.
            sp_seen: set[str] = set()
            for t in sp.tasks:
                if not t.active:
                    continue
                name_u = t.name.strip().upper()
                if name_u in sp_seen:
                    raise ManifestValidationError(
                        f"{manifest_path}: duplicate task name '{t.name}' "
                        f"within sub_process '{sp.name}'"
                    )
                sp_seen.add(name_u)

        for t in sp.tasks:
            _validate_task(
                t,
                manifest_path=manifest_path,
                functions_dir=functions_dir,
            )

            name_u = t.name.strip().upper()
            # Global-uniqueness check also applies only to active tasks, for
            # the same reason as the per-sub_process check above.
            if t.active:
                if name_u in seen_names:
                    raise ManifestValidationError(
                        f"{manifest_path}: task name '{t.name}' appears in both "
                        f"sub_process '{seen_names[name_u]}' and '{sp.name}' — "
                        f"task names must be globally unique"
                    )
                seen_names[name_u] = sp.name
            manifest._task_index[name_u] = t

            # Inactive tasks may have no source_file (see _parse_task); skip
            # indexing them by file when absent.
            if t.source_file:
                file_key = os.path.splitext(os.path.basename(t.source_file))[0].upper()
                if file_key in manifest._file_index:
                    raise ManifestValidationError(
                        f"{manifest_path}: source_file '{t.source_file}' is "
                        f"referenced by more than one task"
                    )
                manifest._file_index[file_key] = t

        for child in sp.sub_processes:
            _visit_sub_process(child)

    for process in manifest.processes:
        for sp in process.sub_processes:
            _visit_sub_process(sp)


def _validate_task(
    task: TaskEntry,
    *,
    manifest_path: str,
    functions_dir: str,
) -> None:
    """Check that the source file exists and its CREATE OR REPLACE FUNCTION
    identifier matches the manifest task name (case-insensitive).
    """
    # Inactive tasks are retained in the manifest for audit (via
    # inactive_reason) but not executed. If the on-disk SQL was removed when
    # the task was dropped, skip file validation — there is nothing to parse.
    if not task.active or not task.source_file:
        return

    sql_path = os.path.join(functions_dir, task.source_file)
    if not os.path.isfile(sql_path):
        raise ManifestValidationError(
            f"{manifest_path}: task '{task.name}' references source_file "
            f"'{task.source_file}' but no such file exists in {functions_dir}"
        )

    try:
        with open(sql_path, "r", encoding="utf-8") as fh:
            # Signature is always near the top; scanning the first 40 lines
            # is enough and avoids loading large files twice.
            head = "".join(fh.readline() for _ in range(40))
    except OSError as exc:
        raise ManifestValidationError(
            f"{manifest_path}: cannot read source_file '{sql_path}' for task "
            f"'{task.name}': {exc}"
        ) from exc

    match = PATTERNS["FUNCTION_DEF"].search(head)
    if match is None:
        raise ManifestValidationError(
            f"{manifest_path}: task '{task.name}' source_file '{task.source_file}' "
            f"does not contain a 'CREATE OR REPLACE FUNCTION ...' declaration "
            f"in the first 40 lines"
        )

    declared_name = match.group(2)
    if declared_name.strip().upper() != task.name.strip().upper():
        raise ManifestValidationError(
            f"{manifest_path}: task '{task.name}' does not match the function "
            f"identifier in '{task.source_file}' "
            f"(CREATE OR REPLACE FUNCTION {declared_name})"
        )
