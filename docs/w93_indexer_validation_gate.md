# W93 — Indexer Validation Gate

**Branch:** `fix/w93-indexer-validation-gate`
**Status:** Ready for review
**Blocks:** W80 v1 close-out (canary measurement requires a clean retrieval corpus)
**Pattern parallel:** W87 (unrecognized-term runtime gate), W45 (empty-retrieval runtime gate). W93 is the same architectural shape — refuse to lie about state — applied at the indexer rather than at request time.

---

## Diagnosis

Audit of [src/tools/vector_store.py](../src/tools/vector_store.py) keys (2026-05-15) surfaced four OFSERM docs with 42-character descriptions reading exactly **`(indexing failed: LengthFinishReasonError)`**, yet marked `status=approved`:

```
rtie:vec:OFSERM:ABL_CAP_MITIGANT_DATA_POPULATION
rtie:vec:OFSERM:ENTITY_INFO_HIER_DATA_POP
rtie:vec:OFSERM:FSI_STD_CAPITAL_ACCT_HEAD_POP
rtie:vec:OFSERM:PARTY_SHAREHOLDING_PERCENT_CALCULATION_FOR_REPORTING_ENTITY
```

### How they got there

[src/agents/indexer.py](../src/agents/indexer.py) `_generate_description` catches LLM exceptions and returns a sentinel dict whose `description` is the literal failure string. The same call site (`index_all_loaded`, ~line 372 pre-W93) then:

1. Embedded the sentinel string via OpenAI (`text-embedding-3-small`).
2. Wrote the doc with `status="approved"`.
3. Added the function to the `indexed` count.

The sentinel-string embedding is uncorrelated with function semantics, so the doc is effectively unfindable by KNN — yet every count-based health probe (`num_docs`, schema counts, status distribution) shows green. The indexer was lying about its own state.

### Compounding observations

| Field | Value |
|---|---|
| Each doc has a real 1536-dim embedding | Yes (6144 bytes) — embedded from the sentinel string |
| `status` | `approved` (the lie) |
| `description_hash` | Identical across all 4: `b4dca0d5e194e68e` — `sha256("(indexing failed: LengthFinishReasonError)")[:16]` |
| `source_hash` | Unique per doc (computed correctly from source) |
| Detectable by count probes | **No** — every count and status check returns green |
| Detectable by FT.SEARCH on `function_name` | Yes (still indexed) |
| Detectable by KNN | Effectively no — embedding is uncorrelated with semantics |

The same audit found **~47 additional OFSERM docs with 500–999 char descriptions** (single-paragraph instead of the typical 3-paragraph 2000–3999 char output). These are *partial generations*, not failures — they pass the W93 floor at 100 chars but represent the next quality-tier issue. Tracked as **W80a** (separate scope; description regeneration, not a one-line config fix).

---

## Design

Three structural changes — gate at the indexer, schema for failed state, regression check at boot.

### 1. Validation gate ([src/agents/indexer.py](../src/agents/indexer.py))

Two module-level constants and one static validator:

```python
INDEXING_FAILED_SENTINEL_PREFIX = "(indexing failed:"
DESCRIPTION_MIN_LENGTH = 100
```

```python
IndexerAgent._validate_description_result(desc_result) -> (is_valid, reason)
```

Rules, in order:

| Check | Reason returned |
|---|---|
| `description` starts with `(indexing failed:` | `sentinel_prefix` |
| Stripped `description` length < `DESCRIPTION_MIN_LENGTH` (100) | `too_short` |

Sentinel check precedes length check so operators see the specific failure category even if a future failure handler pads the sentinel with extra context.

### 2. `status="failed"` instead of `"approved"`

Both call sites (`index_module`, `index_all_loaded`) validate immediately after `_generate_description`. On rejection:

- **No embedding API call** — saves OpenAI cost on a known-bad doc.
- `upsert_function` is called with `status="failed"`, `embedding=None`, `failure_reason=<reason>`.
- The doc is added to the `errors` list with category, not to `indexed`.

`VectorStore.upsert_function` was extended to:
- Accept `embedding: Optional[List[float]]` and skip the `embedding` field when `None`.
- Accept `failure_reason: Optional[str]` and write it as a diagnostic field.
- **Refuse** to write `status="approved"` + `embedding=None` (defense in depth — that combination is the exact state-lie W93 exists to prevent).

A failed doc has the same `rtie:vec:*` key shape as approved docs, so the RediSearch index ingests it normally. But the missing `embedding` field means KNN queries silently exclude it — there's no way for a failed doc to fabricate a retrieval result.

### 3. Retry-on-failed in the skip-if-unchanged check

Pre-W93 the source-hash match alone caused the indexer to skip the doc on subsequent passes, so a failed doc would stay failed forever unless the source changed. Updated to:

```python
if existing
   and existing.get("source_hash") == source_hash
   and existing.get("status") == "approved":
    skipped.append(fn_name)
    continue
```

A `status="failed"` doc is always re-attempted next pass.

### 4. Boot-time regression check ([src/main.py](../src/main.py) lifespan)

After `_vector_store.ensure_index()`, the lifespan calls `VectorStore.scan_for_invalid_approved_docs()`. It iterates every `rtie:vec:*` doc, `HMGET`s `status` + `description`, and flags any approved doc whose description still matches the rejection rules. Findings are logged at `CRITICAL` with the affected schema/function pairs and their lengths.

The check does **not** abort startup. The check is advisory because:

- With the gate in place this should always return an empty list.
- A non-empty result means either legacy docs from before W93 (remediation = run indexer) or a regression in a different code path (remediation = fix that path).
- Aborting on legacy data would block the operator from running the very indexer pass that fixes them.

Once an operator re-indexes the 4 legacy docs the check is silent.

---

## What W93 does NOT do

- **Does not regenerate the 47 stunted single-paragraph descriptions.** Those pass the floor at 100 chars; addressing them is W80a (indexer prompt revision + selective re-run).
- **Does not change the LLM `max_tokens` default.** The pre-W93 failures were `finish_reason=length` — the underlying cause was likely `max_tokens=2000` being too tight for the larger functions. Tuning that is in scope for W80a, not here.
- **Does not retire the sentinel string from `_generate_description`.** The sentinel still gets returned on exception — but the gate now intercepts before it can be embedded or marked approved. Retiring the sentinel entirely would require restructuring the exception path to raise instead of return; deferred to avoid scope creep.
- **Does not modify the runtime retrieval path.** That's W80 v1 (parked on `feat/w80-embedding-input` pending W93 close-out).

## Files changed

| File | Change |
|---|---|
| [src/agents/indexer.py](../src/agents/indexer.py) | + constants, + validator, gate wired into both `index_module` and `index_all_loaded`, retry-on-failed skip rule |
| [src/tools/vector_store.py](../src/tools/vector_store.py) | `upsert_function` accepts optional `embedding`/`failure_reason`, refuses approved+null embedding, + `scan_for_invalid_approved_docs` |
| [src/main.py](../src/main.py) | Lifespan boot-time check post-`ensure_index` |
| [tests/unit/agents/test_w93_indexer_gate.py](../tests/unit/agents/test_w93_indexer_gate.py) | 14 tests: 9 validator + 5 call-site wiring |

## Test plan

1. `python -m pytest tests/unit/agents/test_w93_indexer_gate.py -v` — green (14/14).
2. Re-run indexer on the 4 known-broken docs. They should now either succeed (real description + embedding + approved) or land cleanly as `status=failed` with a populated `failure_reason`. Either is acceptable; both surface honestly.
3. Restart backend with at least one legacy sentinel doc still in Redis. The lifespan emits a `CRITICAL` log line listing the affected `schema:function_name` pairs and their description lengths.
4. After step 2 completes successfully, restart again. The boot-time check should be silent (no `CRITICAL` line).

## Resume sequence

After W93 lands:

1. Resume W80 v1 close-out — checkout `feat/w80-embedding-input`, rebase onto post-W93 main, re-run canaries against the clean corpus.
2. Open W80a for the stunted-description regeneration (47 OFSERM functions, plus the `CAP_CONSL_NON_REGULATORY_ENTITY_SIGNIFICANT_INVESTMENT_IDENTIFICATION` single-paragraph case from the stakeholder-test-2 trace).
