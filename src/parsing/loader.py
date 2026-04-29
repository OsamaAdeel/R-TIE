"""
Startup loader — orchestrates the complete parsing pipeline.
Called once when the application starts.
"""

import glob
import os
import traceback
from typing import Any

from src.parsing.parser import parse_function, PATTERNS, clean_source_lines
from src.parsing.builder import build_function_graph
from src.parsing.indexer import (
    build_cross_function_graph,
    build_global_column_index,
    resolve_execution_order,
    build_alias_map,
)
from src.parsing.literals import (
    CompiledPattern,
    compile_patterns,
    extract_literals,
    merge_into_index,
)
from src.parsing.serializer import calculate_compression_ratio, to_json
from src.parsing.store import (
    store_function_graph,
    get_function_graph,
    store_full_graph,
    store_column_index,
    store_raw_source,
    store_batch_hierarchy,
    store_literal_index,
    is_graph_stale,
)
from src.parsing.keyspace import SchemaAwareKeyspace
from src.parsing.manifest import (
    BatchManifest,
    ManifestValidationError,
    load_manifest,
)
from src.logger import get_logger

logger = get_logger(__name__, concern="app")

# ---------------------------------------------------------------------------
# Project root resolution
# ---------------------------------------------------------------------------

# loader.py lives at src/parsing/loader.py
# RTIE root = 2 levels up from this file's directory
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_RTIE_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))


def _resolve_functions_dir(functions_dir: str) -> str | None:
    """Resolve *functions_dir* to an absolute path that exists.

    Checks (in order):
      1. The path as-is (already absolute or cwd-relative).
      2. Relative to the RTIE project root.

    Returns the first path that exists, or ``None``.
    """
    candidates = [
        functions_dir,
        os.path.join(_RTIE_ROOT, functions_dir),
    ]
    for candidate in candidates:
        resolved = os.path.abspath(candidate)
        if os.path.isdir(resolved):
            return resolved
    return None


def _function_name_from_file(file_path: str) -> str:
    """Derive the canonical function name from an SQL file's basename.

    Strips the directory and .sql extension, then applies the project-wide
    Redis-key normalization (whitespace runs collapsed to a single
    underscore, uppercased). This canonical form is what gets written as
    the Redis key segment in ``graph:<schema>:<FN>`` AND used as the
    manifest-vs-disk reconciliation key in strict mode — both paths must
    use the same normalization or the strict-mode skip warning fires
    spuriously when manifest/disk disagree on the space-vs-underscore
    surface form.

    Phase 0 finding (w35_diagnostic.md Section 2.5 issue #2): without
    normalization, ``BASEL III CAPITAL.sql`` and ``BASEL_III_CAPITAL.sql``
    produced two distinct Redis keys for the same logical function.
    """
    base = os.path.splitext(os.path.basename(file_path))[0]
    return SchemaAwareKeyspace.normalize_function_name(base)


def _extract_schema_from_source(source_lines: list[str]) -> str | None:
    """Return the schema prefix from ``CREATE OR REPLACE FUNCTION schema.name``.

    Scans the first 40 lines only — the function signature always appears
    near the top of an OFSAA .sql file. Returns upper-cased schema name or
    None if the signature is absent or unprefixed.
    """
    head = "".join(source_lines[:40])
    match = PATTERNS["FUNCTION_DEF"].search(head)
    if match and match.group(1):
        return match.group(1).rstrip(".").upper()
    return None


# ===================================================================
# Module discovery — scan db/modules/*/functions/
# ===================================================================

def discover_module_folders(base_dir: str) -> list[dict]:
    """Scan *base_dir* for ``<module>/functions/`` folders containing .sql files.

    Returns a list of ``{"module_name", "functions_dir", "sql_count"}`` dicts,
    one per module that has at least one .sql file. Top-level directories
    under ``base_dir`` that do not contain a ``functions/`` subdirectory are
    ignored, so schema-name folders like ``db/modules/OFSERM/`` are silently
    skipped when they nest their module folders one level deeper.

    The *base_dir* is resolved relative to the project root if not absolute.
    """
    candidates = [base_dir, os.path.join(_RTIE_ROOT, base_dir)]
    resolved_base: str | None = None
    for candidate in candidates:
        abs_candidate = os.path.abspath(candidate)
        if os.path.isdir(abs_candidate):
            resolved_base = abs_candidate
            break
    if resolved_base is None:
        logger.warning("Module discovery: base directory not found: %s", base_dir)
        return []

    modules: list[dict] = []
    for entry in sorted(os.listdir(resolved_base)):
        module_path = os.path.join(resolved_base, entry)
        if not os.path.isdir(module_path):
            continue
        functions_dir = os.path.join(module_path, "functions")
        if not os.path.isdir(functions_dir):
            continue
        sql_files = glob.glob(os.path.join(functions_dir, "*.sql"))
        modules.append({
            "module_name": entry,
            "functions_dir": functions_dir,
            "sql_count": len(sql_files),
        })
    return modules


# ===================================================================
# 1. Load all functions
# ===================================================================

def load_all_functions(
    functions_dir: str,
    schema: str,
    redis_client,
    force_reparse: bool = False,
    business_identifier_patterns: dict | None = None,
) -> dict:
    """Scan *functions_dir* for ``*.sql`` files, parse each one, and build
    cross-function indices.

    Parameters
    ----------
    functions_dir:
        Directory containing ``.sql`` function files.  May be absolute or
        relative to the RTIE project root.
    schema:
        Oracle schema name used as a namespace in Redis keys.
    redis_client:
        Active Redis client instance.
    force_reparse:
        When ``True``, ignore cached graphs and re-parse every file.
    business_identifier_patterns:
        Optional ``business_identifier_patterns`` config block (see
        ``config/settings.yaml``). When ``None`` the default
        ``CAP\\d{3}`` pattern is used. Pass an empty dict ``{}`` to
        disable literal indexing entirely. The persisted index lives at
        ``graph:literal:<schema>:<identifier>`` and is rebuilt every
        time this function runs against a non-skipped file (cached
        functions do NOT contribute to the rebuild — see comment below).

    Returns
    -------
    dict
        Summary with keys: ``status``, ``functions_parsed``,
        ``functions_skipped``, ``functions_failed``, ``total_nodes``,
        ``total_edges``, ``compression_stats``, ``execution_order``,
        ``errors``, ``literals_indexed`` (Phase 5 — count of distinct
        ``(schema, identifier)`` keys written).
    """
    resolved_dir = _resolve_functions_dir(functions_dir)

    if resolved_dir is None:
        msg = f"Functions directory not found: {functions_dir}"
        logger.error(msg)
        return {
            "status": "error",
            "functions_parsed": 0,
            "functions_skipped": 0,
            "functions_failed": 0,
            "total_nodes": 0,
            "total_edges": 0,
            "compression_stats": {},
            "execution_order": [],
            "errors": [msg],
        }

    # ------------------------------------------------------------------
    # Manifest resolution
    # ------------------------------------------------------------------
    # The module root is the parent of functions_dir (i.e. .../<batch>/).
    # A sibling manifest.yaml, if present, drives the parse order, the
    # schema, and attaches hierarchy metadata to every produced graph.
    module_dir = os.path.dirname(resolved_dir)
    manifest: BatchManifest | None = load_manifest(module_dir)

    effective_schema_default = schema
    if manifest is not None:
        logger.info(
            "Module %s: manifest found with %d processes, %d active tasks, "
            "%d inactive tasks",
            manifest.batch,
            manifest.process_count(),
            manifest.active_task_count(),
            manifest.inactive_task_count(),
        )
        if manifest.schema != schema:
            logger.warning(
                "Module %s: manifest schema '%s' overrides config schema '%s' "
                "for this batch",
                manifest.batch, manifest.schema, schema,
            )
            effective_schema_default = manifest.schema
    else:
        logger.info(
            "Module %s: no manifest.yaml found, using flat structure",
            os.path.basename(module_dir) or functions_dir,
        )

    sql_pattern = os.path.join(resolved_dir, "*.sql")
    fs_sql_files = sorted(glob.glob(sql_pattern))

    if not fs_sql_files and manifest is None:
        msg = f"No .sql files found in {resolved_dir}"
        logger.warning(msg)
        return {
            "status": "warning",
            "functions_parsed": 0,
            "functions_skipped": 0,
            "functions_failed": 0,
            "total_nodes": 0,
            "total_edges": 0,
            "compression_stats": {},
            "execution_order": [],
            "errors": [msg],
        }

    # Build the parse order: manifest order (declaration order, both active
    # and inactive) when the manifest exists, otherwise filesystem order.
    # Also identify extra .sql files not referenced in the manifest so we
    # can warn and skip them ("strict" mode).
    if manifest is not None:
        sql_files: list[str] = []
        manifest_file_keys: set[str] = set()
        for task in manifest.iter_all_tasks():
            # Inactive tasks may have no source_file (the on-disk SQL was
            # removed when the task was dropped from the batch). Skip them
            # here so we don't try to open an empty path.
            if not task.source_file:
                continue
            # Use the same normalization as _function_name_from_file so the
            # strict-mode reconciliation below compares like-for-like.
            manifest_file_keys.add(_function_name_from_file(task.source_file))
            sql_files.append(os.path.join(resolved_dir, task.source_file))

        fs_keys = {
            _function_name_from_file(f): f for f in fs_sql_files
        }
        for fs_key, fs_path in fs_keys.items():
            if fs_key not in manifest_file_keys:
                logger.warning(
                    "Module %s: source file %s not referenced in manifest.yaml — "
                    "skipping (strict mode)",
                    manifest.batch, os.path.basename(fs_path),
                )
    else:
        sql_files = fs_sql_files

    # ------------------------------------------------------------------
    # Per-function parse loop
    # ------------------------------------------------------------------
    all_graphs: dict[str, dict] = {}
    parsed_count = 0
    skipped_count = 0
    failed_count = 0
    errors: list[str] = []
    total_nodes = 0
    total_edges = 0
    compression_stats: list[dict] = []

    # Track per-file effective schema so the cross-function indexer at the
    # end groups graphs by their actual stored schema, not the directory default.
    graph_schemas: dict[str, str] = {}

    # W35 Phase 5: per-schema accumulator for business-identifier literal
    # extraction. Keyed by schema -> {identifier -> [{function, line, role}]}.
    # We build the index in memory across the parse loop and write to
    # Redis once after the loop, so a function that contributes to two
    # schemas (rare today, but possible) ends up in both partitions.
    compiled_literal_patterns: list[CompiledPattern] = compile_patterns(
        business_identifier_patterns
    )
    literal_index_by_schema: dict[str, dict[str, list[dict]]] = {}

    for sql_file in sql_files:
        func_name = _function_name_from_file(sql_file)
        try:
            # Read source lines up-front so we can detect the schema prefix
            # before the staleness/storage path (which writes under that schema).
            with open(sql_file, "r", encoding="utf-8") as fh:
                source_lines = fh.readlines()

            extracted_schema = _extract_schema_from_source(source_lines)
            effective_schema = extracted_schema or effective_schema_default
            if extracted_schema and extracted_schema != effective_schema_default:
                logger.warning(
                    "Parsed %s.%s into graph:%s:%s. "
                    "Note: full multi-schema support (W35) is not yet implemented. "
                    "Queries about this function may produce partial results.",
                    extracted_schema, func_name, extracted_schema, func_name,
                )

            # Hierarchy metadata (None when no manifest)
            hierarchy: dict | None = None
            if manifest is not None:
                task = manifest.get_task_by_file(sql_file)
                if task is not None:
                    hierarchy = task.to_node_hierarchy()

            # W35 Phase 5: extract business identifier literals before the
            # cache short-circuit. Comments are stripped first so we don't
            # match e.g. `'CAP973'` inside a `-- ... 'CAP973' ...` comment.
            # Literals are accumulated per-schema in memory and flushed to
            # Redis once after the loop. Done unconditionally (cached or
            # parsed) so the persisted index is complete after every load.
            if compiled_literal_patterns:
                cleaned_lines, _ = clean_source_lines(source_lines)
                lit_records = extract_literals(
                    source_lines=cleaned_lines,
                    function_name=func_name,
                    patterns=compiled_literal_patterns,
                )
                if lit_records:
                    schema_bucket = literal_index_by_schema.setdefault(
                        effective_schema, {}
                    )
                    merge_into_index(schema_bucket, lit_records)

            # Staleness check (keyed by effective schema, not the directory
            # default). Cached graphs are only trusted when the hierarchy
            # metadata on the cached graph matches what the manifest
            # currently says — otherwise we re-parse so manifest edits
            # propagate without needing force_reparse.
            if not force_reparse and not is_graph_stale(
                redis_client, effective_schema, func_name, sql_file
            ):
                cached = get_function_graph(redis_client, effective_schema, func_name)
                if cached is not None and cached.get("hierarchy") == hierarchy:
                    all_graphs[func_name] = cached
                    graph_schemas[func_name] = effective_schema
                    total_nodes += len(cached.get("nodes", []))
                    total_edges += len(cached.get("edges", []))
                    skipped_count += 1
                    logger.info(
                        "Skipped (cached) %s.%s", effective_schema, func_name
                    )
                    continue

            # Build function graph
            graph = build_function_graph(
                source_lines=source_lines,
                function_name=func_name,
                file_name=os.path.basename(sql_file),
                schema=effective_schema,
                hierarchy=hierarchy,
            )

            # Store in Redis
            store_function_graph(redis_client, effective_schema, func_name, graph)
            store_raw_source(redis_client, effective_schema, func_name, source_lines)

            # Compression stats
            comp = calculate_compression_ratio(len(source_lines), graph)
            compression_stats.append({func_name: comp})

            all_graphs[func_name] = graph
            graph_schemas[func_name] = effective_schema
            total_nodes += len(graph.get("nodes", []))
            total_edges += len(graph.get("edges", []))
            parsed_count += 1
            logger.info(
                "Parsed %s.%s — %d nodes, %d edges",
                effective_schema,
                func_name,
                len(graph.get("nodes", [])),
                len(graph.get("edges", [])),
            )

        except Exception:
            tb = traceback.format_exc()
            err_msg = f"Failed to parse {func_name}: {tb}"
            errors.append(err_msg)
            failed_count += 1
            # Log the filename so developers can immediately identify which
            # file broke — W38 requires no silent skips.
            logger.error(
                "FAILED to parse %s (schema=%s):\n%s",
                os.path.basename(sql_file), schema, tb,
            )

    # ------------------------------------------------------------------
    # Cross-function indices (only if we have at least one graph)
    # ------------------------------------------------------------------
    execution_order: list[str] = []

    # Cross-function indices are scoped to the primary schema only.
    # Functions stored under alternate schemas (e.g. OFSERM) still live at
    # graph:<schema>:<fn> so the W37 pre-check can find them, but they are
    # intentionally excluded from the primary-schema rollups until full
    # multi-schema support lands (W35). The primary schema follows the
    # manifest when one is present (the manifest's schema field is the
    # authoritative owner of the batch's output tables), otherwise it
    # falls back to the config-supplied schema.
    primary_schema = manifest.schema if manifest is not None else schema
    primary_graphs = [
        g for fn, g in all_graphs.items()
        if graph_schemas.get(fn, primary_schema) == primary_schema
    ]

    if primary_graphs:
        try:
            full_graph = build_cross_function_graph(primary_graphs)
            store_full_graph(redis_client, primary_schema, full_graph)
        except Exception:
            tb = traceback.format_exc()
            errors.append(f"Failed to build cross-function graph: {tb}")
            logger.error("Error building cross-function graph:\n%s", tb)

        try:
            column_index = build_global_column_index(primary_graphs)
            store_column_index(redis_client, primary_schema, column_index)
        except Exception:
            tb = traceback.format_exc()
            errors.append(f"Failed to build global column index: {tb}")
            logger.error("Error building global column index:\n%s", tb)

        try:
            execution_order = resolve_execution_order(primary_graphs)
        except Exception:
            tb = traceback.format_exc()
            errors.append(f"Failed to resolve execution order: {tb}")
            logger.error("Error resolving execution order:\n%s", tb)

        try:
            alias_map = build_alias_map()
            # Store alias map in Redis using the standard key pattern
            from src.parsing.serializer import to_msgpack
            alias_key = SchemaAwareKeyspace.graph_aliases_key(primary_schema)
            redis_client.set(alias_key, to_msgpack(alias_map))
        except Exception:
            tb = traceback.format_exc()
            errors.append(f"Failed to build alias map: {tb}")
            logger.error("Error building alias map:\n%s", tb)

    # ------------------------------------------------------------------
    # Persist manifest hierarchy
    # ------------------------------------------------------------------
    if manifest is not None:
        try:
            store_batch_hierarchy(redis_client, manifest.batch, manifest.to_dict())
        except Exception:
            tb = traceback.format_exc()
            errors.append(f"Failed to store batch hierarchy: {tb}")
            logger.error("Error storing batch hierarchy:\n%s", tb)

    # ------------------------------------------------------------------
    # W35 Phase 5: persist business identifier literal index
    # ------------------------------------------------------------------
    # Single fan-out write per (schema, identifier) — failures on one key
    # are logged inside store_literal_index and don't abort the rest.
    literals_indexed = 0
    for sch, schema_index in literal_index_by_schema.items():
        try:
            written = store_literal_index(redis_client, sch, schema_index)
            literals_indexed += written
            logger.info(
                "Stored %d business-identifier literal keys for schema %s",
                written, sch,
            )
        except Exception:
            tb = traceback.format_exc()
            errors.append(f"Failed to store literal index for {sch}: {tb}")
            logger.error("Error storing literal index for %s:\n%s", sch, tb)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    total_usable = parsed_count + skipped_count
    status = "success" if not errors else "partial" if total_usable > 0 else "error"

    summary = {
        "status": status,
        "functions_parsed": parsed_count,
        "functions_skipped": skipped_count,
        "functions_failed": failed_count,
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "compression_stats": compression_stats,
        "execution_order": execution_order,
        "errors": errors,
        "literals_indexed": literals_indexed,
    }

    logger.info(
        "Load complete for schema=%s — parsed=%d, skipped=%d, failed=%d, "
        "nodes=%d, edges=%d, status=%s",
        schema,
        parsed_count,
        skipped_count,
        failed_count,
        total_nodes,
        total_edges,
        status,
    )

    return summary


# ===================================================================
# 2. Parse a single function
# ===================================================================

def parse_single_function(
    sql_file_path: str,
    schema: str,
    redis_client,
) -> dict:
    """Parse and store a single SQL function file.

    Used by the ``/refresh-cache`` command to re-parse one function
    without reloading the entire pipeline.

    Parameters
    ----------
    sql_file_path:
        Absolute or project-relative path to the ``.sql`` file.
    schema:
        Oracle schema name.
    redis_client:
        Active Redis client instance.

    Returns
    -------
    dict
        Result with keys: ``status``, ``function_name``, ``nodes``,
        ``edges``, ``compression``, ``error`` (if any).
    """
    # Resolve path — try as-is, then relative to project root
    resolved_path: str | None = None
    for candidate in [sql_file_path, os.path.join(_RTIE_ROOT, sql_file_path)]:
        abs_candidate = os.path.abspath(candidate)
        if os.path.isfile(abs_candidate):
            resolved_path = abs_candidate
            break

    if resolved_path is None:
        err = f"SQL file not found: {sql_file_path}"
        logger.error(err)
        return {
            "status": "error",
            "function_name": None,
            "nodes": 0,
            "edges": 0,
            "compression": {},
            "error": err,
        }

    func_name = _function_name_from_file(resolved_path)

    try:
        with open(resolved_path, "r", encoding="utf-8") as fh:
            source_lines = fh.readlines()

        graph = build_function_graph(
            source_lines=source_lines,
            function_name=func_name,
            file_name=os.path.basename(resolved_path),
            schema=schema,
        )

        store_function_graph(redis_client, schema, func_name, graph)
        store_raw_source(redis_client, schema, func_name, source_lines)

        comp = calculate_compression_ratio(len(source_lines), graph)
        node_count = len(graph.get("nodes", []))
        edge_count = len(graph.get("edges", []))

        logger.info(
            "Parsed single function %s.%s — %d nodes, %d edges",
            schema,
            func_name,
            node_count,
            edge_count,
        )

        return {
            "status": "success",
            "function_name": func_name,
            "nodes": node_count,
            "edges": edge_count,
            "compression": comp,
            "error": None,
        }

    except Exception:
        tb = traceback.format_exc()
        logger.error("Error parsing single function %s.%s:\n%s", schema, func_name, tb)
        return {
            "status": "error",
            "function_name": func_name,
            "nodes": 0,
            "edges": 0,
            "compression": {},
            "error": tb,
        }
