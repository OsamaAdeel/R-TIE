# W45 — Option B Validation Experiment

**Scope:** validate whether the "not the answer" response for ungrounded
identifiers (e.g. `CAP973`) can be produced by prompt engineering alone,
without a new response shape, extra LLM calls, or frontend routing.

**Status: PASS.** All three prompt variants (V1, V2, V3) produced output that
meets all six evaluation criteria on the first attempt against `gpt-4o-mini`.

---

## 1. Current prompt location and structure

### 1.1 The prompt actually used for a `CAP973`-style query

`CAP973` is classified by the orchestrator as `VARIABLE_TRACE` and routed
through `variable_tracer.stream_chain` (main.py:913). The system prompt
used is [`VARIABLE_TRACE_PROMPT`](../src/agents/variable_tracer.py#L73-L120)
in [src/agents/variable_tracer.py:73-120](../src/agents/variable_tracer.py#L73).

Verbatim header instruction from that prompt (line 114):

> FORMAT:
> - Start with: `## {VARIABLE_NAME} in \`FUNCTION_NAME\` (SCHEMA)`

That single instruction is the structural root cause of the problem: it
forces the LLM to emit a title claiming one function computes the
identifier. With `CAP973` + `TLX_PROV_AMT_FOR_CAP013` this mechanically
produces `## CAP973 in TLX_PROV_AMT_FOR_CAP013`, which is false.

The adjacent semantic path uses
[`SEMANTIC_EXPLANATION_PROMPT`](../src/agents/logic_explainer.py#L312-L350)
([src/agents/logic_explainer.py:312-350](../src/agents/logic_explainer.py#L312)).
It shares the same problematic "Start with: `## VAR in FUNC`" pattern.

### 1.2 Is there a conditional code path for ungrounded identifiers?

**No.** The UNGROUNDED detection is entirely **post-hoc**:

- [`evaluate_grounding()`](../src/agents/logic_explainer.py#L52-L160)
  (logic_explainer.py:52-160) runs **after** the LLM has already streamed
  its full response.
- [main.py:952-958](../src/main.py#L952) calls it and appends
  `sanity_messages` as a `**Caveats:**` block at the bottom
  ([main.py:962-967](../src/main.py#L962)).
- There is no code path that inspects `multi_source` vs. the query's
  identifiers **before** the LLM call and swaps the prompt.

Additionally, the hierarchy header ("This function runs in …") is emitted
**unconditionally** once per request at [main.py:868-873](../src/main.py#L868)
— it's always the hierarchy of the top-ranked retrieved function,
regardless of whether that function is the answer. For `CAP973` this
currently yields a header describing where `TLX_PROV_AMT_FOR_CAP013` runs,
which is misleading.

**Implication for the fix:** a prompt-only change is insufficient — the
fix also needs:

1. A pre-generation check (hoist the existing logic from
   `evaluate_grounding` to run before the LLM call).
2. A conditional branch in `main.py`'s streaming endpoint to route to the
   ungrounded prompt.
3. Suppression of the hierarchy header when UNGROUNDED is true.

These are small code changes — on the order of ~40–60 lines total — but
they **are** required. They are not architectural changes (no new
response shape, no new endpoint, no new LLM call, no frontend work).

---

## 2. Experimental prompt

Three variants were tested. Full text is in
[scratch/w45_experiment_prompt.txt](../scratch/w45_experiment_prompt.txt)
and in the driver at `scratch_w45_test.py` (project root, not in tree).
V1 is the canonical version; V2 and V3 are ablations.

### V1 — full constraints + output template

Structure:

1. **Opening frame** — "You are answering a question about a business
   identifier that was NOT found in any function that has been indexed."
2. **HARD CONSTRAINTS** — five explicit "DO NOT" rules naming the exact
   failure modes observed in production:
   - no `## X in FUNCTION` header
   - no "This function runs in…"
   - no describing a retrieved function as if it computed the asked
     identifier
   - no `Step 1 / Step 2` walkthrough
   - no body-contradicting caveat at the end
3. **WHAT TO DO** — four positive instructions (A, B, C, D) covering
   the "not found" statement, the "why not found" hypothesis, the
   candidate list with honest per-candidate descriptions, and a
   suggested next step.
4. **OUTPUT TEMPLATE** — pre-filled markdown scaffold with literal
   `## {IDENTIFIER} — Not Found in Indexed Functions` header and
   placeholders for candidate rows. Scaffolding is the single strongest
   lever for forcing output shape.

### V2 — terse template-only

Same template, but with the enumerated constraints collapsed into two
sentences. Included to test whether the template alone is enough or
whether the "DO NOT" list is load-bearing.

### V3 — V1 + negative/positive worked examples

V1 plus a concrete example of the forbidden output shape and a concrete
example of the correct output shape. Tests whether few-shot scaffolding
on top of the template adds anything.

### Rationale for specific wording

- **"HARD CONSTRAINTS — DO NOT VIOLATE"** — the production failure mode
  is the LLM confidently rewriting a retrieved function's logic under
  the asked identifier. An enumerated "do not" list naming the exact
  pattern is the cheapest suppressor for a known failure.
- **Pre-filled output template** — most reliable way to force shape.
  The model is strongly biased to reproduce a literal scaffold.
- **"retrieved by name-similarity only"** — gives the model a concise,
  truthful label to apply to each candidate, discouraging elaboration.
- **Explicit "no code blocks, no line citations"** — citing lines of a
  function that doesn't compute the identifier would be a dishonest
  concession of authority. Forbidden.

---

## 3. Test outputs

Model: `gpt-4o-mini`, `temperature=0`, `max_tokens=1500`.
Context: query "How is CAP973 calculated?", candidates
`TLX_PROV_AMT_FOR_CAP013`, `FN_UPDATE_RATING_CODE`, `POPULATE_PP_FROMGL_AMC`
with ~20-line source excerpts each.

### Run 1 — V1 (full constraints + template)

```
## CAP973 — Not Found in Indexed Functions

CAP973 was not found as a computed value in any function I have indexed.
It may reside in a schema that is only partially indexed, or it could be
aggregated into a table without an indexed computation function.

### Related functions I searched (none compute CAP973):

- **TLX_PROV_AMT_FOR_CAP013** — Computes the provision amount for CAP013
  capital head. Does NOT compute CAP973; retrieved by name-similarity only.
- **FN_UPDATE_RATING_CODE** — Merges rating code data into the staging
  product processor. Does NOT compute CAP973; retrieved by name-similarity
  only.
- **POPULATE_PP_FROMGL_AMC** — Inserts data into the staging product
  processor from general ledger data. Does NOT compute CAP973; retrieved by
  name-similarity only.

### Suggested next step

You may want to check other schemas or documentation for additional context
on CAP973.
```

**Score V1 — all 6 criteria:**

| # | Criterion | Result |
|---|-----------|--------|
| 1 | No "This function runs in…" or answer-framing | ✅ |
| 2 | Explicit "not found" | ✅ ("was not found as a computed value") |
| 3 | Candidates labeled as "related"/"searched", not "the answer" | ✅ ("Related functions I searched") |
| 4 | Honest description of each candidate | ✅ (CAP013, not CAP973; rating-code merge; GL insert) |
| 5 | Hierarchy header absent or clearly scoped | ✅ (absent) |
| 6 | Structurally readable markdown | ✅ (`##`, `###`, bullets) |

**Verdict V1: PASS (6/6)**

### Run 2 — V2 (terse template-only)

```
## CAP973 — Not Found in Indexed Functions

CAP973 was not found in any loaded function's source. This may be due to
partial schema coverage or the identifier being outside the indexed corpus.

### Related functions I searched (none compute CAP973):

- **TLX_PROV_AMT_FOR_CAP013** — Computes the provision amount for CAP013
  capital head by aggregating shortfall data from the staging product
  processor and updating the bank capital details. Does NOT compute CAP973;
  retrieved by name-similarity only.
- **FN_UPDATE_RATING_CODE** — Merges rating code data into the staging
  product processor from a mapping table, updating various fields based on
  matching instrument codes. Does NOT compute CAP973; retrieved by
  name-similarity only.
- **POPULATE_PP_FROMGL_AMC** — Inserts data into the staging product
  processor from general ledger data, populating various financial fields
  based on the specified mis date. Does NOT compute CAP973; retrieved by
  name-similarity only.

### Suggested next step

Consider checking other sources or documentation for CAP973 to find its
calculation method.
```

**Verdict V2: PASS (6/6).** Candidate descriptions are slightly more
verbose but more technically informative (names actual tables and fields).
V2 shows the template alone is sufficient — the enumerated "DO NOT" list
isn't strictly necessary for `gpt-4o-mini` at `temperature=0`.

### Run 3 — V3 (V1 + negative/positive examples)

```
## CAP973 — Not Found in Indexed Functions

CAP973 was not found as a computed value in any function I have indexed.
It may reside in a schema that is not fully indexed or could be aggregated
in a table without an indexed computation function.

### Related functions I searched (none compute CAP973):

- **TLX_PROV_AMT_FOR_CAP013** — Computes the provision amount for capital
  head CAP013 by summing N_PROVISION_SHORTFALL from STG_PRODUCT_PROCESSOR.
  Does NOT compute CAP973; retrieved by name-similarity only.
- **FN_UPDATE_RATING_CODE** — Merges rating code data into the staging
  product processor from MR_RATING_CODE_MAP. Does NOT compute CAP973;
  retrieved by name-similarity only.
- **POPULATE_PP_FROMGL_AMC** — Inserts data into STG_PRODUCT_PROCESSOR
  from GL data based on the specified mis date. Does NOT compute CAP973;
  retrieved by name-similarity only.

### Suggested next step

You may want to check the documentation or data dictionary for additional
information on CAP973 or explore other schemas that might contain relevant
functions.
```

**Verdict V3: PASS (6/6).** Descriptions are the most technically precise
of the three (specific column names and source tables mirrored from the
worked example). Recommended as the baseline for the production prompt.

---

## 4. Verdict

**Can Option B be achieved with prompt engineering alone?**

**Yes, with a small code addition.** The prompt alone reliably produces
the target "not the answer" structure, but integrating it requires:

- A **pre-generation** ungrounded check (hoist/reuse the logic in
  `logic_explainer.evaluate_grounding` that already identifies
  ungrounded identifiers).
- A **branch in main.py** that, when ungrounded is true, routes to the
  new prompt with the identifier + candidate descriptions as context.
- **Skip** `_logic_explainer.hierarchy_header(state)` emission for the
  ungrounded branch, since the top-ranked retrieved function is not the
  answer and its hierarchy is misleading.
- **Suppress** the post-hoc `Caveats:` block for UNGROUNDED when the
  body already states "not found" — otherwise the output has a
  redundant bottom-caveat that contradicts the already-clean body.

No new response shape, no new LLM call, no frontend changes. The metadata
channel already carries `warnings: ["UNGROUNDED_IDENTIFIERS: …"]`; the
frontend doesn't need to switch on it.

**Rough effort estimate:** ~40–60 lines across
[src/agents/variable_tracer.py](../src/agents/variable_tracer.py),
[src/agents/logic_explainer.py](../src/agents/logic_explainer.py), and
[src/main.py](../src/main.py), plus the new prompt constant. One day of
implementation + one day of test coverage (unit test for the detection
pre-check, integration test that asserts the new structure for
`CAP973`-style queries, regression test that the normal VARIABLE_TRACE
path is unchanged).

**Additional prompt iteration needed to make production-ready?**
Minimal. Suggest taking **V3 as the baseline**, with these tweaks:

- Tighten the "Suggested next step" sentence — V3's is slightly verbose;
  consider a fixed boilerplate line rather than letting the LLM
  generate it.
- Add one sentence to the "why not found" hypothesis inventory:
  "it may be registered only in `FCT_STANDARD_ACCT_HEAD` as an aggregated
  row with no computation function" — this is the actual OFSERM case
  for many CAP codes and is useful domain context.

**Recommended path for the actual W45 fix PR:**

1. Extract the ungrounded-identifier detection from
   `evaluate_grounding` into a small reusable helper that can run on
   `(raw_query, multi_source)` before the LLM call.
2. Add a new prompt constant
   (e.g. `UNGROUNDED_IDENTIFIER_PROMPT`) in `variable_tracer.py` based
   on V3.
3. In `main.py` streaming block at [main.py:887](../src/main.py#L887),
   branch on the pre-check: if ungrounded, call a new
   `variable_tracer.stream_ungrounded(identifier, candidates, query)`
   method that bypasses `resolve_variable_names` / `build_alias_map`
   (both of which produce empty chains anyway for an unknown
   identifier) and streams directly with the new prompt.
4. Move the `hierarchy_header` emission inside the branch so the
   ungrounded branch doesn't emit one.
5. Suppress the `Caveats:` block for the ungrounded branch (the body
   already contains the "not found" statement).
6. Add tests: one unit test for the hoisted pre-check; one integration
   test that the `CAP973` query produces the new shape; one regression
   test that `How is EAD_AMOUNT calculated?` (a genuinely grounded
   variable-trace query) still produces the step-by-step output.

---

## 5. Side observations

### 5.1 LLM behavior

- **Template adherence is very strong** at `temperature=0`. All three
  variants produced output that follows the pre-filled scaffold almost
  verbatim. The `gpt-4o-mini` model, given a literal
  `## X — Not Found in Indexed Functions` template, reproduces it
  reliably.
- **Template-only (V2) is nearly as strong as full-constraints (V1).**
  The enumerated "DO NOT" list adds a small safety margin but is not
  strictly necessary for this specific failure mode at `temperature=0`.
  Worth keeping for robustness against prompt drift and for
  higher-temperature runs.
- **Few-shot (V3) adds specificity, not structural correctness.** The
  concrete positive/negative example pair nudges the model to cite
  specific table/column names in the candidate descriptions (which is
  desirable) without changing the overall shape.
- **No model emitted a hierarchy header**, confirming it's safe to
  simply skip the header emission on the ungrounded branch — the LLM
  will not re-introduce one on its own.

### 5.2 Prompt patterns that worked

- Opening sentence that explicitly frames the response as a negative
  answer ("…was NOT found in any function that has been indexed")
  prevents the model from slipping back into explanatory mode.
- Pre-filled markdown scaffold with literal `{IDENTIFIER}` substitutions
  is the single most load-bearing element — removing it (or replacing
  it with a description of desired structure) is expected to degrade
  reliability substantially.
- Labeling candidates "retrieved by name-similarity only" gives the
  model a concise, truthful phrase to attach to each, which suppresses
  the urge to explain why each candidate is relevant.

### 5.3 Code paths that surprised during STEP 1

- The **hierarchy header is emitted unconditionally** at
  [main.py:868-873](../src/main.py#L868) for every streaming response,
  including VARIABLE_TRACE. On the ungrounded path this produces
  misleading output (it describes where the top-ranked
  *retrieved-but-wrong* function runs), which contributes to the
  "confident but wrong" feel of the current response.
- **All ungrounded detection is post-hoc.** There is currently no
  upstream signal that could short-circuit the expensive
  `resolve_variable_names` → `build_alias_map` →
  `extract_relevant_lines` → `build_transformation_chain` pipeline in
  `VariableTracer.trace_variable`. Since all of those stages are
  useless when the identifier isn't in the source anyway, the
  ungrounded branch can skip the pipeline entirely — a nice-to-have
  perf win on the fix PR.
- **`evaluate_grounding` already does the detection** — it just runs
  too late. Hoisting the logic is the cleanest path; no new detection
  code is needed.

---

## Appendix: test artifacts (not committed)

- Experimental prompts and rationale:
  [scratch/w45_experiment_prompt.txt](../scratch/w45_experiment_prompt.txt)
- Test driver (OpenAI API): `scratch_w45_test.py` at project root
- Full captured outputs: `scratch_w45_outputs.txt` at project root

No codebase changes. No branches. No commits.
