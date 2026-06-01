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

    def test_structured_field_array_cap_present(self):
        """W122-recovery: after the corpus-scale LengthFinishReasonError
        failures (86/333 functions), the prompt must explicitly cap
        each structured-field array length so the JSON output fits
        within max_tokens. The cap is a VOLUME constraint only — the
        field shapes / types are unchanged.
        """
        # The cap directive must be present.
        assert "STRUCTURED-FIELD ARRAY CAP" in DESCRIPTION_SYSTEM_PROMPT
        assert "AT MOST 30 entries" in DESCRIPTION_SYSTEM_PROMPT
        # The pre-cap "COMPLETE lists" phrasing — which the LLM took
        # too literally on large-INSERT functions — must be gone.
        assert "hold the COMPLETE lists" not in DESCRIPTION_SYSTEM_PROMPT
        # The drop-order guidance must be present so the LLM knows
        # WHICH entries to drop when source has >30 (drop generic
        # keys first, keep business-meaningful identifiers).
        assert "Drop in this order" in DESCRIPTION_SYSTEM_PROMPT
        assert "Generic surrogate-key columns" in DESCRIPTION_SYSTEM_PROMPT
        # Sanity check: the three fields the cap applies to.
        assert "tables_read" in DESCRIPTION_SYSTEM_PROMPT
        assert "tables_written" in DESCRIPTION_SYSTEM_PROMPT
        assert "key_columns" in DESCRIPTION_SYSTEM_PROMPT

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


# ---------------------------------------------------------------------------
# W122-recovery upsert guard
#
# The W122d mass re-index hit RateLimitError on every function and
# the W93-rejection upsert overwrote 333 status=approved docs with
# sentinel descriptions + empty embeddings — destroying a working
# KNN-retrievable corpus in the process.
#
# The guard now treats LLM-call sentinels ("(indexing failed: ...)")
# as TRANSIENT failures that must not destroy an existing approved
# doc's description + embedding. Real content rejections (too_short)
# are unaffected — those represent the generator returning a
# genuinely-bad description for which marking the doc failed is the
# correct response.
# ---------------------------------------------------------------------------

class TestW122UpsertGuard:
    @pytest.mark.asyncio
    async def test_sentinel_rejection_preserves_existing_approved_doc(self):
        """When the LLM call fails (sentinel) and the existing doc is
        approved with a real description, the upsert MUST be skipped
        entirely so the description + embedding survive the failure.
        """
        indexer = _make_indexer()
        # Existing approved doc with a real (non-sentinel) description.
        indexer._vector_store.get_function_doc = AsyncMock(return_value={
            "status": "approved",
            "description": "Existing real description with strong content.",
            "source_hash": "deadbeef",
        })

        sentinel = {
            "description": "(indexing failed: LengthFinishReasonError)",
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }
        outcome = await indexer._apply_validation_rejection(
            schema="OFSERM",
            module="ABL_CAR",
            function_name="FN_TARGET",
            desc_result=sentinel,
            reject_reason="sentinel_prefix",
            source_hash="newsh1",
            correlation_id=None,
        )
        assert outcome == "preserved"
        # The crucial assertion: no upsert was issued — the existing
        # doc's description + embedding remain untouched.
        indexer._vector_store.upsert_function.assert_not_called()

    @pytest.mark.asyncio
    async def test_sentinel_rejection_writes_failed_when_no_existing_doc(self):
        """If no prior doc exists, the sentinel rejection still has to
        record SOMETHING so the indexer's status tracking stays
        coherent. Falls through to the standard failed-write path.
        """
        indexer = _make_indexer()
        indexer._vector_store.get_function_doc = AsyncMock(return_value=None)

        sentinel = {
            "description": "(indexing failed: LengthFinishReasonError)",
            "tables_read": ["T1"],
            "tables_written": ["T2"],
            "key_columns": ["C1"],
        }
        outcome = await indexer._apply_validation_rejection(
            schema="OFSERM",
            module="ABL_CAR",
            function_name="FN_NEW",
            desc_result=sentinel,
            reject_reason="sentinel_prefix",
            source_hash="sh1",
            correlation_id=None,
        )
        assert outcome == "failed_written"
        indexer._vector_store.upsert_function.assert_called_once()
        kwargs = indexer._vector_store.upsert_function.call_args.kwargs
        assert kwargs["status"] == "failed"
        assert kwargs["embedding"] is None
        assert kwargs["failure_reason"] == "sentinel_prefix"

    @pytest.mark.asyncio
    async def test_sentinel_rejection_overwrites_existing_failed_doc(self):
        """A previously-failed doc is NOT a "good state" worth preserving.
        Overwrite with the fresh failed record (refreshes failure_reason
        and source_hash).
        """
        indexer = _make_indexer()
        indexer._vector_store.get_function_doc = AsyncMock(return_value={
            "status": "failed",
            "description": "(indexing failed: some-previous-class)",
            "source_hash": "oldhash",
        })

        sentinel = {
            "description": "(indexing failed: LengthFinishReasonError)",
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }
        outcome = await indexer._apply_validation_rejection(
            schema="OFSERM",
            module="ABL_CAR",
            function_name="FN_PREV_FAILED",
            desc_result=sentinel,
            reject_reason="sentinel_prefix",
            source_hash="newhash",
            correlation_id=None,
        )
        assert outcome == "failed_written"
        indexer._vector_store.upsert_function.assert_called_once()

    @pytest.mark.asyncio
    async def test_sentinel_rejection_overwrites_approved_doc_with_sentinel_description(self):
        """Defense-in-depth: if an approved doc somehow already carries
        a sentinel description (e.g., a previous guard bypass left it
        in a malformed state), don't preserve it — that's not a good
        state.
        """
        indexer = _make_indexer()
        indexer._vector_store.get_function_doc = AsyncMock(return_value={
            "status": "approved",
            "description": "(indexing failed: SomeOldClass)",
            "source_hash": "oldhash",
        })

        sentinel = {
            "description": "(indexing failed: LengthFinishReasonError)",
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }
        outcome = await indexer._apply_validation_rejection(
            schema="OFSERM",
            module="ABL_CAR",
            function_name="FN_BAD_APPROVED",
            desc_result=sentinel,
            reject_reason="sentinel_prefix",
            source_hash="newhash",
            correlation_id=None,
        )
        assert outcome == "failed_written"

    @pytest.mark.asyncio
    async def test_too_short_rejection_writes_failed_regardless_of_existing(self):
        """too_short is a real content-quality rejection, not a
        transient API failure. Mark failed as before — even when an
        existing approved doc exists, because the generator now
        produces a worse description than what's stored, but the
        validator says we can't trust the new one either. Recording
        failed gives operators visibility into the regression.
        """
        indexer = _make_indexer()
        indexer._vector_store.get_function_doc = AsyncMock(return_value={
            "status": "approved",
            "description": "Existing real description.",
            "source_hash": "oldhash",
        })

        too_short = {
            "description": "x",  # under DESCRIPTION_MIN_LENGTH
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }
        outcome = await indexer._apply_validation_rejection(
            schema="OFSERM",
            module="ABL_CAR",
            function_name="FN_THIN",
            desc_result=too_short,
            reject_reason="too_short",
            source_hash="newhash",
            correlation_id=None,
        )
        assert outcome == "failed_written"
        indexer._vector_store.upsert_function.assert_called_once()
        kwargs = indexer._vector_store.upsert_function.call_args.kwargs
        assert kwargs["status"] == "failed"
        assert kwargs["failure_reason"] == "too_short"

    @pytest.mark.asyncio
    async def test_index_all_loaded_guards_existing_approved_on_sentinel(self):
        """End-to-end through index_all_loaded: an LLM-call failure
        mid-run must not destroy the 247 approved docs. Simulates the
        exact W122-recovery concern.
        """
        from src.parsing.keyspace import SchemaAwareKeyspace

        indexer = _make_indexer()
        schema = "OFSERM"
        fn_name = "FN_PRECIOUS"
        src = (
            f"CREATE OR REPLACE FUNCTION {schema}.{fn_name} AS "
            f"BEGIN NULL; END;"
        )

        # Existing approved doc — a previously-good description we MUST
        # not destroy.
        indexer._vector_store.get_function_doc = AsyncMock(return_value={
            "status": "approved",
            "description": "Existing real W122a description.",
            "source_hash": "differenthash",  # so we don't skip via cache
        })

        graph_key = SchemaAwareKeyspace.graph_key(schema, fn_name)
        graph_redis = MagicMock()
        graph_redis.keys = MagicMock(return_value=[graph_key.encode()])

        sentinel = {
            "description": "(indexing failed: LengthFinishReasonError)",
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
            new=AsyncMock(return_value=sentinel),
        ), patch.object(
            indexer, "_get_embedding", new=AsyncMock(),
        ) as get_emb, patch.object(
            indexer, "_build_function_to_module_map", return_value={},
        ), patch.object(
            indexer, "_build_function_to_description_map", return_value={},
        ):
            result = await indexer.index_all_loaded(
                graph_redis_client=graph_redis, force=True,
            )

        # No embedding call (sentinel → guard short-circuit).
        get_emb.assert_not_called()
        # No upsert_function call either — the guard preserved the
        # existing approved doc by NOT writing anything.
        indexer._vector_store.upsert_function.assert_not_called()
        # The function counts as an error (the run summary still
        # reflects the LLM failure honestly) but the doc state is
        # preserved.
        per_schema = result["results"][schema]
        assert per_schema["errors"] == 1
        assert per_schema["indexed"] == 0
        # The error_details message should flag preservation.
        err = per_schema["error_details"][0]
        assert "preserved" in err["error"].lower()


# ---------------------------------------------------------------------------
# Targeted-retry path: index_all_loaded(only_failed=True) filters to
# status=failed docs only.
#
# W122-recovery's retry of the 86 LengthFinishReasonError failures must
# NOT touch the 247 already-approved docs. The filter narrows the
# candidate set at the schema-scan stage so no source-read / LLM call
# is ever made against an approved doc during the retry.
# ---------------------------------------------------------------------------

class TestOnlyFailedSelection:
    @pytest.mark.asyncio
    async def test_only_failed_narrows_to_failed_docs(self):
        """only_failed=True must filter the candidate set to functions
        whose existing vector doc has status=failed. Approved docs and
        functions without an existing doc are excluded.
        """
        from src.parsing.keyspace import SchemaAwareKeyspace

        indexer = _make_indexer()
        schema = "OFSERM"

        # Three functions: one failed, one approved, one with no doc.
        # only_failed must reduce this to the single failed one.
        fn_failed = "FN_TARGET_FAILED"
        fn_approved = "FN_LEAVE_ALONE_APPROVED"
        fn_missing = "FN_NEVER_SEEN"
        src_template = (
            "CREATE OR REPLACE FUNCTION OFSERM.{} AS BEGIN NULL; END;"
        )

        graph_keys = [
            SchemaAwareKeyspace.graph_key(schema, fn).encode()
            for fn in (fn_failed, fn_approved, fn_missing)
        ]
        graph_redis = MagicMock()
        graph_redis.keys = MagicMock(return_value=graph_keys)

        # Per-function existing-doc state.
        doc_state = {
            fn_failed: {"status": "failed",
                        "description": "(indexing failed: LengthFinishReasonError)",
                        "source_hash": "sh1"},
            fn_approved: {"status": "approved",
                          "description": "A real W122a description.",
                          "source_hash": "sh2"},
            fn_missing: None,
        }

        async def fake_get_doc(s, fn):
            return doc_state.get(fn)

        indexer._vector_store.get_function_doc = AsyncMock(
            side_effect=fake_get_doc,
        )

        good = {
            "description": _CLEAN_DESCRIPTION,
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }

        # Per-fn source loader returns a stub source per fn_name.
        def fake_raw_source(_client, _schema, fn_name):
            return [src_template.format(fn_name)]

        with patch(
            "src.agents.indexer.discovered_schemas", return_value=[schema],
        ), patch(
            "src.agents.indexer.get_raw_source", side_effect=fake_raw_source,
        ), patch.object(
            indexer, "_generate_description_with_validation",
            new=AsyncMock(return_value=good),
        ) as gen, patch.object(
            indexer, "_get_embedding",
            new=AsyncMock(return_value=[0.1] * 1536),
        ), patch.object(
            indexer, "_build_function_to_module_map", return_value={},
        ), patch.object(
            indexer, "_build_function_to_description_map", return_value={},
        ):
            result = await indexer.index_all_loaded(
                graph_redis_client=graph_redis, only_failed=True,
            )

        # Exactly ONE function was generated — the failed one. The
        # approved doc and missing-doc function were filtered out
        # BEFORE the LLM call.
        assert gen.call_count == 1
        call = gen.call_args_list[0]
        # Generated for fn_failed (first positional arg is the function
        # name).
        assert call.args[0] == fn_failed
        per_schema = result["results"][schema]
        assert per_schema["indexed"] == 1
        # No "skipped" path needed — the 2 non-targets were dropped at
        # the filter stage, not counted as skipped.
        assert per_schema["skipped"] == 0

    @pytest.mark.asyncio
    async def test_only_failed_with_empty_failed_cohort_is_a_no_op(self):
        """only_failed=True against a corpus with zero failed docs must
        not touch anything — defensive against re-running the recovery
        command after recovery is already complete.
        """
        from src.parsing.keyspace import SchemaAwareKeyspace

        indexer = _make_indexer()
        schema = "OFSERM"
        fn = "FN_ALL_GOOD"
        graph_redis = MagicMock()
        graph_redis.keys = MagicMock(
            return_value=[SchemaAwareKeyspace.graph_key(schema, fn).encode()],
        )
        indexer._vector_store.get_function_doc = AsyncMock(return_value={
            "status": "approved",
            "description": "A good description.",
            "source_hash": "ok",
        })

        with patch(
            "src.agents.indexer.discovered_schemas", return_value=[schema],
        ), patch(
            "src.agents.indexer.get_raw_source",
            return_value=["CREATE OR REPLACE FUNCTION OFSERM.FN_ALL_GOOD AS BEGIN NULL; END;"],
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
                graph_redis_client=graph_redis, only_failed=True,
            )

        gen.assert_not_called()
        get_emb.assert_not_called()
        per_schema = result["results"][schema]
        assert per_schema["indexed"] == 0
        assert per_schema["errors"] == 0
        assert per_schema["skipped"] == 0

    @pytest.mark.asyncio
    async def test_only_failed_false_is_unchanged_baseline(self):
        """When only_failed=False (default), no filtering. Confirms
        the new code path doesn't accidentally fire when not requested.
        """
        from src.parsing.keyspace import SchemaAwareKeyspace

        indexer = _make_indexer()
        schema = "OFSERM"
        fn = "FN_APPROVED"
        # An approved doc with a DIFFERENT source_hash so the cache
        # skip doesn't fire — we want to see the regeneration happen.
        indexer._vector_store.get_function_doc = AsyncMock(return_value={
            "status": "approved",
            "description": "Old description.",
            "source_hash": "different-from-current",
        })
        graph_redis = MagicMock()
        graph_redis.keys = MagicMock(
            return_value=[SchemaAwareKeyspace.graph_key(schema, fn).encode()],
        )

        good = {
            "description": _CLEAN_DESCRIPTION,
            "tables_read": [],
            "tables_written": [],
            "key_columns": [],
        }
        with patch(
            "src.agents.indexer.discovered_schemas", return_value=[schema],
        ), patch(
            "src.agents.indexer.get_raw_source",
            return_value=["CREATE OR REPLACE FUNCTION OFSERM.FN_APPROVED AS BEGIN NULL; END;"],
        ), patch.object(
            indexer, "_generate_description_with_validation",
            new=AsyncMock(return_value=good),
        ) as gen, patch.object(
            indexer, "_get_embedding",
            new=AsyncMock(return_value=[0.1] * 1536),
        ), patch.object(
            indexer, "_build_function_to_module_map", return_value={},
        ), patch.object(
            indexer, "_build_function_to_description_map", return_value={},
        ):
            # only_failed defaults to False; do NOT set it.
            result = await indexer.index_all_loaded(
                graph_redis_client=graph_redis, force=True,
            )

        # Without only_failed, the approved doc IS re-indexed (force=True).
        gen.assert_called_once()
        assert result["results"][schema]["indexed"] == 1
