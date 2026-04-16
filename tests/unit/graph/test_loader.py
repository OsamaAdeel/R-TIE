"""
Stub tests for src.tools.graph.loader — load_all_functions with mocked deps.
"""

import pytest
from unittest.mock import MagicMock, patch, mock_open


def test_load_all_functions_no_sql_files():
    """When the functions directory exists but contains no .sql files,
    load_all_functions returns a warning status with zero counts."""
    with patch("src.tools.graph.loader._resolve_functions_dir", return_value="/fake/dir"), \
         patch("glob.glob", return_value=[]):

        from src.tools.graph.loader import load_all_functions

        mock_redis = MagicMock()
        result = load_all_functions("/fake/dir", "SCH", mock_redis)

        assert result["status"] == "warning"
        assert result["functions_parsed"] == 0
        assert result["functions_skipped"] == 0
        assert result["total_nodes"] == 0


def test_load_all_functions_dir_not_found():
    """When the functions directory cannot be resolved, load_all_functions
    returns an error status."""
    with patch("src.tools.graph.loader._resolve_functions_dir", return_value=None):

        from src.tools.graph.loader import load_all_functions

        mock_redis = MagicMock()
        result = load_all_functions("/nonexistent", "SCH", mock_redis)

        assert result["status"] == "error"
        assert result["functions_parsed"] == 0
        assert len(result["errors"]) >= 1


def test_load_all_functions_parses_single_file():
    """With one .sql file present, load_all_functions parses it and
    returns functions_parsed == 1."""
    fake_sql = "BEGIN\n  INSERT INTO T (C) SELECT 1 FROM DUAL;\nEND;\n"

    mock_redis = MagicMock()
    storage = {}
    mock_redis.set.side_effect = lambda k, v: storage.update({k: v})
    mock_redis.get.side_effect = lambda k: storage.get(k)

    with patch("src.tools.graph.loader._resolve_functions_dir", return_value="/fake/dir"), \
         patch("glob.glob", return_value=["/fake/dir/fn_test.sql"]), \
         patch("src.tools.graph.loader.is_graph_stale", return_value=True), \
         patch("builtins.open", mock_open(read_data=fake_sql)), \
         patch("src.tools.graph.loader.build_cross_function_graph", return_value={"nodes": [], "edges": []}), \
         patch("src.tools.graph.loader.build_global_column_index", return_value={}), \
         patch("src.tools.graph.loader.resolve_execution_order", return_value=["FN_TEST"]), \
         patch("src.tools.graph.loader.build_alias_map", return_value={}), \
         patch("src.tools.graph.loader.store_full_graph", return_value=True), \
         patch("src.tools.graph.loader.store_column_index", return_value=True):

        from src.tools.graph.loader import load_all_functions

        result = load_all_functions("/fake/dir", "SCH", mock_redis, force_reparse=True)

        assert result["functions_parsed"] == 1
        assert result["status"] in ("success", "partial")
