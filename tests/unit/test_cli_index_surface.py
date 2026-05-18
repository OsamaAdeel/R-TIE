"""W93b — `python cli.py index` default switched to loader-validated path.

Pre-W93b the `index` command called `index_all_modules`, which disk-walks
`db/modules/*` and indexes every .sql file including the ones the loader
rejected. Running it mid-W93-verification repolluted the corpus
(178 → 281 OFSERM docs); cleanup required deleting docs lacking a
`graph:OFSERM:<fn>` backing.

W93b switches the default to `index_all_loaded` (the same Phase-3 path
the backend lifespan uses) and preserves `index_all_modules` behind an
explicit `--from-disk` opt-in.

These tests pin two things:

1. `main()` arg parsing routes `--from-disk` correctly to `cmd_index`.
2. The module docstring documents the new surface — both the default
   safety property and the `--from-disk` opt-in — so a future ticket
   can't silently rename or drop the flag without breaking a test.

We intentionally do NOT exercise `cmd_index` end-to-end (it constructs
real Redis + IndexerAgent clients). That belongs in the W93b smoke
test ([docs/RTIE_Weakness_Log.md](../../docs/RTIE_Weakness_Log.md)
W93b entry).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# cli.py lives at the repo root, not under src/. Add the repo root to
# sys.path so `import cli` works regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# load_dotenv side-effect at cli.py import is safe (no-op if .env.dev
# is missing). OPENAI_API_KEY is not read at import time.
import cli  # noqa: E402


# ---------------------------------------------------------------------------
# Help / docstring surface — documents the new CLI shape
# ---------------------------------------------------------------------------

class TestHelpSurface:
    def test_doc_mentions_from_disk_flag(self):
        """`--from-disk` must appear in the help so users discover the
        opt-in. If a future ticket renames or drops the flag this test
        fails before the docstring drifts out of sync with main().
        """
        assert "--from-disk" in (cli.__doc__ or "")

    def test_doc_warns_loader_prerequisite(self):
        """Default path requires the loader to have run at least once
        (it scans graph:<schema>:<fn> keys the loader populates). The
        help must surface this so first-time users don't get a silent
        zero-indexed result.
        """
        doc = (cli.__doc__ or "").lower()
        assert "loader" in doc and (
            "run.py" in doc or "backend" in doc
        ), "Help must mention loader prerequisite + how to satisfy it"

    def test_doc_describes_default_as_safe(self):
        """The default's selling point is that it matches what RTIE
        actually serves — no loader-rejected docs. Pin the framing so
        the help doesn't drift to neutral language that hides W93b's
        rationale.
        """
        doc = (cli.__doc__ or "").lower()
        assert "loader-validated" in doc or "graph:<schema>:<fn>" in doc


# ---------------------------------------------------------------------------
# Arg parsing — `--from-disk` routes correctly
# ---------------------------------------------------------------------------

class TestArgRouting:
    def _run_main_with_argv(self, argv: list[str]) -> AsyncMock:
        """Patch cmd_index, invoke main() with the given argv, return
        the mock so tests can assert kwargs.
        """
        mock_cmd_index = AsyncMock(return_value=None)
        with patch.object(cli, "cmd_index", mock_cmd_index), patch.object(
            sys, "argv", ["cli.py"] + argv
        ):
            asyncio.run(cli.main())
        return mock_cmd_index

    def test_bare_index_calls_default_path(self):
        """`python cli.py index` must NOT pass from_disk=True. This is
        the W93b safety guarantee — defaulting to disk-walk is exactly
        what got us into the corpus-pollution incident.
        """
        mock = self._run_main_with_argv(["index"])
        mock.assert_awaited_once_with(force=False, from_disk=False)

    def test_index_force_keeps_default_path(self):
        """`--force` must not flip the path selection. Force is a
        property of the cache-skip behavior, not the data source.
        """
        mock = self._run_main_with_argv(["index", "--force"])
        mock.assert_awaited_once_with(force=True, from_disk=False)

    def test_index_from_disk_routes_to_disk_walk(self):
        """`--from-disk` must set from_disk=True so cmd_index calls
        index_all_modules. This is the explicit opt-in.
        """
        mock = self._run_main_with_argv(["index", "--from-disk"])
        mock.assert_awaited_once_with(force=False, from_disk=True)

    def test_index_from_disk_combines_with_force(self):
        """Both flags together must compose — the escape hatch can
        also force re-embedding.
        """
        mock = self._run_main_with_argv(["index", "--from-disk", "--force"])
        mock.assert_awaited_once_with(force=True, from_disk=True)

    def test_index_help_prints_doc_without_calling_cmd_index(self):
        """`--help` must short-circuit before any Redis / indexer
        construction. The user is asking for the help surface, not
        for an index run.
        """
        mock_cmd_index = AsyncMock(return_value=None)
        with patch.object(cli, "cmd_index", mock_cmd_index), patch.object(
            sys, "argv", ["cli.py", "index", "--help"]
        ):
            asyncio.run(cli.main())
        mock_cmd_index.assert_not_awaited()

    def test_no_args_prints_doc(self):
        """Bare `python cli.py` (no command) must not call cmd_index.
        Preserves the pre-W93b documentation-on-no-args behavior.
        """
        mock_cmd_index = AsyncMock(return_value=None)
        with patch.object(cli, "cmd_index", mock_cmd_index), patch.object(
            sys, "argv", ["cli.py"]
        ):
            asyncio.run(cli.main())
        mock_cmd_index.assert_not_awaited()
