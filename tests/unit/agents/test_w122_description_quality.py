"""W122 (a + b + c): description-quality cluster at the indexer.

These tests pin the W122a post-generation validator, the W122c
manifest-ingestion plumbing, and the W122a retry-on-validation-failure
wrapper. The W122b truncation lift is tested indirectly by asserting
:data:`MAX_SOURCE_CHARS` exposes the new cap as a module constant.

Out of scope here (deliberate): live LLM behavior — the actual prompt's
effectiveness is judged by the 5-function gate-1 harness in Stage B,
not by unit tests. These tests cover the *mechanism* (signature,
retry, validator rules) only.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# IndexerAgent.__init__ constructs OpenAIEmbeddings which refuses to
# instantiate without a key (same constraint as the W93 tests). Stamp
# a dummy key before any module under src.agents is imported.
os.environ.setdefault("OPENAI_API_KEY", "test-key-w122")

from src.agents.indexer import (  # noqa: E402
    DESCRIPTION_SYSTEM_PROMPT,
    INDEXING_FAILED_SENTINEL_PREFIX,
    IndexerAgent,
    MAX_SOURCE_CHARS,
    _W122A_FORBIDDEN_PHRASES,
    _W122A_GENERIC_OPENINGS,
    _W122A_MAX_WORDS,
    _validate_description,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_indexer() -> IndexerAgent:
    """IndexerAgent with a mocked VectorStore. No real Redis / OpenAI."""
    vs = MagicMock()
    vs.ensure_index = AsyncMock(return_value=True)
    vs.upsert_function = AsyncMock(return_value=True)
    vs.get_function_doc = AsyncMock(return_value=None)
    return IndexerAgent(vector_store=vs)


# A clean function-specific opening, well under 150 words. Used as the
# happy-path description in retry-wrapper tests.
_CLEAN_DESCRIPTION = (
    "Enforces RRP eligibility via two threshold gates on customer "
    "balance and risk-weight. Distinguishes ABL-specific assets via "
    "RTLGRAN / RTLLVE codes; applies a 0.002 deduction ratio when "
    "F_BIA_QUALIFIED_IND is true. Reads FSI_RRP_INPUT and "
    "STG_CUST_BAL_POSITIONS, writes FCT_RRP_EXPOSURES."
)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

class TestModuleConstants:
    def test_max_source_chars_lifted_to_12000(self):
        """W122b: cap raised 3000 -> 12000 after the W108 TLS check."""
        assert MAX_SOURCE_CHARS == 12000

    def test_indexer_max_tokens_default_lifted_to_4000(self):
        """W122b output-budget companion lift.

        Gate-1's first run surfaced 4/5 functions hitting
        ``LengthFinishReasonError`` because the W122b source-cap lift
        (3000 -> 12000) made many more tables/columns visible to the
        LLM; the redesigned prompt requires COMPLETE structured-field
        arrays in JSON output, and the unchanged ``max_tokens=2000``
        ceiling couldn't fit both the description and the arrays for
        functions with 60+ columns visible. Raising the default to
        4000 addresses the root cause directly.

        The default is pinned here so a future reviewer who lowers it
        for cost reasons sees this test fail and gets the rationale
        before changing it.
        """
        import inspect
        sig = inspect.signature(IndexerAgent.__init__)
        assert sig.parameters["max_tokens"].default == 4000

    def test_max_words_is_design_cap(self):
        assert _W122A_MAX_WORDS == 150

    def test_forbidden_phrases_cover_meta_language(self):
        """Sanity check the design's call-outs are wired in."""
        joined = " ".join(_W122A_FORBIDDEN_PHRASES)
        assert "keywords and concepts" in joined
        assert "discoverable" in joined
        assert "domain keywords" in joined

    def test_generic_openings_cover_bad_patterns(self):
        joined = " ".join(_W122A_GENERIC_OPENINGS)
        assert "this function populates" in joined
        assert "this pl/sql function" in joined
        assert "the function is responsible for" in joined

    def test_system_prompt_replaced(self):
        """Pre-W122a markers should be gone; W122a markers should be present."""
        # Old keyword seed list — must NOT be present in the redesigned prompt.
        # The pre-W122 prompt opened "You are a PL/SQL documentation specialist".
        assert "documentation specialist" not in DESCRIPTION_SYSTEM_PROMPT
        # The pre-W122 exhaustive-coverage directive included
        # "Include EVERY table name". W122a explicitly rejects this
        # framing (the structured fields hold the full lists).
        assert "Include EVERY table name" not in DESCRIPTION_SYSTEM_PROMPT
        # New markers from the W122a design.
        assert "150 words" in DESCRIPTION_SYSTEM_PROMPT
        # The W122a prompt explicitly accounts for an analyst-written
        # manifest description in the user message ("If a manifest
        # description is provided …"). Lowercase here because the
        # all-caps "MANIFEST DESCRIPTION" header lives in the user
        # message that _generate_description assembles, not the
        # system prompt.
        assert "manifest description" in DESCRIPTION_SYSTEM_PROMPT
        assert "FORBIDDEN PATTERNS" in DESCRIPTION_SYSTEM_PROMPT
        # JSON output shape preserved (vector-store schema depends on it).
        assert "tables_read" in DESCRIPTION_SYSTEM_PROMPT
        assert "tables_written" in DESCRIPTION_SYSTEM_PROMPT
        assert "key_columns" in DESCRIPTION_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# W122a validator — pure-function rules
# ---------------------------------------------------------------------------

class TestValidator:
    def test_accepts_clean_short_description(self):
        ok, reason = _validate_description(_CLEAN_DESCRIPTION)
        assert ok is True
        assert reason is None

    def test_rejects_over_length(self):
        # 200 single-letter "words" — well over the 150-word cap.
        over = "Enforces RRP eligibility " + " ".join(["foo"] * 200)
        ok, reason = _validate_description(over)
        assert ok is False
        assert reason is not None
        assert "DESCRIPTION_TOO_LONG" in reason
        assert "150" in reason  # the limit is surfaced in the error

    def test_accepts_at_cap_word_count(self):
        # Exactly 150 words — should pass.
        text = " ".join(["computes"] * 150)
        ok, _ = _validate_description(text)
        assert ok is True

    def test_rejects_each_forbidden_phrase(self):
        """Each entry in _W122A_FORBIDDEN_PHRASES must trigger rejection."""
        for phrase in _W122A_FORBIDDEN_PHRASES:
            # Embed in an otherwise-clean opening so only the forbidden
            # phrase trips the validator. Keep length comfortably under
            # the 150-word cap so length isn't the failure cause.
            text = (
                f"Enforces RRP eligibility via two threshold gates. "
                f"Notes that {phrase} matters here."
            )
            ok, reason = _validate_description(text)
            assert ok is False, f"Expected rejection for: {phrase!r}"
            assert reason is not None
            assert "DESCRIPTION_HAS_FORBIDDEN_PATTERN" in reason
            assert phrase in reason

    def test_rejects_each_generic_opening(self):
        """Each pre-W122a generic opening must trip the validator."""
        for opening in _W122A_GENERIC_OPENINGS:
            text = (
                f"{opening} the FSI_RRP_INPUT table for the batch "
                f"and writes to FCT_RRP_EXPOSURES."
            )
            ok, reason = _validate_description(text)
            assert ok is False, f"Expected rejection for opening: {opening!r}"
            assert reason is not None
            assert "DESCRIPTION_GENERIC_OPENING" in reason
            assert opening in reason

    def test_generic_opening_check_is_case_insensitive(self):
        text = (
            "This Function Populates the FSI_RRP_INPUT table for the "
            "batch run."
        )
        ok, reason = _validate_description(text)
        assert ok is False
        assert reason is not None
        assert "DESCRIPTION_GENERIC_OPENING" in reason

    def test_forbidden_phrase_check_is_case_insensitive(self):
        text = (
            "Enforces RRP eligibility via two threshold gates. "
            "Keywords And Concepts: RRP, RTLGRAN."
        )
        ok, reason = _validate_description(text)
        assert ok is False
        assert reason is not None
        assert "DESCRIPTION_HAS_FORBIDDEN_PATTERN" in reason

    def test_rejects_empty(self):
        ok, reason = _validate_description("")
        assert ok is False
        assert reason is not None
        assert "DESCRIPTION_EMPTY" in reason

    def test_does_not_reject_legitimate_basel_terms_in_context(self):
        """The validator must not block legitimate domain terms — the
        Basel-keyword problem is solved by the prompt redesign, not by
        post-gen string filtering. The validator only catches
        meta-language and generic openings.
        """
        text = (
            "Computes operational-risk RWA under the Basic Indicator "
            "Approach. Uses N_GROSS_INCOME from FSI_GL_DATA across "
            "the last 3 fiscal years; applies the 0.15 beta factor."
        )
        ok, reason = _validate_description(text)
        assert ok is True, (
            f"Legitimate Basel-context description was rejected: {reason}"
        )


# ---------------------------------------------------------------------------
# W122c — manifest description threaded into the LLM user message
# ---------------------------------------------------------------------------

class TestManifestIngestion:
    @pytest.mark.asyncio
    async def test_manifest_description_appears_in_user_message(self):
        """When manifest_desc is provided, it must appear in the
        HumanMessage payload alongside the function name and source.
        """
        indexer = _make_indexer()
        captured_messages = {}

        class _FakeResponse:
            content = (
                '{"description": "Enforces RRP eligibility via two '
                'threshold gates on customer balance.", '
                '"tables_read": [], "tables_written": [], '
                '"key_columns": []}'
            )

        async def _fake_ainvoke(messages):
            captured_messages["payload"] = messages
            return _FakeResponse()

        fake_llm = MagicMock()
        fake_llm.ainvoke = _fake_ainvoke

        with patch("src.agents.indexer.create_llm", return_value=fake_llm):
            await indexer._generate_description(
                "FN_G_TEST_CSTM",
                "CREATE OR REPLACE FUNCTION FN_G_TEST_CSTM AS BEGIN NULL; END;",
                manifest_desc="cstm Granularity test Function",
            )

        human_msg = captured_messages["payload"][1]
        body = human_msg.content
        assert "MANIFEST DESCRIPTION" in body
        assert "cstm Granularity test Function" in body
        assert "FN_G_TEST_CSTM" in body

    @pytest.mark.asyncio
    async def test_no_manifest_block_when_not_provided(self):
        """When manifest_desc is None, the MANIFEST DESCRIPTION block
        must be absent — we don't want a stub block confusing the LLM.
        """
        indexer = _make_indexer()
        captured = {}

        class _FakeResponse:
            content = (
                '{"description": "Enforces RRP eligibility via two '
                'threshold gates.", "tables_read": [], '
                '"tables_written": [], "key_columns": []}'
            )

        async def _fake_ainvoke(messages):
            captured["payload"] = messages
            return _FakeResponse()

        fake_llm = MagicMock()
        fake_llm.ainvoke = _fake_ainvoke

        with patch("src.agents.indexer.create_llm", return_value=fake_llm):
            await indexer._generate_description(
                "FN_X",
                "CREATE OR REPLACE FUNCTION FN_X AS BEGIN NULL; END;",
            )

        body = captured["payload"][1].content
        assert "MANIFEST DESCRIPTION" not in body
        assert "FN_X" in body

    @pytest.mark.asyncio
    async def test_retry_reason_appears_in_user_message(self):
        """The retry-reason from the W122a wrapper must surface in the
        user message so the LLM has specific guidance for the rewrite.
        """
        indexer = _make_indexer()
        captured = {}

        class _FakeResponse:
            content = (
                '{"description": "Computes operational-risk RWA under '
                'the Basic Indicator Approach.", "tables_read": [], '
                '"tables_written": [], "key_columns": []}'
            )

        async def _fake_ainvoke(messages):
            captured["payload"] = messages
            return _FakeResponse()

        fake_llm = MagicMock()
        fake_llm.ainvoke = _fake_ainvoke

        with patch("src.agents.indexer.create_llm", return_value=fake_llm):
            await indexer._generate_description(
                "FN_X",
                "CREATE OR REPLACE FUNCTION FN_X AS BEGIN NULL; END;",
                retry_reason="DESCRIPTION_TOO_LONG: 200 words (limit 150).",
            )

        body = captured["payload"][1].content
        assert "PREVIOUS ATTEMPT WAS REJECTED FOR" in body
        assert "DESCRIPTION_TOO_LONG" in body


# ---------------------------------------------------------------------------
# W122a retry wrapper
# ---------------------------------------------------------------------------

class TestRetryWrapper:
    @pytest.mark.asyncio
    async def test_first_attempt_clean_passes_through(self):
        """When the first attempt is W122a-valid, the wrapper returns
        immediately — no retries, no extra LLM calls.
        """
        indexer = _make_indexer()
        good = {
            "description": _CLEAN_DESCRIPTION,
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }
        with patch.object(
            indexer, "_generate_description",
            new=AsyncMock(return_value=good),
        ) as gen:
            result = await indexer._generate_description_with_validation(
                "FN_OK", "source-stub",
            )
        assert result is good
        assert gen.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_validation_failure(self):
        """Bad first attempt -> retry with retry_reason -> good attempt.
        The wrapper returns the good attempt and the retry call carries
        the rejection reason.
        """
        indexer = _make_indexer()
        bad = {
            "description": "This function populates the table for the batch.",
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }
        good = {
            "description": _CLEAN_DESCRIPTION,
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }
        gen_mock = AsyncMock(side_effect=[bad, good])
        with patch.object(indexer, "_generate_description", new=gen_mock):
            result = await indexer._generate_description_with_validation(
                "FN_X", "source-stub",
            )
        assert result is good
        assert gen_mock.call_count == 2
        # Second call must carry retry_reason describing the generic-opening
        # failure.
        second_call = gen_mock.call_args_list[1]
        assert second_call.kwargs.get("retry_reason") is not None
        assert "GENERIC_OPENING" in second_call.kwargs["retry_reason"]

    @pytest.mark.asyncio
    async def test_accepts_persistently_bad_after_retries(self):
        """Persistent W122a failure must not crash indexing. The
        wrapper logs an error and returns the last attempt; the W93
        downstream gate still rejects sentinel / too-short outputs.
        """
        indexer = _make_indexer()
        bad = {
            "description": "This function populates the table.",
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }
        gen_mock = AsyncMock(return_value=bad)
        with patch.object(indexer, "_generate_description", new=gen_mock):
            result = await indexer._generate_description_with_validation(
                "FN_BAD", "source-stub", max_retries=2,
            )
        # Initial attempt + 2 retries = 3 calls total.
        assert gen_mock.call_count == 3
        # Last attempt is returned despite being invalid.
        assert result is bad

    @pytest.mark.asyncio
    async def test_sentinel_passes_through_without_retry(self):
        """W93 sentinel signals an LLM-call exception, not a quality
        issue. Re-prompting under identical conditions would burn API
        budget; the wrapper short-circuits and lets W93's downstream
        gate handle it.
        """
        indexer = _make_indexer()
        sentinel = {
            "description": "(indexing failed: LengthFinishReasonError)",
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }
        gen_mock = AsyncMock(return_value=sentinel)
        with patch.object(indexer, "_generate_description", new=gen_mock):
            result = await indexer._generate_description_with_validation(
                "FN_FAILED", "source-stub",
            )
        # Only one LLM call — no retry on sentinel.
        assert gen_mock.call_count == 1
        assert result is sentinel

    @pytest.mark.asyncio
    async def test_manifest_desc_threaded_into_all_attempts(self):
        """Across initial attempt + retries, manifest_desc must be
        passed to every underlying _generate_description call so the
        analyst context is preserved on rewrites.
        """
        indexer = _make_indexer()
        bad = {
            "description": "The function is responsible for batch loads.",
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }
        good = {
            "description": _CLEAN_DESCRIPTION,
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }
        gen_mock = AsyncMock(side_effect=[bad, good])
        with patch.object(indexer, "_generate_description", new=gen_mock):
            await indexer._generate_description_with_validation(
                "FN_X", "src", manifest_desc="ground truth from analyst",
            )
        for call in gen_mock.call_args_list:
            assert call.kwargs.get("manifest_desc") == "ground truth from analyst"


# ---------------------------------------------------------------------------
# W93 caching behavior must be preserved — W122 changed how descriptions
# are generated, NOT how the source_hash cache decides skip-vs-regenerate.
# The W93 test file pins this in detail; the smoke test below is a
# guard against accidental regression from W122 plumbing.
# ---------------------------------------------------------------------------

class TestCachingPreserved:
    @pytest.mark.asyncio
    async def test_approved_doc_with_matching_hash_still_skipped(self):
        indexer = _make_indexer()
        src = "CREATE OR REPLACE FUNCTION OFSMDM.FN_DONE AS BEGIN NULL; END;"
        existing_hash = IndexerAgent._compute_source_hash(src)
        indexer._vector_store.get_function_doc = AsyncMock(return_value={
            "source_hash": existing_hash,
            "status": "approved",
        })

        with patch.object(
            indexer, "_generate_description_with_validation",
            new=AsyncMock(return_value={"description": "x" * 500}),
        ) as gen, patch.object(
            indexer, "_get_embedding",
            new=AsyncMock(return_value=[0.1] * 1536),
        ), patch.object(
            indexer, "_scan_module_functions",
            return_value=[{"name": "FN_DONE", "source": src}],
        ):
            result = await indexer.index_module("test_module", force=False)

        # The retry wrapper must NOT be invoked when the cache hits.
        gen.assert_not_called()
        assert result["skipped"] == 1
        assert result["indexed"] == 0


# ---------------------------------------------------------------------------
# W122d — force-regenerate path
#
# The W93 source_hash cache keys on source CONTENT, not prompt content.
# After W122a redesigned the description prompt, most source files are
# unchanged so cache-hits would skip nearly every function and persist
# pre-W122 descriptions. The W122d mass re-index must therefore pass
# force=True so every function is regenerated regardless of source_hash.
#
# The existing IndexerAgent.index_module / index_all_loaded already
# implement force=True semantics — the W93 skip-block is wrapped in
# `if not force`. These tests pin that contract specifically for W122d:
# an existing approved doc with matching source_hash MUST be re-indexed
# when force=True. (The sibling W93 test verifies the inverse — same
# doc IS skipped when force=False.)
# ---------------------------------------------------------------------------

class TestForceRegeneratePath:
    @pytest.mark.asyncio
    async def test_index_module_force_bypasses_approved_cache(self):
        """index_module(force=True) must re-index even when an existing
        approved doc has matching source_hash. Inverse of W93's
        ``test_approved_doc_with_matching_hash_is_skipped``.
        """
        indexer = _make_indexer()
        src = "CREATE OR REPLACE FUNCTION OFSMDM.FN_DONE AS BEGIN NULL; END;"
        existing_hash = IndexerAgent._compute_source_hash(src)
        # Existing doc looks healthy (approved, hash matches) — but
        # force=True must override and re-index anyway.
        indexer._vector_store.get_function_doc = AsyncMock(return_value={
            "source_hash": existing_hash,
            "status": "approved",
        })
        good = {
            "description": _CLEAN_DESCRIPTION,
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }

        with patch.object(
            indexer, "_generate_description_with_validation",
            new=AsyncMock(return_value=good),
        ) as gen, patch.object(
            indexer, "_get_embedding",
            new=AsyncMock(return_value=[0.1] * 1536),
        ) as get_emb, patch.object(
            indexer, "_scan_module_functions",
            return_value=[{"name": "FN_DONE", "source": src}],
        ):
            result = await indexer.index_module("test_module", force=True)

        # The cache was bypassed: both the LLM and embedding APIs were
        # called, and the function counts as indexed (not skipped).
        gen.assert_called_once()
        get_emb.assert_called_once()
        assert result["indexed"] == 1
        assert result["skipped"] == 0
        # upsert ran with the fresh approved doc.
        kwargs = indexer._vector_store.upsert_function.call_args.kwargs
        assert kwargs["status"] == "approved"
        assert kwargs["embedding"] is not None

    @pytest.mark.asyncio
    async def test_index_all_loaded_force_bypasses_approved_cache(self):
        """index_all_loaded(force=True) — the path the W122d mass re-index
        actually uses via ``python cli.py index --force`` — must bypass
        the approved-doc cache and re-generate every function.

        This is the binding-contract test for W122d: without this
        guarantee the new W122a prompt would have zero effect on the
        existing corpus because the source_hash cache would short-
        circuit every function.
        """
        from src.parsing.keyspace import SchemaAwareKeyspace

        indexer = _make_indexer()
        schema = "OFSMDM"
        fn_name = "FN_FORCE_TARGET"
        src = (
            f"CREATE OR REPLACE FUNCTION {schema}.{fn_name} AS "
            f"BEGIN NULL; END;"
        )
        existing_hash = IndexerAgent._compute_source_hash(src)
        indexer._vector_store.get_function_doc = AsyncMock(return_value={
            "source_hash": existing_hash,
            "status": "approved",
        })

        # Fake graph_redis_client: SCAN returns one graph key pointing at
        # this function under OFSMDM.
        graph_key = SchemaAwareKeyspace.graph_key(schema, fn_name)
        graph_redis = MagicMock()
        graph_redis.keys = MagicMock(return_value=[graph_key.encode()])

        good = {
            "description": _CLEAN_DESCRIPTION,
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }

        with patch(
            "src.agents.indexer.discovered_schemas", return_value=[schema],
        ), patch(
            "src.agents.indexer.get_raw_source", return_value=[src],
        ), patch.object(
            indexer, "_generate_description_with_validation",
            new=AsyncMock(return_value=good),
        ) as gen, patch.object(
            indexer, "_get_embedding",
            new=AsyncMock(return_value=[0.1] * 1536),
        ) as get_emb, patch.object(
            indexer, "_build_function_to_module_map", return_value={},
        ), patch.object(
            indexer, "_build_function_to_description_map", return_value={},
        ):
            result = await indexer.index_all_loaded(
                graph_redis_client=graph_redis, force=True,
            )

        gen.assert_called_once()
        get_emb.assert_called_once()
        # Per-schema result confirms the function was indexed, not skipped.
        per_schema = result["results"][schema]
        assert per_schema["indexed"] == 1
        assert per_schema["skipped"] == 0

    @pytest.mark.asyncio
    async def test_force_false_baseline_still_skips(self):
        """Inverse sanity check: with force=False the same doc + matching
        hash + approved status MUST still be skipped. Pins that the
        W122d-required override is gated specifically on the force flag,
        not accidentally always-on.
        """
        from src.parsing.keyspace import SchemaAwareKeyspace

        indexer = _make_indexer()
        schema = "OFSMDM"
        fn_name = "FN_NO_FORCE"
        src = (
            f"CREATE OR REPLACE FUNCTION {schema}.{fn_name} AS "
            f"BEGIN NULL; END;"
        )
        existing_hash = IndexerAgent._compute_source_hash(src)
        indexer._vector_store.get_function_doc = AsyncMock(return_value={
            "source_hash": existing_hash,
            "status": "approved",
        })

        graph_key = SchemaAwareKeyspace.graph_key(schema, fn_name)
        graph_redis = MagicMock()
        graph_redis.keys = MagicMock(return_value=[graph_key.encode()])

        with patch(
            "src.agents.indexer.discovered_schemas", return_value=[schema],
        ), patch(
            "src.agents.indexer.get_raw_source", return_value=[src],
        ), patch.object(
            indexer, "_generate_description_with_validation",
            new=AsyncMock(),
        ) as gen, patch.object(
            indexer, "_get_embedding", new=AsyncMock(),
        ) as get_emb, patch.object(
            indexer, "_build_function_to_module_map", return_value={},
        ), patch.object(
            indexer, "_build_function_to_description_map", return_value={},
        ):
            result = await indexer.index_all_loaded(
                graph_redis_client=graph_redis, force=False,
            )

        # Neither LLM nor embedding API touched — cache hit short-
        # circuited everything. This is the path that pre-W122d would
        # have preserved every old description on a mass re-index.
        gen.assert_not_called()
        get_emb.assert_not_called()
        per_schema = result["results"][schema]
        assert per_schema["skipped"] == 1
        assert per_schema["indexed"] == 0

