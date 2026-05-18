# W80a — Stunted Description Regeneration (backlog)

**Status:** Logged, not started
**Parent:** W80 v1 (merged at `f2945c5`, 2026-05-16)
**Related:** W93 (indexer validation gate, merged at `51a0ed9`)
**Pattern parallel:** Quality follow-up to W93 — W93 stopped sentinels from being marked approved; W80a addresses the next tier down: legitimate-but-thin descriptions that pass the W93 floor.

---

## Why this exists

After W93 landed (indexer validation gate), the OFSERM corpus distribution looked like this (audit on `main` post-W93 close-out):

| Description length | Count | Notes |
|---|---|---|
| 0–199 chars | 0 | W93 gate now rejects (status="failed") |
| 200–499 chars | 0 | (Bimodal gap — confirms there's no legitimate failure mode in this range) |
| 500–999 chars | ~47 | **Single-paragraph, real but stunted** |
| 1000–1999 chars | ~34 | Two-paragraph |
| 2000–3999 chars | ~81 | Full three-paragraph (LLM default output shape) |

The ~47 functions in the 500–999 bucket are real LLM outputs, not failures — the W93 gate at `DESCRIPTION_MIN_LENGTH=100` correctly accepts them. But they describe their functions in a single paragraph instead of the canonical three-paragraph shape (purpose / read+write tables / calculations and keywords). They produce **weaker embedding signal** than the rich descriptions, which directly degrades KNN ranking for queries that should surface them.

The W80 v1 canary on the significant-investment trace surfaces this as a tangible problem: `CAP_CONSL_NON_REGULATORY_ENTITY_SIGNIFICANT_INVESTMENT_IDENTIFICATION` has only an 864-char description and consistently loses ranking races to its full-bodied siblings even on queries that should rank it first. Post-W80 the canary's recall floor (≥2 of 5) is met, but the *ceiling* (5 of 5, or strong ranking against `CS_INSIGNIFICANT_INVST_*` near-misses) requires uniform description quality.

## Scope

Two work items:

### W80a — description prompt revision + selective re-run

1. **Audit the 47 stunted descriptions.** Group by likely cause: source under-engagement (the LLM didn't see enough of the function), prompt fatigue (the LLM produced a short response despite enough source), or genuine "this function is small enough that one paragraph is appropriate."
2. **Revise [DESCRIPTION_SYSTEM_PROMPT](../src/agents/indexer.py) (currently at indexer.py:64-86)** if the audit shows a systematic prompt-shape issue. The current prompt asks for "2–4 paragraphs" which the LLM treats as a floor; the rich outputs land at 3 paragraphs because the *content guidance* (purpose / tables / calculations) implicitly asks for that shape. Tightening the content guidance — explicitly enumerating "paragraph 1: purpose; paragraph 2: tables read/written; paragraph 3: calculations and keywords" — should produce uniform output.
3. **Bump `max_tokens`** on the indexer's `_generate_description` LLM call from 2000 → 4000. The 4 W93-handled failures were `LengthFinishReasonError`, suggesting `max_tokens` is too tight for rich outputs even when the prompt allows them.
4. **Re-run the indexer in `--force` mode on just the 47 functions.** Use `index_all_loaded` (Phase 3 path), not `index_all_modules` (which has the W93b corpus-pollution footgun). The W93 retry-on-failed logic doesn't trigger here because the docs are status=approved; W80a needs an explicit `--force` re-attempt.
5. **Verify uniform output.** After the re-run, the 500–999 bucket should be empty or contain only genuinely-small functions; the 2000–3999 bucket should hold ≥90% of the corpus.

### Canary expansion (optional but recommended)

The W80 v1 canary floor is ≥2 of 5. Post-W80a we should be able to lift the floor to **≥3 of 5** or **≥4 of 5**, depending on what the audit reveals. The new canary upper bound is the W80b motivation (hybrid BM25 + KNN to capture the last 1–2 functions even when description quality is uniform).

## Non-goals

- **Does not regenerate the W93-failed descriptions.** Those have `status="failed"` and no embedding; they're correctly excluded from KNN. They need separate root-cause investigation (the LLM call itself fails, not just the output shape). Tracked under their own `failure_reason` field.
- **Does not change the W93 floor.** `DESCRIPTION_MIN_LENGTH=100` is correct for catching sentinels; lifting it to e.g. 500 would falsely reject legitimate-but-thin descriptions on small functions. W80a addresses quality at the prompt level, not the gate level.
- **Does not introduce hybrid retrieval.** That's W80b, separate ticket.

## Estimated cost

- ~47 OpenAI calls at the current `gpt-5-mini` rate for description generation
- ~47 OpenAI embedding calls (`text-embedding-3-small`)
- Wall-clock: ~5–10 minutes (the indexer's `asyncio.sleep(2)` between functions dominates)
- Risk: low — W93 gate prevents any new sentinel-as-approved regressions

## Pre-condition

W93 must remain in place (it does — merged at `51a0ed9`). W80a re-runs the indexer; if the LLM call fails for any of the 47, W93's gate catches it and marks failed. No regression to the lying state is possible.
