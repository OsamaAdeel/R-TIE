# RTIE Weakness Log

Catalog of known weaknesses in RTIE's grounding/routing layers, with
discovery context. Entries are append-only; closed weaknesses keep
their entry with a status update rather than being deleted.

Numbering matches the W-ticket convention used in branches, code
comments, and PR titles (`refactor/w35-…`, `fix/w83b-…`, etc.).

---

## Containerization v1 (infra/containerization-v1) — partial validation, v2 follow-ups pending

**Status:** Dockerfiles, compose stack, entrypoint, and README onboarding shipped on `infra/containerization-v1` (2026-05-14). Strict `poetry install --only main --no-root --no-ansi` and `npm ci --no-audit --no-fund` both succeed against the re-synced lockfiles. Outside the strict grounding/routing weaknesses this log normally tracks, but recorded here per agreement when v1 landed.

**What was validated (build-only dry run):**
- `docker compose build` produces `rtie-rtie-backend:latest` (463 MB) and `rtie-rtie-frontend:latest` (75 MB).
- Backend Dockerfile installs the locked dep set deterministically against the regenerated `poetry.lock`.
- Frontend Dockerfile installs deterministically against the Linux-regenerated `package-lock.json` (npm bumped to 11.x inside the builder stage to match the host npm that generated the lock).

**What was NOT validated in v1 — must be exercised before declaring deployable:**
- Cold-start indexer flow (`docker compose down -v && docker compose up -d` against a wiped Redis volume; expect 5-30 min for `load_all_functions` + `IndexerAgent.index_all_loaded` to complete).
- Canary query end-to-end through the containerized stack (the OFSMDM `SUM(N_EOP_BAL)` canary for `V_LV_CODE='ABL'` on 2025-12-31 — expected `-24,179,237,139.63`).
- Warm-restart timing (`docker compose down` without `-v`, then `up -d` — expect seconds, not minutes, to first /health 200).
- Frontend → backend round-trip via the nginx proxy in [deploy/frontend/nginx.conf](deploy/frontend/nginx.conf), including the SSE /v1/stream path with `proxy_buffering off`.

These four items together constitute the full-stack validation needed before this is treated as production-ready. None of them were exercised in v1 because the developer chose the build-only dry-run path to avoid wiping the currently-populated Redis volume.

**v2 follow-ups:**
- **Docker Hub rate-limit mitigation for CI image builds.** Anonymous pulls from `registry-1.docker.io` are subject to per-IP rate limits (100 / 6h for anonymous, 200 / 6h for authenticated free-tier). A laptop-side build on a shared egress IP already showed flake symptoms during v1 (one stalled `node:20-alpine` layer pull required a buildkit restart, one transient PyPI read timeout on `anthropic`). CI builds will hit this harder. Options: (a) configure a Techlogix registry mirror via Docker Desktop's `registry-mirrors` setting and a daemon-level proxy, (b) move to authenticated Docker Hub pulls with a service-account PAT in CI secrets, (c) push base-image SHAs to the Techlogix private registry and rewrite `FROM` directives to pull from there.
- **Pin base image SHAs.** `python:3.11-slim`, `node:20-alpine`, `nginx:1.27-alpine`, `postgres:15-alpine`, and `redis/redis-stack:latest` are all tag-pinned today; pin to immutable SHAs (`python:3.11-slim@sha256:...`) for true build reproducibility once the image surface is stable.
- **Re-sync host lockfiles when host npm changes.** The v1 `package-lock.json` was Linux-regenerated inside a `node:20-alpine` container because the host-generated lockfile was missing Linux-specific optional deps (e.g. `@emnapi/runtime@1.10.0`). When npm or node versions on the host change, re-run that regenerate-in-Linux step and commit the result before the next image build.

---

## W85. Anchor-vs-asked-function mismatch — FIXED 2026-05-12 (merge SHA pending)

**Status:** Implemented and merged. Live in `_w57_check_anchor_vs_asked_mismatch`
(W57 Check 8) in [src/agents/logic_explainer.py](src/agents/logic_explainer.py).
Fires `GROUNDING-ANCHOR-MISMATCH-HIGH` when the W70 cascade anchor
(with W76 fallback) differs from the function the user explicitly
named in their query.

**Implementation summary:**
- Asked function: extracted from `raw_query` via the existing
  W58-filtered `_extract_function_candidates_local` (NOT
  `state["object_name"]`, which the classifier sometimes sets to
  an enriched query blob — see Section 1 of the W85 PR report).
- Anchor function: `state["w70_anchor"]["function"]` (W84-exposed)
  with `state["w76_anchor"]` as fallback.
- Gates that prevent false positives: no-anchor (no signal to
  compare), no-named-function (W58 filter drops CAP codes /
  columns / tables / alias literals), asked-not-in-graph (W45
  territory), and multi-candidate match (anchor matches any
  named function → no mismatch).
- Fires INDEPENDENTLY of every content check. Anchor mismatch
  and content fabrication are distinct trust violations;
  collapsing them would underreport.

**Discovery context (original entry):**



**Failure surface.** Run 9 D1 (`Trace N_NET_INTEREST_INCOME from
STG_OPS_RISK_DATA to its final landing in FCT_STANDARD_ACCT_HEAD for
V_STD_ACCT_HEAD_ID = 'CAP170'`) — anchor cascade drifted to an
unrelated function (`INSIGNFCNT_INVST_DED_STD_ACCT_HEAD_DATA_POP`),
body contained fabricated SQL (`SELECT N_NET_INTEREST_INCOME INTO
some_variable …`) and line-citation padding (Lines 54-349 cited
three times as three different "steps"). Badged VERIFIED — no
existing W57 check caught it because the citation form was
line-range rather than function-to-function arrows that W82 catches.

**Detection signal.** Buildable directly from W84's `w70_anchor`
exposure:

1. Extract the asked-about function(s) from the query (orchestrator
   already does this for W76 anchoring).
2. Compare against `state["w70_anchor"]["function"]` — the cascade-
   resolved primary anchor passed to the explainer prompt.
3. If they disagree AND the asked-about function is in
   `functions_analyzed` (i.e., it WAS retrieved but the cascade
   picked a sibling), the body is at high risk of describing the
   wrong function. Flag as `GROUNDING-ANCHOR-DRIFT` (severity
   HIGH).

**Why distinct from W83B.** W83B targets the *content* of a calendar
claim against source-content. W85 targets the *routing* of the
anchor against the asked-about function — independent of what the
body says about calendars. A response can fail W85 without saying
anything December-related (D1 also failed on fabricated SQL form),
and a response can fail W83B with the anchor correctly placed (A2
anchors on CS_GOODWILL_CALCULATION cleanly; the failure is content).

**Why distinct from W82.** W82 (sequential-step fabrication catch)
fires when the body presents `F1 → F2 → F3` with at least one F
absent from retrieved sources. D1 evaded W82 because the response
expressed the trace as repeated line ranges from a single function
rather than function-to-function arrows. W85's signal is upstream
of W82's surface pattern.

**Priority.** Between W83B (this PR) and W82 (existing). All three
target a distinct failure surface; W85's signal-to-noise should be
very high because the comparison is purely structural (anchor name
vs asked-about name) and doesn't require source-content analysis.

**Scope.** Not addressed in W83B. Logged here for prioritization.

---

## W83C. Calendar-general overgeneralization detection — FIXED (2026-05-15)

**Failure surface.** Stakeholder test 2 (2026-05-14). Query: ``Trace
N_SIGNIFICANT_INVST_AMT from classification through deduction.``
RTIE response repeated, on every step: ``This entire function ONLY
runs when the reporting month is March 2026, specifically on the
date March 31, 2026.`` Source has only ``D_CALENDAR_DATE =
TO_DATE('20260331', ...)`` — a single calendar-date filter, NOT a
month-3 gate.

**Why W83B didn't catch it.** W83B's structure is polarity-correct
for any calendar period, but its Class C prose patterns and its
``_W57_DECEMBER_GATE_PATTERNS`` source-content gate were
December-only. "March" / "Q1" / "June" claims slipped through Class
C unrecognised even though W83B's firing rule and source-content
strategy would have caught the fabrication.

**Fix.** Mechanical pattern-set widening of W83B's Class C + a
strict per-claim source-content gate:

  - Class C extended to cover all 12 months, all 4 quarters,
    year-end variants, and month-end-date claims. Each token tagged
    with ``(period_id, claim_type, label)``. Bare month names
    (``march``, ``may``, ``june``, etc.) use ``\b``-bounded regex
    to avoid English-homonym false positives
    (``demarcation``, modal ``may run``).
  - New ``_w57_calendar_gate_supports_claim`` two-track gate:
      * **Month claims** require MONTH/EXTRACT logic for the
        specific month. A single in-month date literal does NOT
        suffice. (Closes the stakeholder case: the March-31 date
        does not support the "ONLY runs in March" claim.)
      * **Date claims** accept matching date literals.
      * **Quarter claims** accept QUARTER/MONTH evidence covering
        any member month, or any quarter-month-end date literal
        (lenient).
      * **Year-end claims** accept December month evidence OR any
        year-end date literal (preserves W83a's lenient gate for
        backward compat — `_w57_source_has_december_gate` is
        unchanged).
  - Firing rule, 80-char proximity window, anchor resolution
    (W70→W76→no-op), and dedup vs Check 5 / W83a are all
    preserved from W83B.
  - Warning message names the actual detected period (``March``,
    ``Q3``, ``year-end / fiscal year-end``), lists up to two when
    a body claims multiple unsupported periods, and keeps the
    ``GROUNDING-CALENDAR-HIGH`` code so existing W83B benchmark
    attribution still works.

**v1 scope-deferred (W83D).** OVERGENERALIZATION class — source
contains a *localized* calendar predicate (e.g., one IF branch),
prose claims whole-function gating. Requires AST control-flow
position analysis to distinguish "predicate gates function body"
from "predicate gates one nested block". Tracked as W83D below.

**Implementation.** ``src/agents/logic_explainer.py`` —
``_W57_MONTHS_META`` / ``_W57_QUARTERS_META`` /
``_W57_MONTH_END_DAYS`` metadata; generated
``_W83B_C_TOKEN_TAG_PAIRS`` / ``_W83B_CALENDAR_REFERENT``;
``_W57_MONTH_EVIDENCE_BY_NUM`` /
``_W57_QUARTER_EVIDENCE_BY_ID`` / ``_W57_YEAR_END_EVIDENCE``;
``_w57_calendar_gate_supports_claim`` /
``_w83b_collect_claim_tags`` helpers; updated
``_w57_check_calendar_gating_grounded`` to use claim-driven flow.

**Tests.** ``tests/unit/agents/test_w83c_calendar_overgeneralization.py``
(47 tests — stakeholder reproduction, every-month parameterized
smoke, quarter coverage, year-end regression, source-content gate
strict semantics, dedup, message format, end-to-end via
``w57_enforce_grounding``).

**Merge SHA.** _pending_

---

## W83D. December-gating overgeneralization (AST-based) — Deferred from W83B (2026-05-12), renamed from prior W83C (2026-05-15)

**Failure surface.** Run 9 B3 (`FN_LOAD_OPS_RISK_DATA`, CBA branch
question). Body claims `"This entire function ONLY runs when the
reporting month is December"` — but the source contains a *localized*
December conditional (`IF TO_NUMBER (EXTRACT (MONTH FROM TO_DATE
(CQD, 'DD-MON-RR'))) = 12 THEN ...`) that gates ONE nested block,
not the whole function body. The function has branches outside the
IF that run regardless of month.

**Why W83B / W83C don't catch this.** Both source-content gates
(W83B's ``_w57_source_has_december_gate`` and W83C's
``_w57_calendar_gate_supports_claim``) are binary presence checks:
any month-12 logic anywhere in the source → claim is grounded → no
fire. Localized vs whole-function gating is not distinguishable
without control-flow position analysis of the predicate (does the IF
wrap the function body, or does it wrap one branch?). RTIE has the
AST infrastructure for this (`src/parsing/query_engine.py`) but
wiring it into a W57 sub-check is non-trivial.

**Detection sketch (for future work).**

1. Detect the body phrase pattern "entire function only runs …
   December" / "this function only runs … December" / "the function
   runs only … December" — a stronger claim than W83B's hedged
   forms.
2. Locate the December predicate in source (using
   `_W57_DECEMBER_GATE_PATTERNS` or the W83C per-month evidence
   patterns).
3. Walk the AST from the predicate up: if the enclosing block
   includes the function's RETURN/COMMIT/end, the predicate gates
   the whole function — claim is grounded. Otherwise the predicate
   wraps a nested block — claim is overgeneralization → fire.

**Trade-off.** W83D's complexity is significantly higher than W83B's
or W83C's. Defer until either (a) the same class shows up in another
benchmark run, or (b) the AST utilities are needed for an unrelated
piece of work and W83D becomes cheap-to-add as a side benefit.

**Naming history.** Originally logged as W83C (2026-05-12). Renamed
to W83D when the W83C ticket was retargeted (2026-05-15) at
calendar-general pattern-set widening — a strictly mechanical fix
that landed in a single PR — to keep the smaller scope on the
faster path.

**Status.** Deferred. Not assigned to a sprint.

---

## W85b. Column-trace anchor drift — Discovered W85 validation (2026-05-12)

**Failure surface.** Run 9 D1 (``Trace N_NET_INTEREST_INCOME from
STG_OPS_RISK_DATA to its final landing in FCT_STANDARD_ACCT_HEAD
for V_STD_ACCT_HEAD_ID = 'CAP170'``) — the W70 cascade lands on a
function unrelated to the asked column trace
(``INSIGNFCNT_INVST_DED_STD_ACCT_HEAD_DATA_POP``), and the body
emits fabricated SQL with line-citation padding pointing at the
anchor's source.

**Why W85 doesn't catch this.** W85 gates on the user naming a
known function in their query. Column-trace queries name a column
(``N_NET_INTEREST_INCOME`` — W58 ``N_`` prefix → filtered), source
tables (``STG_*``, ``FCT_*`` — W58 prefix → filtered), and a code
literal (``CAP170`` — no underscore → fails the candidate regex).
With no named function to compare the anchor against, W85 correctly
no-ops. The sibling-mismatch class W85 was designed for is closed;
this column-trace class is structurally different.

**Distinct signal needed.** Two candidate detectors, either of which
would catch D1:

1. **Fabricated-SQL detector.** Body contains SQL blocks with
   identifiers (``INTO some_variable``, placeholder names,
   pseudo-variables) that don't appear in any retrieved source.
   Surface pattern: SELECT/INSERT with a target that isn't a known
   column or PL/SQL variable in the cited function's source.
2. **Distributed-line-citation-padding detector.** Same line range
   cited multiple times across the response, each framed as a
   different "step" (``Lines 54-349`` cited as Step 1, Step 2, Step
   3). Existing W57 check 1.3 catches repeat ranges as
   GROUNDING-LOW, but doesn't fire as HIGH when the repeats are
   framed as distinct sequential steps — D1's exact failure mode.

Either signal could fire independently of W85; they don't share
W85's anchor-comparison shape.

**Priority.** Lower than active work. D1 is the only Run-9 evidence
for this class so far. Reassess after the next benchmark run; if
the class repeats, lift priority. If it stays at one occurrence,
defer further.

**Status.** Deferred. Not assigned to a sprint.

---

## W88. Named regulatory computation pre-router — DIAGNOSTIC IN FLIGHT (2026-05-12)

Diagnostic for a DATA_QUERY pre-router that maps named Basel computations (BIA, CET1 ratio, RWA, LCR/NSFR, etc.) to canonical OFSERM fact tables + methodology / CAP-code filters. Empirical inventory in [docs/w88_diagnostic.md](docs/w88_diagnostic.md); fix-PR pending Toheed review.

---

## W86. DATA_QUERY all-null metric columns return VERIFIED — FIXED 2026-05-12 (merge SHA pending)

**Status:** Implemented in `_detect_all_null_metric_columns` in
[src/agents/data_query.py](src/agents/data_query.py). Sibling sub-check
to W33's Layer-4 detector, wired into the same sanity-warnings emission
point in `answer_stream`. Sets `suspicious=True` and appends
`suspicious_metric_all_null:` to `sanity_warnings`, which the
`/v1/stream` badge-decision path at
[src/main.py](src/main.py) downgrades from VERIFIED to UNVERIFIED.

**Failure surface.** Stakeholder test 2026-05-12 (`db/modules/ABL_CAR_CSTM_V4/rtie_benchmark_*.md` and W88 Section 3) surfaced two cases W33 didn't catch:

- **Q1** ("BIA op risk values on 31-Dec-2026"): aggregate returned one row with `SUM(N_ALPHA_PERCENT)=NULL`, `SUM(N_BETA_FACTOR)=NULL`. Future date has no rows in any table. W33 gates failed: (a) row_count=1 (not zero), (b) no non-date predicate, (c) baseline table is empty at the requested date. Stamped VERIFIED.
- **Q5** ("NPLs on 31-dec-2025"): row-list returned 100 rows from `STG_PRODUCT_PROCESSOR` with `N_EOP_BAL_NPL=NULL` on every row (column genuinely empty for this date, confirmed independently). W33's first gate failed: rows came back. Stamped VERIFIED.

**Detection signal.** Fire `suspicious_metric_all_null` when:
1. Result has at least one row (W33's territory when zero).
2. Every metric column in the result is 100% NULL across all returned rows (full-missing answer, not partial).

Metric column classification combines three rules: (a) the SELECT-list entry is an aggregate other than `COUNT(*)`/`COUNT(<int>)`, (b) Oracle `data_type` is NUMBER and the column name is not a known dimension suffix (`*_SKEY`, `*_DATE`, `*_ID`, `*_CODE`, `*_FLAG`, `*_IND`, `FIC_MIS_DATE`, `N_RUN_SKEY`), or (c) the column name starts with `N_` (OFSAA measure convention) and is not a dimension suffix. When type metadata is missing, falls back to a conservative-broad rule that still excludes obvious dimension prefixes.

**v1 scope.** Only fully-missing answers (every metric column all-null). Partial-null cases — Q7-style, where some metric columns have values and others are NULL — are explicitly deferred. `COUNT(*)` aggregates returning 0 are excluded (0 is a real answer to "how many"). W86 is suppressed when W33 has already fired (already UNVERIFIED — no double-warning noise).

**Why distinct from W33.** W33 catches zero-result aggregates against populated tables with non-date predicates (CHAR-padding / case-mismatch class). W86 catches the all-null class — table empty at the requested date, or column empty across the returned rows. Different gates, different signal.

**Tests.** 27 new unit tests in [tests/unit/agents/test_data_query_w86.py](tests/unit/agents/test_data_query_w86.py) covering metric-column classification, all-null detection, partial-null exclusion, dimension exclusion, COUNT(*) exclusion, and end-to-end streaming wiring.

---

## W89. VARIABLE_TRACE chain ordering — FIXED in this PR

- **Discovered:** Stakeholder test 2 (2026-05-14). RTIE response walked the retrieved functions in a non-execution order while Cowork's reference walked classification → aggregation → threshold → deduction. Calibration evidence preserved at [scratch/stakeholder_test_2_2026-05-14_chain_ordering.md](scratch/stakeholder_test_2_2026-05-14_chain_ordering.md).
- **Root cause:** The VARIABLE_TRACE chain assembly didn't consult manifest `task_order`. The order signal exists (W39, stored under each function's `graph:{schema}:{fn}` hierarchy block) but wasn't reaching the narrative-generator. Two surfaces ordered alphabetically pre-W89: `tagged_lines.sort(key=lambda x: (x["function"], x["line"]))` in [src/agents/variable_tracer.py](src/agents/variable_tracer.py) `extract_relevant_lines`, and the outer `sorted(by_function.items())` in `build_transformation_chain`. The response payload's `functions_analyzed` array used semantic-rank order (`list(state["multi_source"].keys())`).
- **Fix:** New [src/agents/chain_ordering.py](src/agents/chain_ordering.py) helper `order_chain_by_manifest` sorts by `(batch, process, sub_process_path, task_order)` before narrative generation. Unmanifested functions sort to the end in their original input order. Wired into [src/main.py](src/main.py) `event_stream` BEFORE the meta event emit (gated on `query_type == "VARIABLE_TRACE"` so FUNCTION_LOGIC / COLUMN_LOGIC / DATA_QUERY are unaffected) and into `build_transformation_chain`'s new `function_order` parameter. `VARIABLE_TRACE_PROMPT` got one additive sentence instructing the LLM to walk the provided functions in the order they're given.
- **Tests:** 20 unit tests in [tests/unit/agents/test_w89_chain_ordering.py](tests/unit/agents/test_w89_chain_ordering.py) covering: simple task_order sort, multi-batch / multi-process / multi-sub-process sort, unmanifested-to-end, empty / single / already-sorted no-op, Redis failure fallback, partial manifest entry, cross-schema chain, `build_transformation_chain` order honoured. 3 integration tests in [tests/integration/test_live_stream.py](tests/integration/test_live_stream.py) cover the live SSE round-trip (functions_analyzed monotonicity check, FUNCTION_LOGIC shape unchanged, DATA_QUERY shape unchanged).
- **Merge SHA:** pending.

---

## W80. Cross-table multi-stage VARIABLE_TRACE retrieval — scope expanded 2026-05-14

- **Original scope (Run 8):** ~25% retrieval miss on VARIABLE_TRACE queries. Documented as a known failure surface but framed as a partial coverage gap.
- **Actual scope (stakeholder test 2, 2026-05-14):** Closer to 100% retrieval miss on cross-table multi-stage VARIABLE_TRACE queries. The `N_SIGNIFICANT_INVST_AMT` trace returned 10 functions, 0 matching Cowork's correct 5-function pipeline. Pure name-similarity matching missed every upstream function operating on different table names. Evidence preserved at [scratch/stakeholder_test_2_2026-05-14_chain_ordering.md](scratch/stakeholder_test_2_2026-05-14_chain_ordering.md).
- **Implication for the fix:** Semantic search by name-similarity alone is not sufficient. W80 implementation must consider graph-edge traversal (writer → column → table → reader) as a signal complementary to semantic search, not a replacement. Multi-stage chains span sub-processes named differently from the target variable; the only reliable retrieval signal across those boundaries is the manifest-anchored graph itself.
- **Relationship to W89:** Orthogonal. W89 (chain ordering, fixed this PR) only guarantees that whatever functions retrieval returned are presented in execution order. W80 fixes which functions retrieval returns. Both must land for stakeholder-style queries like the test_2 trace to produce a Cowork-equivalent answer.

---

## W90. Distributed citation-padding (HIGH tier) — NEW 2026-05-14

- **Discovered:** Stakeholder test 2 (2026-05-14). GROUNDING-LOW fired on "Line 24 cited 4 times" (the W57 padding detector working as designed at LOW tier). But the actual padding pattern was 27 distinct empty-text citations at the same line across multiple SQL blocks — distributed padding at scale, not just same-line repetition.
- **Today:** LOW tier, advisory only, badge stays VERIFIED.
- **Fix needed:** When over a threshold (e.g. 10+ empty-text citations, or all citations point to a single line within a single function), escalate to HIGH and flip badge. The signal is qualitatively the same as W57's same-line repeat detector but operates on the broader count.
- **Priority:** Bundle with W82 (similar surface — both are fabrication-style detectors).

---

## W91. `(SCHEMA)` placeholder leak in markdown — NEW 2026-05-14

- **Discovered:** Stakeholder test 2 (2026-05-14). Response heading shows literal `(SCHEMA)` — a template placeholder that wasn't substituted with the actual schema name. Also surfaced in Q9 of the 2026-05-12 stakeholder test, so this is reproducible.
- **Root cause:** `VARIABLE_TRACE_PROMPT` in [src/agents/variable_tracer.py](src/agents/variable_tracer.py) instructs the LLM to `Start with: ## {VARIABLE_NAME} in `FUNCTION_NAME` (SCHEMA)`. The LLM treats `(SCHEMA)` as literal text rather than a variable to fill. The prompt should either pre-substitute the schema or remove the bracketed token.
- **Priority:** Small fix; bundle with W50 (formatting pass).

---

## W92. Response schema-label mismatch — NEW 2026-05-14

- **Discovered:** Stakeholder test 2 (2026-05-14). `data.schema: "OFSMDM"` in the response payload, but every table cited in the response body (FSI_NON_REG_CONSL_ENTITY_INVST, etc.) is OFSERM. `schema_searched` correctly lists both schemas; only the single-schema label is wrong.
- **Root cause hypothesis:** The response builder reads `state["schema"]` (the orchestrator's primary-schema guess), which is the request's classified routing schema, not the schema(s) actually consulted during retrieval. After Phase 3, each `multi_source` entry carries its own `schema` field — the response builder should aggregate from those instead.
- **Priority:** Bundle with W35 Phase 8 cleanup work — same broader theme of "schema is no longer single-valued post-Phase-3."

---

> **Priority queue note (2026-05-14):** Stakeholder test 2 surfaced W89 (fixed this PR) + W90 + W91 + W92 + W80 scope expansion. Updated priority queue reflects the new tickets. Calibration evidence preserved at [scratch/stakeholder_test_2_2026-05-14_chain_ordering.md](scratch/stakeholder_test_2_2026-05-14_chain_ordering.md).

---

## W87. Orchestrator entity-extraction fallback — FIXED 2026-05-15 (merge SHA pending)

- **Discovered:** Stakeholder test 1 (2026-05-12) Q11 — "what is the threshold value for G Test". Transcript was paste-only context in the chat, not committed to scratch/. RTIE stamped `object_name` with the concatenated enriched-query blob ("what is the threshold value for G Test Find the threshold value used for the G Test check G Test G_T"), passed that to semantic search, anchored on `CS_THRESHOLD_TREATMENT_AGGREGATE_THRESHOLD_ASSIGNMENT`, and fabricated a December gate that W83a caught as UNVERIFIED. Cowork's reference response was an honest "I don't know — please clarify what 'G Test' maps to."
- **Root cause:** [src/agents/orchestrator.py:669](src/agents/orchestrator.py#L669) (`classify_query`) sets `state["object_name"] = enriched_query` where `enriched_query = f"{query} {result.intent} {' '.join(result.search_terms)}"`. When no orchestrator-stage resolver — function-name extraction (W58 filter), W76 named-function anchor, or BI literal-index routing — successfully resolves the query, this concatenated blob is what reaches the embedding call at [src/main.py:1084](src/main.py#L1084). Semantic search then returns name-similar but unrelated functions and the narrative LLM anchors on one of them. The trust-violation chain (semantic search → narrative LLM → W83a fabrication catch) is downstream of this initial fallback.
- **Fix:** New `_detect_unrecognized_term_query` gate at [src/agents/orchestrator.py:1339](src/agents/orchestrator.py#L1339), wired between `apply_bi_routing` and the embedding call at [src/main.py:1018-1064](src/main.py#L1018-L1064). Fires when `query_type ∈ {FUNCTION_LOGIC, COLUMN_LOGIC, VARIABLE_TRACE}` AND `extract_function_candidates(raw_query)` is empty AND `state["bi_routing"]` is absent AND the W76 anchor record has no function AND any classifier-set `target_variable` fails `schemas_for_column` lookup. Builds a deterministic UNVERIFIED clarification body via `build_unrecognized_term_response` ([orchestrator.py:1404](src/agents/orchestrator.py#L1404)) — mirrors W37's `build_function_not_found_response` shape but with `badge="UNVERIFIED"`, `confidence=0.2`, and a `UNRECOGNIZED_TERM: '{term}' not in indexed corpus` warning. Streamed via `_stream_unrecognized_term_response` at [src/main.py:2397](src/main.py#L2397) (stage → meta → tokens → done). W87 is an architectural sibling of W37 (pre-search, deterministic body) — NOT W45/W49 (which are post-retrieval). Term extraction prefers the classifier's `target_variable`, falls back to quoted phrase, then multi-word capitalized run, then longest single capitalized non-stopword token; returns None when no term can be isolated, which falls through to the existing classifier-`partial_flag` clarification path.
- **Tests:** 35 unit tests in [tests/unit/agents/test_w87_unrecognized_term.py](tests/unit/agents/test_w87_unrecognized_term.py) cover the gate (positive G-Test reproduction, target_variable-vs-heuristic priority, VARIABLE_TRACE query type, quoted phrase, unfindable business concept) and negatives (known function, CAP-code BI routing, W76 anchor, empty W76 anchor, resolved column, DATA_QUERY / UNSUPPORTED / VALUE_TRACE / empty query types, mixed-with-known-function, column-check raises). Term-extraction edge cases and variation-generation are pinned. Response-builder shape pinned (badge, validated, confidence, warnings, type, status, requested_term, message vs explanation.markdown sync, honest naming of indices RTIE actually consults). 3 integration tests appended to [tests/integration/test_live_stream.py](tests/integration/test_live_stream.py) — fires on Q11 reproduction, no-fire on FN_LOAD_OPS_RISK_DATA, no-fire on CAP973. All pass against live backend. Manual canaries captured at [scratch/w87_canary_a.txt](scratch/w87_canary_a.txt) / [b.txt](scratch/w87_canary_b.txt) / [c.txt](scratch/w87_canary_c.txt).
- **Merge SHA:** pending.

---

## Operational observations — surfaced during W83C canary run (2026-05-15)

Two pre-existing items observed while running [tests/integration/test_live_stream.py](tests/integration/test_live_stream.py) against the live backend for W83C verification. Neither was introduced by W83C; logged here so they don't get lost.

### Integration suite has 8 unrelated pre-existing failures

The full live-stream integration suite reports **PASS 27 / FAIL 8 / Total 35** on the post-W87 main. Failing tests, by ID:

- TEST 2 — Named function IS in graph: FN_LOAD_OPS_RISK_DATA (asserts no GROUNDING warning but a `pass-through` template-phrase warning fires)
- TEST 4 — Business identifier IS in a loaded function (asserts no GROUNDING warning; ungrounded-citation + template-phrase warnings fire)
- TEST 6 — New module folder discovery (asserts `graph:OFSMDM:TEST_SIMPLE` exists in Redis; key missing)
- TEST 7 — OFSERM file parsing with warning (asserts `graph:OFSERM:ABL_DEF_PENSION_FUND_ASSET_NET_DTL` exists; key missing)
- TEST 11 — W45 CAP973 structured response (asserts ungrounded warning + W45 `next_step` markdown; gets a different anchor + missing markdown sections)
- TEST 12 — W45 regression: grounded VARIABLE_TRACE (asserts VERIFIED; gets UNVERIFIED with multiple unrelated warnings)
- TEST 14 — W49 ABL_Def_Pension partial-source structure (gets `function_not_found` / DECLINED instead of UNVERIFIED partial-source markdown)
- TEST 16 — W49 regression: CAP973 W45 branch wins (gets a different anchor + missing W45 markdown markers)

Many of these stem from incremental warning growth (multiple W57 sub-checks now firing on the same body that previously had one) and from Redis/state expectations from earlier tickets that have drifted. None block the W83C merge — they are not regressions introduced by W83C — but they suggest the integration suite needs a calibration pass against current pipeline behavior. Not yet ticketed.

### Windows cp1252 encoding crash in the integration runner

`python tests/integration/test_live_stream.py` crashes mid-suite on Windows (PowerShell / cmd default code page 1252) when a test result contains the U+2192 arrow character (`→`):

```
UnicodeEncodeError: 'charmap' codec can't encode character '→' in position N: character maps to <undefined>
```

Workaround: set `$env:PYTHONIOENCODING = "utf-8"` before invocation. Affects the W84 cross-flow VARIABLE_TRACE test (and any later test in the run, since the crash terminates the process).

Permanent fix would be one of:
- Replace `→` with the ASCII `->` in test names / strings (lowest-risk single-line change in the test file).
- Add `sys.stdout.reconfigure(encoding='utf-8')` at the top of [tests/integration/test_live_stream.py](tests/integration/test_live_stream.py) so the runner is encoding-safe regardless of host shell.
- Document the `PYTHONIOENCODING=utf-8` requirement in the Windows onboarding doc.

Not blocking; the suite runs to completion under `PYTHONIOENCODING=utf-8`. Not yet ticketed.
