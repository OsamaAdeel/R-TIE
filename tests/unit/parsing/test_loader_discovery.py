"""Unit tests for W38 loader changes: module discovery and schema extraction."""

import os
import tempfile
from unittest.mock import MagicMock, patch, mock_open

import pytest

from src.parsing.loader import (
    discover_module_folders,
    _extract_schema_from_source,
)


# ---------------------------------------------------------------------------
# _extract_schema_from_source
# ---------------------------------------------------------------------------

def test_extract_schema_with_prefix():
    lines = [
        "CREATE OR REPLACE FUNCTION OFSERM.ABL_Def_Pension_Fund_Asset_Net_DTL(\n",
        "    P_V_BATCH_ID VARCHAR2\n",
        ")\n",
    ]
    assert _extract_schema_from_source(lines) == "OFSERM"


def test_extract_schema_without_prefix():
    lines = [
        "CREATE OR REPLACE FUNCTION FN_FOO RETURN VARCHAR2 AS\n",
        "BEGIN\n",
    ]
    assert _extract_schema_from_source(lines) is None


def test_extract_schema_uppercases_result():
    lines = [
        "create or replace function ofsmdm.fn_foo\n",
    ]
    assert _extract_schema_from_source(lines) == "OFSMDM"


def test_extract_schema_handles_comments_and_blank_lines():
    lines = [
        "-- file header\n",
        "\n",
        "/*  multi-line block comment describing the function */\n",
        "CREATE OR REPLACE FUNCTION OFSMDM.TEST_FN RETURN VARCHAR2 AS\n",
    ]
    assert _extract_schema_from_source(lines) == "OFSMDM"


def test_extract_schema_from_empty_file():
    assert _extract_schema_from_source([]) is None


def test_extract_schema_only_scans_first_40_lines():
    lines = ["-- just a comment\n"] * 60 + [
        "CREATE OR REPLACE FUNCTION OFSMDM.FN RETURN VARCHAR2 AS\n",
    ]
    # The CREATE line is at index 60, past the 40-line window.
    assert _extract_schema_from_source(lines) is None


# ---------------------------------------------------------------------------
# discover_module_folders
# ---------------------------------------------------------------------------

def test_discover_finds_module_with_functions_folder(tmp_path):
    mod_a = tmp_path / "MODULE_A" / "functions"
    mod_a.mkdir(parents=True)
    (mod_a / "f1.sql").write_text("-- f1")

    results = discover_module_folders(str(tmp_path))
    names = [r["module_name"] for r in results]
    assert "MODULE_A" in names


def test_discover_counts_sql_files(tmp_path):
    mod = tmp_path / "MODULE_A" / "functions"
    mod.mkdir(parents=True)
    (mod / "f1.sql").write_text("")
    (mod / "f2.sql").write_text("")
    (mod / "README.md").write_text("")  # non-sql, ignored

    results = discover_module_folders(str(tmp_path))
    counts = {r["module_name"]: r["sql_count"] for r in results}
    assert counts["MODULE_A"] == 2


def test_discover_skips_folders_without_functions_subdir(tmp_path):
    # OFSERM has nested ABL_BIS_CAPITAL_STRUCTURE/functions/ — its own top-level
    # folder has no functions/ subdir so it should be skipped.
    nested = tmp_path / "OFSERM" / "ABL_BIS" / "functions"
    nested.mkdir(parents=True)
    (nested / "f.sql").write_text("")

    results = discover_module_folders(str(tmp_path))
    names = [r["module_name"] for r in results]
    assert "OFSERM" not in names


def test_discover_handles_missing_base_dir():
    assert discover_module_folders("/nonexistent/path") == []


def test_discover_returns_multiple_modules_sorted(tmp_path):
    for name in ("ZZZ_LAST", "AAA_FIRST", "MMM_MID"):
        d = tmp_path / name / "functions"
        d.mkdir(parents=True)
        (d / "f.sql").write_text("")
    results = discover_module_folders(str(tmp_path))
    names = [r["module_name"] for r in results]
    assert names == sorted(names)


# ---------------------------------------------------------------------------
# Integration: load_all_functions uses extracted schema
# ---------------------------------------------------------------------------

def _collect_logs(mock_logger: MagicMock) -> list[str]:
    """Flatten a mock logger's warning/info/error calls into rendered messages."""
    messages = []
    for call in mock_logger.warning.call_args_list:
        args = call.args
        if args:
            fmt = args[0]
            rest = args[1:]
            try:
                messages.append(fmt % rest if rest else fmt)
            except TypeError:
                messages.append(str(args))
    return messages


def test_load_uses_extracted_schema_and_logs_warning():
    """A file containing CREATE OR REPLACE FUNCTION OFSERM.FOO should be
    stored at graph:OFSERM:FOO and emit a WARNING about multi-schema scope."""
    sql = (
        "CREATE OR REPLACE FUNCTION OFSERM.TEST_PEN_FN(\n"
        "  P VARCHAR2\n"
        ") RETURN VARCHAR2 AS BEGIN\n"
        "  INSERT INTO T (C) VALUES (1);\n"
        "  RETURN 'OK';\n"
        "END;\n"
    )

    mock_redis = MagicMock()
    storage: dict = {}
    mock_redis.set.side_effect = lambda k, v: storage.update({k: v})
    mock_redis.get.side_effect = lambda k: storage.get(k)

    mock_logger = MagicMock()

    with patch(
        "src.parsing.loader._resolve_functions_dir", return_value="/fake/dir"
    ), patch(
        "glob.glob", return_value=["/fake/dir/TEST_PEN_FN.sql"]
    ), patch(
        "src.parsing.loader.is_graph_stale", return_value=True
    ), patch(
        "builtins.open", mock_open(read_data=sql)
    ), patch(
        "src.parsing.loader.build_function_graph",
        return_value={"nodes": [], "edges": []},
    ), patch(
        "src.parsing.loader.store_function_graph", return_value=True
    ) as mock_store, patch(
        "src.parsing.loader.store_raw_source", return_value=True
    ), patch(
        "src.parsing.loader.build_cross_function_graph",
        return_value={"nodes": [], "edges": []},
    ), patch(
        "src.parsing.loader.build_global_column_index", return_value={}
    ), patch(
        "src.parsing.loader.resolve_execution_order", return_value=[]
    ), patch(
        "src.parsing.loader.build_alias_map", return_value={}
    ), patch(
        "src.parsing.loader.store_full_graph", return_value=True
    ), patch(
        "src.parsing.loader.store_column_index", return_value=True
    ), patch(
        "src.parsing.loader.logger", mock_logger
    ):
        from src.parsing.loader import load_all_functions

        result = load_all_functions(
            "/fake/dir", "OFSMDM", mock_redis, force_reparse=True
        )

    # The store call should have used OFSERM, not OFSMDM.
    call_schemas = [call.args[1] for call in mock_store.call_args_list]
    assert "OFSERM" in call_schemas
    assert "OFSMDM" not in call_schemas

    # Warning log should mention OFSERM and multi-schema scope.
    warning_messages = _collect_logs(mock_logger)
    assert any(
        "OFSERM" in msg and "W35" in msg for msg in warning_messages
    ), f"Expected OFSERM/W35 warning, got: {warning_messages}"

    assert result["functions_parsed"] == 1


def test_load_passes_through_matching_schema():
    """When the extracted schema matches the passed-in schema, no warning."""
    sql = (
        "CREATE OR REPLACE FUNCTION OFSMDM.FN_FOO RETURN VARCHAR2 AS BEGIN\n"
        "  RETURN 'OK';\nEND;\n"
    )
    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    mock_logger = MagicMock()

    with patch(
        "src.parsing.loader._resolve_functions_dir", return_value="/fake/dir"
    ), patch(
        "glob.glob", return_value=["/fake/dir/FN_FOO.sql"]
    ), patch(
        "src.parsing.loader.is_graph_stale", return_value=True
    ), patch(
        "builtins.open", mock_open(read_data=sql)
    ), patch(
        "src.parsing.loader.build_function_graph",
        return_value={"nodes": [], "edges": []},
    ), patch(
        "src.parsing.loader.store_function_graph", return_value=True
    ), patch(
        "src.parsing.loader.store_raw_source", return_value=True
    ), patch(
        "src.parsing.loader.build_cross_function_graph",
        return_value={"nodes": [], "edges": []},
    ), patch(
        "src.parsing.loader.build_global_column_index", return_value={}
    ), patch(
        "src.parsing.loader.resolve_execution_order", return_value=[]
    ), patch(
        "src.parsing.loader.build_alias_map", return_value={}
    ), patch(
        "src.parsing.loader.store_full_graph", return_value=True
    ), patch(
        "src.parsing.loader.store_column_index", return_value=True
    ), patch(
        "src.parsing.loader.logger", mock_logger
    ):
        from src.parsing.loader import load_all_functions

        result = load_all_functions(
            "/fake/dir", "OFSMDM", mock_redis, force_reparse=True
        )

    warning_messages = _collect_logs(mock_logger)
    # Should NOT contain the multi-schema warning.
    assert not any("W35" in msg for msg in warning_messages)
    assert result["functions_parsed"] == 1
