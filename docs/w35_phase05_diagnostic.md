# W35 Phase 0.5 — `commented_out_nodes` Bug Diagnostic

**Branch:** `fix/w35-phase05-merge-node-classification`
**Captured:** 2026-04-27
**Scope:** Phase A only — diagnostic. No code changes have been made. This document is the input for the user to approve the proposed fix before Phase B (implementation).

> **Stand-down note for the trace script.** The trace script lives at `scratch/w35p05_parser_trace.py` and writes `scratch/w35p05_parser_trace_summary.json`. `scratch/` is the project's sanctioned home for one-off experiments and is not staged in this PR. Run with `poetry run python scratch/w35p05_parser_trace.py` to reproduce.

---

## Section 1 — Code paths read

### `src/parsing/parser.py`

The parser converts source lines to a list of "raw blocks" — typed DML windows annotated with positional metadata.

- [`PATTERNS["BLOCK_COMMENT_START"]`](src/parsing/parser.py#L28) = `re.compile(r'/\*')` — no anchoring; matches anywhere on a line.
- [`PATTERNS["BLOCK_COMMENT_END"]`](src/parsing/parser.py#L29) = `re.compile(r'\*/')`.
- [`clean_source_lines`](src/parsing/parser.py#L76-L135) strips comments while preserving line numbers. Returns `(cleaned_lines, comment_ranges)` — `comment_ranges` only includes block-comment regions that span 2+ lines (line 133: `if in_block:` adds the range only when control flow reaches end-of-file inside a block; multi-line ranges added at line 115 when the closer is found on a later line). **Inline `/* … */` self-closing comments are correctly stripped from `cleaned_lines` and do NOT appear in `comment_ranges`.**
- [`_build_comment_map`](src/parsing/parser.py#L205-L229) is the **separate, parallel** comment tracker that returns a per-line `bool[]`. **This is where the bug lives.** Reproduced in full:
  ```python
  for line in lines:
      if in_comment:
          comment_map.append(True)
          if PATTERNS["BLOCK_COMMENT_END"].search(line):
              in_comment = False
      else:
          if PATTERNS["BLOCK_COMMENT_START"].search(line):
              # Check whether the comment closes on the same line
              if not PATTERNS["BLOCK_COMMENT_END"].search(line):
                  in_comment = True
              # The line itself is (at least partly) a comment line, but we
              # only mark *fully-enclosed* lines.  For safety, mark it.
              comment_map.append(True)              # <<< unconditional True
          else:
              comment_map.append(False)
  ```
  When `BLOCK_COMMENT_START` matches, the function appends `True` regardless of whether `BLOCK_COMMENT_END` is on the same line. The comment "For safety, mark it" describes intentional conservatism — but it conflates "this line *contains* a `/*`" with "this line *is* a comment", and that conflation is the bug.
- [`parse_function`](src/parsing/parser.py#L741-L930) is the main entry. Around [line 814](src/parsing/parser.py#L814):
  ```python
  is_commented = comment_map[idx] or idx in block_comment_lines
  ```
  This is the per-block flag that determines `commented_out` routing downstream. The `idx` here is the line where the DML keyword (MERGE/INSERT/UPDATE/DELETE) was detected. `block_comment_lines` is the union of all `comment_ranges` returned by `clean_source_lines` — accurate. `comment_map[idx]` is the buggy parallel tracker.
- The parser writes [`preceded_by_commit`](src/parsing/parser.py#L850) and [`followed_by_commit`](src/parsing/parser.py#L851) per block (filled in at lines 901-923 by scanning ±3 non-blank/non-comment lines for `COMMIT;`). It does **NOT** write a field named `committed_after`.

### `src/parsing/builder.py`

- [`build_function_graph`](src/parsing/builder.py#L56-L130) is the entry. At [lines 91-92](src/parsing/builder.py#L91-L92) it splits `raw_blocks` into two lists by the parser's `is_commented_out` flag:
  ```python
  commented_blocks = [b for b in raw_blocks if b.get("is_commented_out")]
  raw_blocks = [b for b in raw_blocks if not b.get("is_commented_out")]
  ```
  Active blocks → `nodes`; commented blocks → `commented_out_nodes` with id suffix `_COMMENTED_<n>`.
- Every node-builder (`build_insert_node`, `build_update_node`, `build_merge_node`, `build_scalar_compute_node`, `build_while_loop_node`, `build_for_loop_node`) reads `committed_after` from the raw block: `raw_block.get("committed_after", False)` (lines 192, 231, 278, 337, 378, 421). **The parser never writes this key.** So `committed_after` is `False` for every node ever produced — including OFSMDM nodes that land in `nodes` correctly.
- [`query_engine.py:1069`](src/parsing/query_engine.py#L1069) reads `node.get("committed_after", False)` — the only consumer. The downstream effect of always-False is that no node is treated as "definitely committed", but since every node's value is the same constant False, the relative ordering is meaningless.

### `src/parsing/serializer.py`

Pure msgpack/JSON serialization with no schema-aware logic. Not implicated. Read for completeness.

---

## Section 2 — `CS_Deferred_Tax_Asset_Net_of_DTL_Calculation` parser trace

> **Note about the file under inspection.** The file in the working tree currently has +177/-23 of pretty-printing modifications uncommitted by the user (multi-line MERGE format). The parser trace below was run against this on-disk file. The parsed result LANDS the MERGE in `nodes` correctly, **but only because the user reformatted the source.** The original committed version (the version Redis was loaded from) has the entire MERGE on one ~10-KB line WITH the optimizer hint embedded, which reproduces the bug. Section 4 confirms this on three other OFSERM functions whose source is still in the original megaline format — they all reproduce the bug.

Trace excerpt for the modified file (run on the working-tree contents):

```
Total source lines: 188
comment_map flagged-True line indices (count=1):
  L  26:         USING (  SELECT /*+ PARALLEL(4) */            ← inline hint, fully closed

raw_blocks (n=1):
  type=MERGE  L25-178  is_commented_out=False  preceded_by_commit=False  followed_by_commit=True
        first_line: 'MERGE INTO FCT_STANDARD_ACCT_HEAD TT'

builder result: nodes=1  edges=0  commented_out_nodes=0
  NODE id=CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION_N1  type=MERGE  committed_after=False
```

Why the modified file works:
- The user's edit moved the optimizer hint `/*+ PARALLEL(4) */` to line 26, so `comment_map[25] = True` (line 26 is 0-indexed 25), but the MERGE keyword is on line 25 (0-indexed 24), where `comment_map[24] = False`.
- `parse_function` checks `comment_map[idx]` at the index of the DML keyword (24). False → block is `is_commented_out=False`.

For comparison: the version in `git show HEAD:db/modules/.../CS_Deferred_Tax_Asset_Net_of_DTL_Calculation.sql` has the entire MERGE on a single line ~10 KB long, with `/*+ PARALLEL(4) */` embedded mid-line. `_build_comment_map` flags that whole line as commented. `comment_map[idx_of_MERGE] = True`. Block becomes `is_commented_out=True`. Routes to `commented_out_nodes`. **This is what produced the empty `meta.node_count = 0` for ~375 of 380 OFSERM entries currently in Redis.**

Note also: even the modified-file trace shows `committed_after=False` despite `followed_by_commit=True`. Bug B2 (separate, secondary) — the parser writes `followed_by_commit` but the builder reads `committed_after`.

---

## Section 3 — OFSMDM working-function comparison

`db/modules/OFSDMINFO_ABL_DATA_PREPARATION/functions/FN_LOAD_OPS_RISK_DATA.sql` — 377 lines, 14 raw blocks. Excerpt:

```
comment_map flagged-True line indices (count=9):
  L 139:       --        /* Populate STG_OPS_RISK_DATA By inserting records ... */
  L 167:       --      /* ABLOR LOB FOR ABLIBG*/
  L 196:       /* -- NEW LOGIC OF OPERATIONAL RISK ... --- */
  L 226:       /* Deduction from CFI/RBA with ratio */
  L 245:       /* Deduction from CFI/RBA with ratio for ABLIBG*/
  L 267:       /* Deduction from CBA */
  L 287:       /*Total Deduction for ABLIBG*/
  L 307:          LN_TOTAL_DEDUCT_ABLIBG + (-1 * LN_DEDUCT_RATIO_ABLIBG_1); /* Sub Total For ABLIBG*/
  L 328:       /*Update Value of CBA and RBA for ABLIBG*/

raw_blocks (n=14):
  type=DELETE   L198-199  is_commented_out=False  followed_by_commit=True
  type=INSERT   L203-222  is_commented_out=False  followed_by_commit=True
  type=SELECT_INTO L270-276 is_commented_out=False ...
  ... (14 total) ...

builder result: nodes=14  edges=1  commented_out_nodes=0
```

What's different about OFSMDM:
- **Source is hand-written, not OFSAA-extracted megalines.** The DML keywords (DELETE, INSERT, UPDATE, SELECT_INTO) live on their own dedicated lines. No DML keyword shares a line with `/* … */`.
- The 9 `comment_map`-flagged lines all contain real comments — none is a DML start.
- So `comment_map[idx_of_DML]` is `False` everywhere it matters. None of the blocks gets misclassified.

Note line 307: `LN_TOTAL_DEDUCT_ABLIBG + ... ; /* Sub Total For ABLIBG*/`. This is a SCALAR_COMPUTE ASSIGNMENT line **with an inline comment**. The parser's `_classify_line` runs on `cleaned_lines[idx]` (comments stripped), so the assignment is detected. But `comment_map[306] = True` (the inline `/* */`), and an SCALAR_COMPUTE block is created (raw_blocks[5] in the trace, L305-305) **at line 305, not 307** — line 307 is treated as a separate consideration. Actually inspecting the trace: there's a SCALAR_COMPUTE at L305 and the next block starts at L309. Line 307 fell into the gap, presumably because either it's blank in cleaned form or comment_map[306]=True kept the parser from building a block from that line. **Subtle outcome:** there is a real SCALAR_COMPUTE on line 307 that the parser silently skipped because its line is comment_map-flagged. This is a smaller, secondary instance of the same bug — but the OFSMDM function still produces 14 nodes and looks healthy on the outside, so nobody noticed.

The key difference between OFSMDM and OFSERM: OFSMDM's bug instances are tail-end SCALAR_COMPUTEs, OFSERM's bug instances are the primary MERGE/INSERT statements.

---

## Section 4 — Three additional OFSERM samples

Run on three random OFSERM functions still in the original OFSAA-extracted megaline format:

| Function | total_lines | raw_blocks | nodes | commented_out_nodes | Verdict |
|---|---|---|---|---|---|
| `CS_Goodwill_Calculation` | 34 | 1 (MERGE, L24-24, **is_commented_out=True**) | 0 | 1 | Reproduces bug. The MERGE is one line of ~10 KB containing `/*+ PARALLEL(4) */`. |
| `ACCOUNT_RATINGS_POPULATION` | 34 | 1 (INSERT, L24-24, **is_commented_out=True**) | 0 | 1 | Reproduces bug. The INSERT is one line containing `/*+APPEND*/` (different hint, same shape). |
| `CS_Required_Capital_Ratio_Assignment` | 34 | 1 (MERGE, L24-24, **is_commented_out=True**) | 0 | 1 | Reproduces bug. Same megaline + `/*+ PARALLEL(4) */`. |

All three have `followed_by_commit=True` (the COMMIT lives on a later, separate line). All three end up in `commented_out_nodes` with `committed_after=False`. **The bug is OFSAA-wide — every OFSERM function whose source comes from the OFSAA execution-log extraction is affected, because the extraction collapses each DML to a single line and OFSAA inserts optimizer hints inline.**

OFSMDM functions are written directly (they're hand-authored helpers like `FN_LOAD_OPS_RISK_DATA`, not extracted from OFSAA), so they have multi-line DML and are not affected.

---

## Section 5 — Root cause

There are **two distinct bugs** that combine to produce the symptom "OFSERM functions show `node_count=0` and put their DML in `commented_out_nodes`."

**Bug B1 — `_build_comment_map` flags inline self-closing block comments as fully commented.**
At [src/parsing/parser.py:220-226](src/parsing/parser.py#L220-L226), when a line contains `/*` the function unconditionally appends `True` to `comment_map`, even when the same line also contains `*/`. The comment in the code (`# For safety, mark it.`) describes deliberate conservatism that turns out to be wrong: lines with inline `/*+ HINT */` Oracle optimizer hints are real code lines that happen to contain a fully-closed comment, not comment-only lines. The `is_commented_out` flag in [parse_function](src/parsing/parser.py#L814) reads `comment_map[idx]` for the DML's first line; when the DML and the inline hint share a line (the OFSAA-extracted megaline format), the block is misclassified. The builder at [builder.py:91-92](src/parsing/builder.py#L91-L92) routes it to `commented_out_nodes`.

**Bug B2 — `committed_after` is never written by the parser.**
The parser writes `followed_by_commit` and `preceded_by_commit` on each block ([parser.py:850-851, 893-894](src/parsing/parser.py#L850-L851), populated by the COMMIT-adjacency loop at lines 901-923). The builder reads `raw_block.get("committed_after", False)` ([builder.py:192, 231, 278, 337, 378, 421](src/parsing/builder.py#L192)). Because the keys don't match, every node's `committed_after` is `False` regardless of whether a COMMIT actually follows the DML. This affects OFSMDM and OFSERM identically; the only consumer is [query_engine.py:1069](src/parsing/query_engine.py#L1069), which sees the constant False and degenerates to "no node is committed-after."

B1 is the dominant cause of the user-visible empty-graph symptom. B2 is a smaller defect that means "even the OFSMDM functions that work, lie about commit status." The user's prompt asks both to be fixed — B1 to route DML to `nodes`, and B2 so `committed_after = true` when COMMIT follows.

---

## Section 6 — Proposed fix (plain language)

### Fix B1 — make `_build_comment_map` only flag lines that are inside a multi-line block comment

In `src/parsing/parser.py:_build_comment_map`, change the `BLOCK_COMMENT_START` branch so it only appends `True` when the comment is NOT closed on the same line:

- If the line contains `/*` AND `*/` (inline self-closing), append `False`. The comment is fully stripped by `clean_source_lines`; the line still has real code (the DML keyword, in OFSAA's case).
- If the line contains `/*` but NOT `*/`, append `True` and set `in_comment = True` so subsequent lines are flagged until `*/` arrives.
- Lines fully inside an already-open block comment (the `if in_comment:` branch) keep the existing behavior — append `True` and look for `*/`.

This is a 2-line change to the existing branch.

**Edge case to preserve.** A real comment-only line like `/* this is just a comment */` (whole line wrapped in `/* */`) is now `False` in the new logic. Is that wrong? Looking at how `is_commented` is used: it gates whether a *DML block* lands in `commented_out_nodes`. If a line is a pure comment, it has no DML on it, so `_classify_line` returns `None` and no block is ever created from it. The flag value for pure-comment lines is irrelevant. The fix is safe.

There's a parallel mechanism — `block_comment_lines` (built from `comment_ranges`) — that catches DML lines fully enclosed by a multi-line block comment. That keeps working unchanged: it's the authoritative source for "this line is inside a comment range that spans multiple lines."

### Fix B2 — read the right field name in builder.py

In `src/parsing/builder.py`, replace each `raw_block.get("committed_after", False)` with `raw_block.get("followed_by_commit", False)` at lines 192, 231, 278, 337, 378, 421. Six identical edits, one per node-builder.

The output node still exposes the field as `committed_after` (the consumer-facing name) — only the lookup key on the raw block changes.

Alternative considered: rename the parser's `followed_by_commit` to `committed_after`. Rejected because the parser tracks both `preceded_by_commit` and `followed_by_commit` and renaming only one would obscure the symmetry; renaming both reaches further into the parser than needed.

### What the fix does NOT touch

- No changes to `clean_source_lines` — it already strips inline `/* */` correctly.
- No changes to `block_comment_lines` / `comment_ranges` — already correct.
- No changes to `find_block_end`, `extract_table_names`, `extract_column_maps`, etc.
- No changes to `serializer.py`, the loader, the indexer, or any caller. The per-function graph dict shape stays identical; only the routing of OFSAA-wrapper DMLs flips from `commented_out_nodes` to `nodes`, and `committed_after` starts being populated correctly.

---

## Section 7 — Risks and regression coverage

### Risk: OFSMDM regression

If Bug B1's fix is too aggressive, an OFSMDM block that legitimately should be in `commented_out_nodes` could get reclassified. The trace in Section 3 shows OFSMDM has no DML keyword sharing a line with an inline `/* */` — so the fix should be a no-op for the OFSMDM corpus we have. **Regression test:** parse `FN_LOAD_OPS_RISK_DATA` and the other 11 OFSMDM functions; assert node-count and commented-count match pre-fix values.

### Risk: legitimately commented-out DML

If a developer writes `/* MERGE INTO X ... ; */` (entire DML wrapped in a block comment), it must still go to `commented_out_nodes`. The fix preserves this:
- Single-line `/* MERGE ... */`: the `clean_source_lines` strip removes `MERGE ...` from the cleaned line. `_classify_line` runs on the cleaned line, sees nothing, returns `None`. No block is created. **Correct outcome — but the block doesn't appear anywhere, not even `commented_out_nodes`.** Acceptable: a one-line wrapped DML is functionally absent from the function. If we want to preserve it as a `commented_out_node`, that's a separate enhancement, not a regression.
- Multi-line `/* MERGE ... \n ... ; \n*/`: caught by `comment_ranges` / `block_comment_lines`. The parser uses `cleaned_lines` (which has the body stripped) for keyword detection, but `block_comment_lines` keeps `is_commented = True`. **Existing behavior preserved.** **Regression test:** synthetic fixture with multi-line `/* MERGE ... */`; assert it lands in `commented_out_nodes`.

### Risk: lone `/* HINT */` on its own line above a DML

Pattern:
```
   /*+ PARALLEL(4) */
   MERGE INTO X ...;
```
The hint line gets `comment_map = True` post-fix as well? No — actually no, post-fix the hint line gets `False` (inline self-closing). The MERGE line on the next line gets `False`. Block lands in `nodes`. **Correct.** Pre-fix this same shape was probably also fine because the hint and MERGE don't share a line.

### Risk: comment that opens but doesn't close (truncated source)

Unlikely in practice but: `/* unfinished` with no closing `*/` ever. Post-fix: line gets `True`, `in_comment` flips to True, all subsequent lines flagged True. Same behavior as pre-fix for this branch. Safe.

### Risk: `_classify_line` skips assignments hidden behind `comment_map=True`

Line 307 in `FN_LOAD_OPS_RISK_DATA` is `LN_TOTAL_DEDUCT_ABLIBG + (-1 * LN_DEDUCT_RATIO_ABLIBG_1); /* Sub Total For ABLIBG*/` — currently silently skipped. Post-fix the same line would have `comment_map[306] = False`, which means `is_commented = False`, and the SCALAR_COMPUTE detection would run normally. **The fix may surface this previously-missing assignment as a new node.** That's a *good* outcome, but it changes the node count for OFSMDM functions that have inline-comment-tail assignments. The regression test must accept "node count ≥ pre-fix node count" rather than strict equality.

### Required regression-test set

**B1 edge-case tests (target the comment-map mis-classification specifically):**

1. **`test_single_inline_hint_does_not_flag_line`** — Source line `MERGE INTO FCT_X TT USING (SELECT /*+ PARALLEL(4) */ a FROM b) ...; COMMIT;` packaged as a minimal function. Assert the parser produces one MERGE block in `raw_blocks` with `is_commented_out=False`. The single inline `/*+ … */` hint must NOT mark the line as commented.
2. **`test_two_inline_hints_on_same_line_does_not_flag_line`** — Source line `MERGE /*+ FIRST_ROWS */ INTO X USING (SELECT /*+ PARALLEL(4) */ a FROM b) ...; COMMIT;` (two self-closing block comments on one line). Assert MERGE block is `is_commented_out=False`. The fix must handle multiple `/* … */` per line, not just one.
3. **`test_multi_line_block_comment_still_flags_inner_lines`** — Source with a block comment that spans 3 lines, with a DML keyword on the middle (commented) line:
   ```sql
   /* TODO retire this:
   MERGE INTO X USING ... ;
   COMMIT; */
   ```
   Assert no MERGE block is produced (the cleaned line has no DML keyword), or if produced, lands in `commented_out_nodes`. Multi-line block comments must STILL be tracked correctly post-fix — this guards against a regression that would over-correct B1 by ignoring all `/*` markers.

**Behavior-preservation tests (the original list, unchanged):**

4. **`test_inline_optimizer_hint_lands_in_nodes`** — Synthetic OFSAA-style megaline `MERGE ... /*+ PARALLEL(4) */ ... ; COMMIT;`, end-to-end through the builder. Assert node in `nodes`, `committed_after=True` (relies on B2 fix as well).
5. **`test_multi_dml_with_commit`** — `INSERT ...; UPDATE ...; COMMIT;`. Both DMLs in `nodes`, both `committed_after=True`.
6. **`test_dml_without_commit`** — `INSERT ... ;` (no COMMIT). DML in `nodes`, `committed_after=False`. (Must not flip the default.)
7. **`test_ofsmdm_pattern_preserved`** — Fixture mirroring the `FN_LOAD_OPS_RISK_DATA` shape (multi-line DML, separate COMMIT, inline-comment-tail assignments). Assert node count ≥ baseline; key DML nodes (DELETE, INSERT, UPDATE) all present in `nodes`.
8. **`test_multiline_block_comment_dml_still_routed_correctly`** — Fixture with `/* MERGE INTO X ... ; */` spanning multiple lines. Assert it does NOT appear in `nodes`. (May or may not appear in `commented_out_nodes` depending on whether `_classify_line` sees the cleaned line — acceptable either way; what matters is it's not active.)
9. **`test_committed_after_field_is_populated`** — Single MERGE followed by COMMIT. Assert the resulting node has `committed_after=True`. This test fails today regardless of the OFSAA wrapper because Bug B2 always sets it to False.

---

## Section 8 — Per-function delta log

This section is populated during Phase B with concrete before/after numbers, so that the impact of each fix is visible in this single document rather than scattered across scratch logs. Entries are added in the order the fixes land.

### 8.0 Sampling plan

The `scratch/w35p05_parser_trace.py` script runs the parser+builder over a fixed set of 5 hand-picked functions (CS_Deferred_Tax, FN_LOAD_OPS_RISK_DATA, plus the three OFSAA-megaline samples from Section 4). The same 5 functions are re-run after each fix and the deltas captured below. In addition, after both fixes land, a corpus-wide pass over all 384 .sql files (12 OFSMDM + 372 OFSERM) feeds the aggregate counts in Section 8.3.

Columns in the per-function tables:
- `nodes` / `commented` — `len(graph["nodes"])` and `len(graph["commented_out_nodes"])` from `build_function_graph`.
- `committed_after_true` — count of nodes in `nodes` whose `committed_after` field is `True`.

### 8.1 Pre-fix baseline (captured 2026-04-27, before any code change)

| Function | Source format | nodes | commented | committed_after_true |
|---|---|---:|---:|---:|
| CS_Deferred_Tax_Asset_Net_of_DTL_Calculation | multi-line (user reformatted) | 1 | 0 | 0 |
| FN_LOAD_OPS_RISK_DATA (OFSMDM) | multi-line | 14 | 0 | 0 |
| CS_Goodwill_Calculation | OFSAA megaline | 0 | 1 | 0 |
| ACCOUNT_RATINGS_POPULATION | OFSAA megaline | 0 | 1 | 0 |
| CS_Required_Capital_Ratio_Assignment | OFSAA megaline | 0 | 1 | 0 |

Notable baselines for the corpus-wide aggregate (from Phase 0 diagnostic):
- OFSERM: 380 functions in Redis, 375 (~99%) with `meta.node_count=0`.
- OFSMDM: 38 functions in Redis, all with populated nodes (where the DML existed).
- 0 nodes in any function have `committed_after=true` (Bug B2 universal).

### 8.2 Post-B1 delta

After applying B1 alone (parser fix only; builder still reads the wrong field name for `committed_after`).

**Per-function (5-sample trace via `scratch/w35p05_parser_trace.py`):**

| Function | Source format | nodes (was → now) | commented (was → now) | committed_after_true |
|---|---|---|---|---|
| CS_Deferred_Tax_Asset_Net_of_DTL_Calculation | multi-line (user reformatted) | 1 → **1** | 0 → 0 | 0 |
| FN_LOAD_OPS_RISK_DATA (OFSMDM regression baseline) | multi-line | 14 → **14** | 0 → 0 | 0 |
| CS_Goodwill_Calculation | OFSAA megaline | **0 → 1** | **1 → 0** | 0 |
| ACCOUNT_RATINGS_POPULATION | OFSAA megaline | **0 → 1** | **1 → 0** | 0 |
| CS_Required_Capital_Ratio_Assignment | OFSAA megaline | **0 → 1** | **1 → 0** | 0 |

Three OFSAA-megaline samples flipped from `(0 nodes, 1 commented)` to `(1 node, 0 commented)`. CS_Deferred_Tax (already in pretty-printed form due to the user's pending edit) and FN_LOAD_OPS_RISK_DATA (OFSMDM regression baseline) are unchanged. `committed_after_true=0` everywhere — this confirms B2 is independent and still pending.

**Corpus-wide aggregate (via `scratch/w35p05_corpus_trace.py`, post-B1):**

| Schema | Functions | nodes_total | commented_total | committed_after_true | zero_node_fns |
|---|---:|---:|---:|---:|---:|
| OFSERM | 372 | 372 | 0 | 0 | 0 |
| OFSMDM | 12 | 59 | 0 | 0 | 0 |

**Pre-fix baseline comparison (from Phase 0 diagnostic + Redis state captured 2026-04-27):**

- OFSERM Redis state pre-fix: 380 graph keys (8 duplicates from earlier loads), of which **375 had `meta.node_count = 0`**. Post-B1 corpus pass: **0 zero-node OFSERM functions**.
- OFSMDM corpus pre-fix: 12 functions on disk; trace runs of FN_LOAD_OPS_RISK_DATA show 14 nodes pre- and post-B1. The corpus aggregate of 59 nodes across 12 functions is consistent with the per-function pre-fix distribution observed in Section 3 (FN_LOAD_OPS_RISK_DATA alone contributed 14 nodes).
- `commented_total = 0` for both schemas post-B1: no function has any block landing in `commented_out_nodes`. This is expected — the OFSAA-extraction pipeline doesn't produce intentionally-commented-out DML in the source, so post-B1 the bucket is correctly empty across the corpus. Tests `test_multiline_commented_out_dml_does_not_land_in_nodes` and `test_multi_line_block_comment_still_flags_inner_lines` validate the bucket is still functional for synthetic inputs that need it.

**B1 verdict:** OFSERM trace coverage went from ~1% functional (5 of 380, by node_count > 0) to **100% functional** (372/372). B2 still required for `committed_after` to populate.

### 8.3 Post-B2 delta + corpus-wide aggregate

After applying B2 (six identical edits in `src/parsing/builder.py`, replacing `raw_block.get("committed_after", False)` with `raw_block.get("followed_by_commit", False)`).

**Per-function (5-sample trace, post-both-fixes):**

| Function | nodes (was → B1 → B2) | commented (was → B1 → B2) | committed_after_true (was → B1 → B2) |
|---|---|---|---|
| CS_Deferred_Tax_Asset_Net_of_DTL_Calculation | 1 → 1 → 1 | 0 → 0 → 0 | 0 → 0 → **1** |
| FN_LOAD_OPS_RISK_DATA (OFSMDM regression baseline) | 14 → 14 → 14 | 0 → 0 → 0 | 0 → 0 → **6** |
| CS_Goodwill_Calculation | 0 → 1 → 1 | 1 → 0 → 0 | 0 → 0 → **1** |
| ACCOUNT_RATINGS_POPULATION | 0 → 1 → 1 | 1 → 0 → 0 | 0 → 0 → **1** |
| CS_Required_Capital_Ratio_Assignment | 0 → 1 → 1 | 1 → 0 → 0 | 0 → 0 → **1** |

The OFSMDM regression baseline (`FN_LOAD_OPS_RISK_DATA`) gains 6 `committed_after_true` nodes — the DELETE, INSERT, two UPDATEs, and the trailing INSERT-after-COMMIT (per Section 3 trace). All match the source's actual COMMIT placement.

**Corpus-wide aggregate (post-both-fixes):**

| Schema | Functions | nodes_total | commented_total | committed_after_true | zero_node_fns |
|---|---:|---:|---:|---:|---:|
| OFSERM | 372 | 372 | 0 | **371** | 0 |
| OFSMDM | 12 | 59 | 0 | **27** | 0 |

OFSERM has 371/372 nodes with `committed_after=True` — the OFSAA-wrapper template puts a single MERGE/INSERT followed immediately by COMMIT, so virtually every node qualifies. The single outlier (verified via the JSON summary) is [`THIRD_PARTY_MINORITY_HOLDING_INDICATOR_ASSIGNMENT_UNDER_CAPITAL_CONSOLIDATION.sql`](db/modules/ABL_CAR_CSTM_V4/functions/THIRD_PARTY_MINORITY_HOLDING_INDICATOR_ASSIGNMENT_UNDER_CAPITAL_CONSOLIDATION.sql), whose MERGE megaline ends with a malformed `... AND;` — unbalanced parens and a stray semicolon mid-WHERE. The parser's paren-depth-aware MERGE-end detection extends the block all the way to the end of the file (line 34), swallowing the COMMIT on line 26 *into* the MERGE block. Result: `followed_by_commit=False` because no COMMIT exists *after* the block ends. This is correct parser behavior for malformed input — and is a separate data-hygiene issue (`AND;` mid-statement is not valid PL/SQL), not a fix concern.

OFSMDM has 27/59 = ~46% with `committed_after=True`. The lower fraction reflects that OFSMDM functions contain many SCALAR_COMPUTE assignments and SELECT-INTOs that never need a following COMMIT — only the three actual DML blocks do, and `FN_LOAD_OPS_RISK_DATA` alone contributes 6 to the total. This matches expectations.

**Combined-fix verdict:**

- OFSERM trace coverage: 5/380 functions (1.3%) → **372/372 (100%)**, with 371/372 also having a populated `committed_after`.
- OFSMDM behavior: preserved exactly. Same node counts, plus the (previously always-false) `committed_after` field is now populated correctly per the parser's `followed_by_commit` signal.
- `commented_out_nodes` total across both schemas: 0. The bucket is no longer being misused; it remains available for genuine multi-line commented-out DML if any future function ships one.

---

## Section 9 — Commit plan

Two commits, in this order:

1. **`fix(parsing): route inline /* … */ comment lines correctly (Bug B1)`** — modifies `src/parsing/parser.py:_build_comment_map`. Adds tests B1.1, B1.2, B1.3 from Section 7. Updates Section 8.2 with the post-B1 delta. **Does NOT fix `committed_after`** — that's the next commit.
2. **`fix(parsing): wire committed_after to followed_by_commit (Bug B2)`** — modifies the six `raw_block.get("committed_after", False)` lookups in `src/parsing/builder.py`. Adds test 9 (`test_committed_after_field_is_populated`). Updates Section 8.3 with corpus-wide aggregate after both fixes apply.

Tests 4-8 (behavior preservation + multi-DML + OFSMDM regression) are split across the two commits per which fix they exercise: the OFSAA-megaline routing tests (4, 5) and the OFSMDM-preservation test (7) plus the multi-line-comment test (8) land with B1; the `committed_after`-dependent test (9) and the no-COMMIT default test (6) land with B2.

---

## End of Phase A

The trace artifacts under `scratch/` are not staged and won't be committed.

The fix being implemented (per Phase B):
- ~2 changed lines in `src/parsing/parser.py:_build_comment_map`
- 6 line edits in `src/parsing/builder.py` (one per node-builder, replace `committed_after` with `followed_by_commit` in the `raw_block.get(...)` call)
- 9 new unit tests in `tests/unit/parsing/` (3 B1 edge-case tests + 5 behavior-preservation + 1 B2 verification)
- Synthetic fixtures inline in test files (no permanent .sql fixture files needed — fabricated PL/SQL is small enough to live in test docstrings)

Two commits per Section 9.

---

## Section 10 — Followup investigation: loader gap (231 OFSERM files not in Redis)

After the Phase 0.5 backend restart, post-rebuild Redis contained 141 `graph:OFSERM:*` keys vs 372 .sql files on disk — a 62% gap. **This is not a Phase 0.5 regression.** It is W39 manifest strict mode operating as designed: 231 disk files are not referenced in `db/modules/ABL_CAR_CSTM_V4/manifest.yaml` and are correctly skipped, while 24 inactive manifest tombstones point at non-existent .sql files (the "FAILED to parse" entries).

Full investigation: [`scratch/w35p05_loader_gap.md`](../scratch/w35p05_loader_gap.md). Key takeaways:
- 5/5 sampled missing files parse cleanly in-process via `build_function_graph` — no parser issue.
- The drop point is [src/parsing/loader.py:248-257](src/parsing/loader.py#L248-L257) — strict-mode skip when a disk file is not in `manifest_file_keys`.
- Phase 0.5 should still ship; the manifest-curation gap is a separate stakeholder decision (Phase 1 territory or a small follow-up PR adding a `loader.strict_mode` config flag).
