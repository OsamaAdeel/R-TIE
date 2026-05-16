"""W93: indexer validation gate.

Pre-W93 the indexer's description-generation LLM failure handler
returned a sentinel dict whose ``description`` was the literal string
``(indexing failed: <category>)``. The downstream call site at
:meth:`IndexerAgent.index_all_loaded` then embedded that sentinel via
OpenAI and upserted the doc with ``status="approved"``, producing four
OFSERM docs unfindable via KNN despite looking healthy on every
count-based probe. The indexer was lying about its own state.

W93 adds a validation gate at the indexer that refuses to mark a doc
``approved`` when the description is sentinel-shaped or shorter than
:data:`DESCRIPTION_MIN_LENGTH`. Rejected docs land with
``status="failed"`` and no embedding (so KNN silently excludes them).
A boot-time check in :mod:`src.main` flags any approved doc that still
carries the sentinel shape so future regressions in different code
paths fail loudly rather than silently degrade retrieval.

These tests pin the validator rules and the call-site wiring.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# IndexerAgent.__init__ constructs OpenAIEmbeddings which refuses to
# instantiate without a key. Tests never *use* the embedding client
# (every call site is patched), but the constructor still runs — so
# stamp a dummy key before the import resolves anything from openai.
os.environ.setdefault("OPENAI_API_KEY", "test-key-w93")

from src.agents.indexer import (  # noqa: E402  (after env stamping)
    DESCRIPTION_MIN_LENGTH,
    INDEXING_FAILED_SENTINEL_PREFIX,
    IndexerAgent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_indexer() -> IndexerAgent:
    """Construct an IndexerAgent with a mocked VectorStore.

    The vector store mock is sufficient because every test in this
    module exercises either the static validator or the call-site path
    that *would* invoke the vector store; we never need a real Redis
    or a real OpenAI client.
    """
    vs = MagicMock()
    vs.ensure_index = AsyncMock(return_value=True)
    vs.upsert_function = AsyncMock(return_value=True)
    vs.get_function_doc = AsyncMock(return_value=None)
    return IndexerAgent(vector_store=vs)


def _good_description(length: int = 500) -> str:
    # Three-paragraph-shape filler well above the floor, deterministic.
    return "X" * length


# ---------------------------------------------------------------------------
# Validator — pure-function rules
# ---------------------------------------------------------------------------

class TestValidator:
    def test_accepts_real_description(self):
        ok, reason = IndexerAgent._validate_description_result(
            {"description": _good_description(800)}
        )
        assert ok is True
        assert reason == ""

    def test_accepts_thin_but_legitimate_description(self):
        """The 500-999 char bucket is real-but-stunted, not failed.

        The floor must be well below 500 so legitimate single-paragraph
        descriptions still pass.
        """
        ok, reason = IndexerAgent._validate_description_result(
            {"description": _good_description(150)}
        )
        assert ok is True
        assert reason == ""

    def test_rejects_indexing_failed_sentinel(self):
        """The canonical failure shape: ``(indexing failed: LengthFinishReasonError)``."""
        ok, reason = IndexerAgent._validate_description_result(
            {"description": "(indexing failed: LengthFinishReasonError)"}
        )
        assert ok is False
        assert reason == "sentinel_prefix"

    def test_rejects_sentinel_even_if_padded_long(self):
        """Sentinel detection must precede the length check.

        If a future failure-handler returned ``(indexing failed: …)``
        with extra context concatenated, we still want the specific
        ``sentinel_prefix`` reason rather than a generic ``too_short``
        (which wouldn't even fire on a long sentinel string).
        """
        padded = INDEXING_FAILED_SENTINEL_PREFIX + " X" * 500
        ok, reason = IndexerAgent._validate_description_result(
            {"description": padded}
        )
        assert ok is False
        assert reason == "sentinel_prefix"

    def test_rejects_too_short(self):
        ok, reason = IndexerAgent._validate_description_result(
            {"description": "tiny"}
        )
        assert ok is False
        assert reason == "too_short"

    def test_rejects_empty_string(self):
        ok, reason = IndexerAgent._validate_description_result(
            {"description": ""}
        )
        assert ok is False
        assert reason == "too_short"

    def test_rejects_whitespace_only(self):
        ok, reason = IndexerAgent._validate_description_result(
            {"description": "   \n\t  "}
        )
        assert ok is False
        assert reason == "too_short"

    def test_rejects_missing_description_key(self):
        ok, reason = IndexerAgent._validate_description_result({})
        assert ok is False
        assert reason == "too_short"

    def test_min_length_is_exposed_as_module_constant(self):
        """The boot-time check in main.py imports both constants from
        ``src.agents.indexer``. If a reviewer renames either, both
        producer and consumer need to follow.
        """
        assert DESCRIPTION_MIN_LENGTH > 42  # above the 42-char sentinel
        assert DESCRIPTION_MIN_LENGTH < 500  # below the real-but-stunted bucket
        assert INDEXING_FAILED_SENTINEL_PREFIX == "(indexing failed:"


# ---------------------------------------------------------------------------
# Wiring — when validation fails, the call sites must:
#   1. NOT compute an embedding (saves OpenAI cost on a known-bad doc)
#   2. upsert with status="failed", embedding=None, failure_reason set
# ---------------------------------------------------------------------------

class TestIndexModuleWiring:
    @pytest.mark.asyncio
    async def test_sentinel_description_triggers_failed_upsert(self):
        indexer = _make_indexer()

        # Stub the LLM step to return the sentinel that the real
        # _generate_description returns on LLM exception.
        sentinel = {
            "description": "(indexing failed: LengthFinishReasonError)",
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }

        with patch.object(
            indexer, "_generate_description",
            new=AsyncMock(return_value=sentinel),
        ), patch.object(
            indexer, "_get_embedding",
            new=AsyncMock(return_value=[0.0] * 1536),
        ) as get_emb, patch.object(
            indexer, "_scan_module_functions",
            return_value=[{"name": "FN_BAD", "source": (
                "CREATE OR REPLACE FUNCTION OFSMDM.FN_BAD AS BEGIN NULL; END;"
            )}],
        ):
            result = await indexer.index_module("test_module", force=True)

        # No embedding API call — the gate fired before we paid for it.
        get_emb.assert_not_called()

        # upsert was called with status=failed and embedding=None.
        indexer._vector_store.upsert_function.assert_called_once()
        kwargs = indexer._vector_store.upsert_function.call_args.kwargs
        assert kwargs["status"] == "failed"
        assert kwargs["embedding"] is None
        assert kwargs["failure_reason"] == "sentinel_prefix"
        assert kwargs["function_name"] == "FN_BAD"

        # The function counts as an error, not as indexed.
        assert result["indexed"] == 0
        assert result["errors"] == 1
        assert "description rejected: sentinel_prefix" in (
            result["error_details"][0]["error"]
        )

    @pytest.mark.asyncio
    async def test_short_description_triggers_failed_upsert(self):
        indexer = _make_indexer()
        thin = {
            "description": "tiny",  # well under the floor
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }

        with patch.object(
            indexer, "_generate_description",
            new=AsyncMock(return_value=thin),
        ), patch.object(
            indexer, "_get_embedding",
            new=AsyncMock(return_value=[0.0] * 1536),
        ) as get_emb, patch.object(
            indexer, "_scan_module_functions",
            return_value=[{"name": "FN_TINY", "source": (
                "CREATE OR REPLACE FUNCTION OFSMDM.FN_TINY AS BEGIN NULL; END;"
            )}],
        ):
            await indexer.index_module("test_module", force=True)

        get_emb.assert_not_called()
        kwargs = indexer._vector_store.upsert_function.call_args.kwargs
        assert kwargs["status"] == "failed"
        assert kwargs["failure_reason"] == "too_short"

    @pytest.mark.asyncio
    async def test_valid_description_uses_approved_path(self):
        """Sanity-check that the gate doesn't reject the happy path."""
        indexer = _make_indexer()
        good = {
            "description": _good_description(800),
            "tables_read": ["T1"],
            "tables_written": ["T2"],
            "key_columns": ["C1"],
        }

        with patch.object(
            indexer, "_generate_description",
            new=AsyncMock(return_value=good),
        ), patch.object(
            indexer, "_get_embedding",
            new=AsyncMock(return_value=[0.1] * 1536),
        ) as get_emb, patch.object(
            indexer, "_scan_module_functions",
            return_value=[{"name": "FN_OK", "source": (
                "CREATE OR REPLACE FUNCTION OFSMDM.FN_OK AS BEGIN NULL; END;"
            )}],
        ):
            result = await indexer.index_module("test_module", force=True)

        get_emb.assert_called_once()
        kwargs = indexer._vector_store.upsert_function.call_args.kwargs
        assert kwargs["status"] == "approved"
        assert kwargs["embedding"] is not None
        assert kwargs.get("failure_reason") in (None, "")
        assert result["indexed"] == 1
        assert result["errors"] == 0

    @pytest.mark.asyncio
    async def test_failed_doc_is_retried_on_next_pass(self):
        """W93's skip-if-unchanged check must NOT skip failed docs.

        Pre-W93 the source_hash match alone caused the next indexing
        pass to skip the doc, so a failed doc would stay failed forever
        unless the source changed. With the gate, an existing
        ``status="failed"`` doc must always be re-attempted because by
        definition it never got a real description.
        """
        indexer = _make_indexer()
        good = {
            "description": _good_description(800),
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }
        # Existing doc: same source_hash but status=failed -> must NOT skip.
        # (The hash here is whatever _compute_source_hash returns for the
        # source string below; we just need the call site to find a match.)
        src = "CREATE OR REPLACE FUNCTION OFSMDM.FN_RETRY AS BEGIN NULL; END;"
        existing_hash = IndexerAgent._compute_source_hash(src)
        indexer._vector_store.get_function_doc = AsyncMock(return_value={
            "source_hash": existing_hash,
            "status": "failed",
        })

        with patch.object(
            indexer, "_generate_description",
            new=AsyncMock(return_value=good),
        ), patch.object(
            indexer, "_get_embedding",
            new=AsyncMock(return_value=[0.1] * 1536),
        ), patch.object(
            indexer, "_scan_module_functions",
            return_value=[{"name": "FN_RETRY", "source": src}],
        ):
            result = await indexer.index_module("test_module", force=False)

        # Re-attempted, succeeded, marked approved.
        assert result["indexed"] == 1
        assert result["skipped"] == 0
        kwargs = indexer._vector_store.upsert_function.call_args.kwargs
        assert kwargs["status"] == "approved"

    @pytest.mark.asyncio
    async def test_approved_doc_with_matching_hash_is_skipped(self):
        """Inverse of the retry-on-failed test: the happy-path skip
        still works when the existing doc is genuinely up-to-date.
        """
        indexer = _make_indexer()
        src = "CREATE OR REPLACE FUNCTION OFSMDM.FN_DONE AS BEGIN NULL; END;"
        existing_hash = IndexerAgent._compute_source_hash(src)
        indexer._vector_store.get_function_doc = AsyncMock(return_value={
            "source_hash": existing_hash,
            "status": "approved",
        })

        with patch.object(
            indexer, "_generate_description",
            new=AsyncMock(return_value={"description": _good_description()}),
        ) as gen, patch.object(
            indexer, "_get_embedding",
            new=AsyncMock(return_value=[0.1] * 1536),
        ) as get_emb, patch.object(
            indexer, "_scan_module_functions",
            return_value=[{"name": "FN_DONE", "source": src}],
        ):
            result = await indexer.index_module("test_module", force=False)

        # Neither the LLM nor the embedding API was called — both should
        # be skipped when an approved doc with matching hash exists.
        gen.assert_not_called()
        get_emb.assert_not_called()
        assert result["skipped"] == 1
        assert result["indexed"] == 0
