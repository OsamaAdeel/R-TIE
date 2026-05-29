# RTIE Weakness Log

Catalog of known weaknesses in RTIE's grounding/routing layers, with
discovery context. Entries are append-only; closed weaknesses keep
their entry with a status update rather than being deleted.

Numbering matches the W-ticket convention used in branches, code
comments, and PR titles (`refactor/w35-…`, `fix/w83b-…`, etc.).

---

## W101. Manifest validator too strict for OFSAA N:M semantics — FIXED 2026-05-20

**Status:** Implemented and merged on `fix/w101-manifest-validator-relaxation`. Live in `_validate_and_index` in [src/parsing/manifest.py](src/parsing/manifest.py).

**Architectural principle.** The manifest validator must reflect OFSAA semantics, not Python-collection intuitions. OFSAA's runchart model:

1. **Task order = absolute runchart row position.** A sub-process whose runchart lists 16 rows but only ships 6 active PL/SQL tasks (TYPE3/TYPE2 rows are filtered out at manifest-authoring time) legitimately has order values `[2, 4, 5, 6, 8, 16]`. The orders are unique within container, but contiguous 1..N is the wrong invariant.
2. **Function-task is N:M.** The same `.sql` function can be wired into N task slots across M process contexts (e.g., `BNK_UNDERLYING_EXPOSURES_DATA_POPULATION` fires once under `ABL_INVESTMENT_DATA_POPULATION > BNK_PRODUCT_PROCESSOR_DATA_POPULATION` and again under `SEC_DATA_POPULATION`). Task names repeat legitimately; what stays globally unique is `function_name × source_file`.

**Failure surface.** After Stage 1+2+3 corpus update (2026-05-20), `python run.py` would crash at loader startup with `ManifestValidationError`. Diagnostic audit of all 120 task containers in `ABL_CAR_CSTM_V4/manifest.yaml`:

- **39 containers** had non-contiguous all-orders (validator FAIL on contiguity rule)
- **14 containers** already used the runchart-absolute-position pattern but passed the validator coincidentally (their inactive placeholder rows filled the gaps, making the all-orders set 1..N)
- **3 active task names** appeared in two distinct sub-processes each, all sharing the same `source_file` (validator FAIL on global-uniqueness)

The 14 coincidentally-passing containers proved the runchart-absolute-position convention was already the de facto manifest authoring spec; the validator's `[1..N]` rule was the outlier.

**Fix.** Two narrowly-scoped validator relaxations in [src/parsing/manifest.py](src/parsing/manifest.py) `_validate_and_index`:

1. **Order check** — require `len(orders) == len(set(orders))` (unique within container); drop `sorted(orders) == [1..N]`. Emit a `logger.debug` line when the active subset has gaps so the convention stays auditable from logs alone.
2. **Global-uniqueness check** — allow duplicate active task names when every occurrence shares the same `source_file` basename (N:M semantics). Differing `source_file` for the same name remains a hard error (real name collision worth catching). The `_file_index` collision check was relaxed in the same spirit (debug-log + keep first binding).

**Tests** ([tests/unit/parsing/test_manifest.py](tests/unit/parsing/test_manifest.py)):

- `test_non_contiguous_orders_pass_w101` — container with `[2, 4, 5, 6, 8, 16]` validates clean
- `test_duplicate_orders_within_container_raise_w101` — two active tasks at order=1 still rejected
- `test_same_name_same_source_across_containers_passes_w101` — OFSAA N:M happy path
- `test_same_name_different_source_across_containers_raises_w101` — real name collision still rejected

All 18 manifest tests pass.

**Out-of-scope.** Found-but-deferred manifest issues surfaced once the contiguity gate was removed: one Stage-3 task entry has `name: "FN PRODUCT RECLASS CSTM"` (spaces) referencing `source_file: FN_PRODUCT_RECLASS_CSTM.sql` (underscores), failing the function-name-match check at `_validate_task`. Pre-existing manifest authoring bug, not a W101 regression — flagged separately for the Stage-3 fixup pass.

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

## W85. Anchor-vs-asked-function mismatch — FIXED 2026-05-12 (merge SHA 8cd8354)

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

**Merge SHA.** 83cfb0c (2026-05-15)

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

## W88. Named regulatory computation pre-router — FIXED 2026-05-18 (merge SHA 702fca5)

- **Discovered:** Diagnostic 2026-05-12 ([docs/w88_diagnostic.md](docs/w88_diagnostic.md)). Stakeholder queries naming Basel-defined regulatory computations (BIA op-risk capital, CET1 ratio, total Credit Risk RWA, LCR, NSFR) all classified correctly as `DATA_QUERY` but every one routed to `OFSMDM` staging with the LLM SQL generator fabricating queries against `ABL_OPS_RISK_DATA`. Diagnostic Section 3 measured 15/15 queries routing wrong: zero of fifteen reached the correct `OFSERM.FCT_OPS_RISK_DATA` / `OFSERM.FCT_STANDARD_ACCT_HEAD` tables, every one returned `VERIFIED` with null or wrong results. Pre-existing W86 catches the all-null result shape downstream but never prevents the misrouting.
- **Root cause:** Two-stage failure. (a) The classifier correctly stamps `query_type=DATA_QUERY` for these queries but stamps no authoritative `target_table` or `required_filters` — those decisions are pushed to the LLM SQL generator. (b) The schema catalog the LLM sees at SQL-generation time is dominated by OFSMDM staging tables (loader-rejected OFSERM detail tables don't appear), so the LLM picks the only thing it sees. The named computation never reaches its canonical fact table even though both `FCT_OPS_RISK_DATA` and `FCT_STANDARD_ACCT_HEAD` exist with the right data in the local Oracle.
- **Fix:** New module [src/agents/computation_router.py](src/agents/computation_router.py) — a deterministic pre-router with a static registry of 9 named computations (6 anchor + 3 decline) per diagnostic Section 5 recommended v1 scope. Wired into [src/agents/data_query.py](src/agents/data_query.py) between `_resolve_target_schema` and `_build_schema_catalog` per D3. When `detect_named_computation(raw_query, "DATA_QUERY")` matches, the router short-circuits the LLM SQL generation step: anchor-arm computations emit canonical SQL against the known OFSERM fact table (Pattern B per D2 — full short-circuit, not just anchor-stamping); decline-arm computations emit a structured `status="unsupported"` payload distinct from W45 / W49 in framing (references the regulatory concept and the OFSAA module scope, not a function name). Guardian validation and Oracle execute run unchanged on the canonical SQL.
- **Two anchor families:** Method-SKEY family (`OFSERM.FCT_OPS_RISK_DATA` filtered by `N_BASEL_METHOD_SKEY` joined to `DIM_BASEL_METHODOLOGY`) — BIA in v1. CAP-code family (`OFSERM.FCT_STANDARD_ACCT_HEAD` filtered by `V_STD_ACCT_HEAD_ID` joined to `DIM_STANDARD_ACCT_HEAD`) — CET1 (CAP960), Tier 1 (CAP214), CAR (CAP192), aggregate Credit RWA (CAP169), aggregate Market RWA (CAP090). Both SQL shapes use `DENSE_RANK() OVER (ORDER BY N_MIS_DATE_SKEY DESC, N_RUN_SKEY DESC)` to scope to the latest run (the diagnostic Section 7 anomaly #8 flagged that naive SUM-across-runs returns the wrong number).
- **SKEY resolution (D4):** Method-SKEY anchors resolve their code-string (e.g. `'ORBIA'`) to an integer SKEY via `DIM_BASEL_METHODOLOGY` lookup on first use, cached in module-global `_SKEY_CACHE`. On lookup failure (Oracle unreachable, OFSAA upgrade renamed the code, DIM not populated) the router caches `None` and the affected anchor degrades to a structured decline that explicitly surfaces the operational cause rather than the regulatory scope. **Never ship a guessed SKEY** — the fragility risk the diagnostic Section 3 flagged is closed.
- **Decline arm:** `LEVERAGE_RATIO` (CAP843 = 0.0 placeholder, no loader function — surfaces "Tier 1 Capital Ratio" as the alternative since that one IS computed), `LCR` (no fact table in either schema; lives in the OFSAA Liquidity Risk Management module which isn't loaded), `NSFR` (same scope explanation). `badge` derivation stays with main.py's existing `status="unsupported" → "REJECTED"` map — REJECTED reads as "informed refusal" which is correct for W88 declines.
- **Architecture (D5):** Static Python dict — `W88_NAMED_COMPUTATIONS: tuple[W88ComputationDefinition, ...]`. Inventory is small (15 in diagnostic; 9 implemented in v1) and stable (Basel + OFSAA seed codes). LLM fallback was rejected explicitly: today's failure mode IS the LLM picking wrong tables; falling back to it on registry miss would re-introduce that exact behavior. Adding a new computation is a code change, appropriate for content this stable.
- **W87 interaction:** Not load-bearing. W87's gate set is `{FUNCTION_LOGIC, COLUMN_LOGIC, VARIABLE_TRACE}` — DATA_QUERY is outside it ([src/agents/orchestrator.py:1380](src/agents/orchestrator.py#L1380)). All 15 stakeholder queries classify as DATA_QUERY and never reach W87 (diagnostic Section 3 confirms). Composition is clean: W87 handles entity-seeking failures; W88 handles named-computation routing; neither steps on the other.
- **Tests:** 60 unit tests in [tests/unit/agents/test_w88_computation_router.py](tests/unit/agents/test_w88_computation_router.py) — registry integrity (6 anchor + 3 decline, no duplicate names, anchor/decline shape complete), positive detection per computation (24 parametrized cases covering long name + acronym + CAP-code variants), negative cases (non-DATA_QUERY types, empty / None / non-string inputs, generic data queries that must not match), edge cases (case-insensitivity, hyphen / underscore variants, embedded in longer queries, registry-order determinism), SKEY resolution (lazy lookup, success / failure / null-row / empty-result caching, cap-code anchors don't probe), plan-building (SQL shape per family, DENSE_RANK presence, params binding, None on SKEY-unresolved), decline-payload (pure decline vs SKEY-unresolved fallback, alternative line surface, shape compatibility with main.py's done-payload contract). 10 integration canaries appended to [tests/integration/test_live_stream.py](tests/integration/test_live_stream.py) — one per anchor (6) + one per decline (3) + one regression confirming the W33 CHAR-padding canary still passes with `w88_anchor` absent. Updated 1 pre-existing W86 unit test (`test_stream_fires_w86_on_all_null_aggregate`) to use a non-W88-matching `user_query` ("Get the alpha and beta totals from op risk staging on 2026-12-31" — same scenario, different phrasing) so W86's stream-wiring test still exercises the LLM-SQL → all-null path; the W86 detector itself is unchanged.
- **Out of scope:** TSA / ASA / AMA (method-SKEY 77/36/16 — table-reachable but 0 rows in current run), CR-IRB-F / CR-IRB-A (need `FCT_NON_SEC_EXPOSURES` which is absent locally), MR-IM (needs `FCT_MR_VAR_DATA` which is empty). All six are documented in the diagnostic as honest-decline-when-empty candidates; left out of v1 to keep the scope tight on items with live data. CAP170 (Operational RWA aggregate) is the natural downstream landing of BIA but wasn't in the diagnostic's v1 list — deferred for a follow-up that handles user phrasings like "Operational Risk RWA" explicitly without conflating with BIA's per-row capital charge. Manifest-driven registry (Pattern B from diagnostic Section 4) deferred — static dict is right for v1 stability.
- **Backlog (surfaced during canary review):** [W88b](w88b_classifier_dependency_for_named_computations.md) — classifier-dependency for date-less queries: production users asking "What is the CET1 ratio?" (no date) get routed to FUNCTION_LOGIC and intercepted by W87 before W88 fires. Recommended fix is moving W88 detection upstream of the classifier or adding a classifier hint; priority medium-high. [W88c](w88c_bia_sum_scope_single_entity_vs_aggregate.md) — BIA SUM scope: W88 v1 returns the bank-consolidated total (`SUM across entities at latest run`, 20.40 B PKR), the diagnostic's cowork reference was a single-entity sample (20.39 B PKR), 0.06% delta. Worth resolving before stakeholders notice; priority medium.
- **Merge SHA:** 702fca5 (2026-05-18).

---

## W86. DATA_QUERY all-null metric columns return VERIFIED — FIXED 2026-05-12 (merge SHA 9db4fc7)

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

## W89. VARIABLE_TRACE chain ordering — FIXED 2026-05-14 (merge SHA 334054e)

- **Discovered:** Stakeholder test 2 (2026-05-14). RTIE response walked the retrieved functions in a non-execution order while Cowork's reference walked classification → aggregation → threshold → deduction. Calibration evidence preserved at [scratch/stakeholder_test_2_2026-05-14_chain_ordering.md](scratch/stakeholder_test_2_2026-05-14_chain_ordering.md).
- **Root cause:** The VARIABLE_TRACE chain assembly didn't consult manifest `task_order`. The order signal exists (W39, stored under each function's `graph:{schema}:{fn}` hierarchy block) but wasn't reaching the narrative-generator. Two surfaces ordered alphabetically pre-W89: `tagged_lines.sort(key=lambda x: (x["function"], x["line"]))` in [src/agents/variable_tracer.py](src/agents/variable_tracer.py) `extract_relevant_lines`, and the outer `sorted(by_function.items())` in `build_transformation_chain`. The response payload's `functions_analyzed` array used semantic-rank order (`list(state["multi_source"].keys())`).
- **Fix:** New [src/agents/chain_ordering.py](src/agents/chain_ordering.py) helper `order_chain_by_manifest` sorts by `(batch, process, sub_process_path, task_order)` before narrative generation. Unmanifested functions sort to the end in their original input order. Wired into [src/main.py](src/main.py) `event_stream` BEFORE the meta event emit (gated on `query_type == "VARIABLE_TRACE"` so FUNCTION_LOGIC / COLUMN_LOGIC / DATA_QUERY are unaffected) and into `build_transformation_chain`'s new `function_order` parameter. `VARIABLE_TRACE_PROMPT` got one additive sentence instructing the LLM to walk the provided functions in the order they're given.
- **Tests:** 20 unit tests in [tests/unit/agents/test_w89_chain_ordering.py](tests/unit/agents/test_w89_chain_ordering.py) covering: simple task_order sort, multi-batch / multi-process / multi-sub-process sort, unmanifested-to-end, empty / single / already-sorted no-op, Redis failure fallback, partial manifest entry, cross-schema chain, `build_transformation_chain` order honoured. 3 integration tests in [tests/integration/test_live_stream.py](tests/integration/test_live_stream.py) cover the live SSE round-trip (functions_analyzed monotonicity check, FUNCTION_LOGIC shape unchanged, DATA_QUERY shape unchanged).
- **Merge SHA:** 334054e (2026-05-14).

---

## W80 v1. Vector retrieval embedding input poisoning — FIXED 2026-05-15 (merge SHA f2945c5)

- **Discovered:** Retrieval diagnostic following stakeholder test 2 (2026-05-14). The description-quality + RediSearch probe exercise traced the near-100% retrieval miss on the significant-investment trace (`Trace N_SIGNIFICANT_INVST_AMT from classification through deduction`) to a specific code path: the embedding input to vector search was the classifier's enriched_query blob (`f"{query} {result.intent} {' '.join(result.search_terms)}"`), stamped by `Orchestrator.classify_query` at [src/agents/orchestrator.py:669](src/agents/orchestrator.py#L669). For anchorless queries (no W76 prefix, no CAP-code BI routing), this blob was what reached `aembed_query` at [src/main.py:1084](src/main.py#L1084) and [src/pipeline/logic_graph.py:135](src/pipeline/logic_graph.py#L135) — a diffuse, averaged centroid pulled away from actual function semantics by classifier restatement noise.
- **Root cause:** `state["object_name"]` was being used as a dual-purpose field: the classifier wrote the search-enrichment blob to it, and W76 / BI routing later overwrote with a clean function name when their preconditions fired. The two embedding sites read `state.get("object_name", state["raw_query"])` — picking up whichever shape was present. The bug surface was the anchorless path where neither W76 nor BI fired and the blob reached the embedding unchanged.
- **Fix:** Stop overwriting `object_name` at orchestrator.py:669 — drop the blob construction entirely (discovery confirmed no consumer reads it). `object_name` is now owned exclusively by [`apply_named_function_anchor`](src/agents/orchestrator.py#L526) (W76) and [`apply_bi_routing`](src/agents/orchestrator.py#L1163) (BI). At both embedding sites the input is resolved via a new helper [`resolve_search_query`](src/agents/anchor_resolution.py) — `state.get("object_name") or state.get("raw_query") or ""`. The explicit `or` form (not `dict.get(key, default)`) is load-bearing: the initial state seeds `object_name = ""`, and an empty string must fall through to `raw_query` rather than be returned as-is.
- **Related:** [W43](#w43) fixed this for the graph pipeline by re-extracting candidates from `raw_query`. W80 v1 closes the same gap for the vector retrieval path. [W87](#w87-orchestrator-entity-extraction-fallback--fixed-2026-05-15-merge-sha-d311890) shares the same surface — it short-circuits unrecognized-term queries before they reach the embedding; W80 cleans up what reaches the embedding when W87 doesn't fire.
- **Tests:** 17 unit tests in [tests/unit/agents/test_w80_embedding_input.py](tests/unit/agents/test_w80_embedding_input.py) covering the resolver precedence (clean object_name wins → raw_query fallback → empty), the classifier regression (never sets object_name to blob, never relocates blob to another state field), and the end-to-end precedence with W76 anchors. 3 integration tests appended to [tests/integration/test_live_stream.py](tests/integration/test_live_stream.py): the stakeholder-test-2 reproduction (recall floor: 2 of 5 Cowork-pipeline functions retrieved), anchored-function regression (FN_LOAD_OPS_RISK_DATA unchanged), BI-routing regression (CAP973 unchanged).
- **Out of scope (logged as follow-ups):**
  - **W80a** — regenerate stunted vector-store descriptions. Function #1 of the significant-investment pipeline (`CAP_CONSL_NON_REGULATORY_ENTITY_SIGNIFICANT_INVESTMENT_IDENTIFICATION`) has a single-paragraph description while the other 4 carry 2-3 paragraph LLM-generated descriptions naming tables, columns, regulatory context, and Basel-III stage. Threshold: regenerate any description under 500 chars.
  - **W80b** — hybrid BM25 + KNN retrieval with per-query-type adaptive top-K. The TEXT fields (`function_name`, `description`, `tables_read`, `tables_written`, `key_columns`) are indexed in RediSearch but never queried — dead weight at retrieval time. Adding a BM25 pass over those fields and blending into the KNN ranking would surface dense semantic clusters that pure cosine misses. Top-K is fixed at 5 today; cross-table multi-stage chains need more.
  - **Cross-table multi-stage chain traversal** (graph-edge-based retrieval) remains the [W80 umbrella entry below](#w80--cross-table-graph-traversal-original-umbrella-scope) — a separate ticket from this embedding-input slice.
- **Merge SHA:** f2945c5 (2026-05-18).

---

## W80b. Per-query-type top-K for vector retrieval — FIXED 2026-05-16 (merge SHA bb87a25)

- **Discovered:** W80 v1 canary measurement (2026-05-16). Post-W80 v1 the embedding input was clean, but the canary still surfaced only 2 of 5 Cowork-correct functions for the significant-investment trace, with 3 closely-related siblings beating one target function out of the top-5. RediSearch probe confirmed the cluster has 15 functions in OFSERM (`FT.SEARCH idx:rtie_vectors "@description:(significant investment)"` returns 15) — top_k=5 at [src/main.py:1098](src/main.py#L1098) and [src/pipeline/logic_graph.py:159](src/pipeline/logic_graph.py#L159) was the structural ceiling, not embedding quality.
- **Root cause:** Vector search was called with hardcoded `top_k=5` at every site. The KNN cutoff truncated rank-6+ candidates regardless of how well the embedding ranked them. With W79 per-schema fan-out the merged set was ~10, still well short of the 15-function cluster.
- **Fix:** Tactical bridge — replace the hardcoded value with a per-query-type lookup. New module [src/agents/retrieval_config.py](src/agents/retrieval_config.py) defines `W80B_TOP_K_BY_QUERY_TYPE` and `resolve_top_k(query_type)`. Both call sites in main.py and logic_graph.py route through `resolve_top_k(state.get("query_type"))`. The asymmetry is by design: `FUNCTION_LOGIC` stays at 5 (anchored upstream by W76 / BI routing / W87, extra candidates only add noise); `COLUMN_LOGIC` raises to 15 (a column can have many writers); `VARIABLE_TRACE` raises to 20 (multi-stage chains, with headroom over the 15-function cluster); `VALUE_TRACE` / `DIFFERENCE_EXPLANATION` / `DATA_QUERY` / `UNSUPPORTED` stay at 5 (vector search is advisory or unused on those paths).
- **Cost note:** Embedding cost is per-query, not per-result — bumping top_k doesn't multiply embedding API calls. RediSearch KNN cost scales with top_k but in microseconds at this corpus size (178 docs). With per-schema fan-out (`schema_scope=ALL`) the actual returned set can be roughly `2 * top_k` for two schemas — for VARIABLE_TRACE that's 40 candidates merged, bounded and acceptable. Downstream consumers (`fetch_multi_logic`, narrative LLM prompt, W57 grounding scan, W89 chain reorder, meta-event payload) audited — no load-bearing assumption on `len(multi_source) <= 5`. The `[:5]` slices in [src/agents/logic_explainer.py:1760, 1777, 1789](src/agents/logic_explainer.py#L1760) are display caps in W57 warning messages, not logic gates.
- **Tests:** 14 unit tests in [tests/unit/agents/test_w80b_top_k_routing.py](tests/unit/agents/test_w80b_top_k_routing.py) — lookup-table values per query_type, dict.get fallback on unknown / None / empty-string inputs, configuration invariants (default = 5, VARIABLE_TRACE > FUNCTION_LOGIC, VARIABLE_TRACE >= 15 cluster size). Integration canary in [tests/integration/test_live_stream.py](tests/integration/test_live_stream.py) asserts two properties: (1) recall floor >=2 of 5 (matches W80 v1 — confirms W80b didn't regress retrieval); (2) candidate-set size >5 (proves the per-query-type top-K routing fired and expanded beyond the pre-W80b ceiling). The candidate-set assertion is W80b's load-bearing signal — if a future change reverts the routing, this canary fails even when recall (the W80 v1 floor) still passes.
- **Empirical outcome — FLAT:** First live measurement post-W80b (2026-05-16) returned 20 retrieved entries (vs 5 pre-W80b — mechanism confirmed active) but recall stayed at 2 of 5. The OFSERM top-10 contained the 2 matched targets plus 8 close siblings (CS_SIGNIFICANT_INVST_*, CS_INSIGNIFICANT_*, CS_REGULATORY_INVESTMENTS_*); the 3 missing targets ranked below 20. Cosine similarity is correctly discriminating between functions whose descriptions semantically match "non-regulated entity workflow" (the 2 surfacing) and ones that don't (the 3 missing describe party-level identification, threshold treatment, and capital-deduction-exposure population — distinct semantic foci). **Cluster-density alone was not the dominant constraint.**
- **Description audit on the 3 missing targets (2026-05-16, post-W80b merge):** Pulled verbatim descriptions from `rtie:vec:OFSERM:<fn>` for the 3 missing functions and the 2 surfacing ones. Vocabulary comparison:
  - **Surfacing targets** carry "non-regulated entity" / "non-regulatory entity" framing prominently — `CAP_CONSL_NON_REGULATORY_ENTITY_*` has 3+ direct mentions including the function name in the description; `ABL_SIGNIFCNT_INVSTMNT_IN_NON_REG_CONSL_ENTITY_DATA_POP` has 5+ mentions plus the `FSI_NON_REG_CONSL_ENTITY_INVST` table.
  - **Missing targets** do not — `SIGNIFICANT_INVESTMENT_IN_PARTY_FOR_REPORTING_BANK_IDENTIFICATION` frames as party-level / reporting bank consolidation (0 direct mentions); `SIGNIFICANT_INVST_THRESHOLD_TREATMENT_DATA_POP` only references `FSI_NON_REG_CONSL_ENTITY_INVST` as a source-table input (2 table-name references, 0 conceptual framing); `SIGNFCNT_INVSTMNT_CAP_DEDUCTION_EXPOSURES` frames as capital-deduction exposure mapping (0 mentions).
  - **The 3 missing descriptions accurately describe what their functions do.** They aren't *about* non-regulated entities — they're **downstream consumers** in a multi-stage chain (target 4 reads `FSI_NON_REG_CONSL_ENTITY_INVST` written by target 2; targets 3 and 5 operate on parties and capital-deduction exposures derived from upstream entity classifications). Cosine similarity is correctly placing them outside the query's "non-regulated entity workflow" semantic zone.
- **Audit conclusion — W80a-targeted-regeneration on these 3 NOT pursued:** Adding "non-regulated entity workflow" framing to the missing 3 would mean adding pipeline-context scaffolding that compromises description honesty (the descriptions would carry semantic claims the functions don't structurally make) and is brittle (helps this canary's query shape, but doesn't help other stakeholder queries about the same chain). The audit gives sharp evidence that pure cosine on description text cannot recover this class of multi-stage-chain query without dishonest scaffolding.
- **Elevated next step — W80c:** Hybrid graph + vector retrieval with rerank. The 3 missing functions are reachable from the 2 surfacing ones via writer → column → reader edges through shared tables (`FCT_PARTY_SHR_HLD_PERCENT`, `FSI_NON_REG_CONSL_ENTITY_INVST`, `FSI_THRESHOLD_TREATMENT`, `FSI_CAP_DEDUCTION_EXPOSURES`). Graph traversal carries chain semantics that pure cosine cannot. Architectural preconditions: W36 Phase 7 (schema-aware retrieval surface) and [W88](#w88-named-regulatory-computation-pre-router--diagnostic-in-flight-2026-05-12) (named-computation pre-router). W80b's expanded candidate set (top_k=20 for VARIABLE_TRACE) is the prerequisite W80c needs — a graph-aware reranker requires a candidate pool to rerank.
- **Merge SHA:** `bb87a25` (merge), `82d52e6` (commit).

---

## W80c. Hybrid graph + vector retrieval rerank — FIXED 2026-05-18 / 2026-05-19 (PR 2 at `211303e`, v2 merge SHA e9ad402)

- **Discovered:** W80b post-merge measurement (2026-05-16, recorded above). Cluster-density expansion alone (top_k=20) did not lift recall above 2 of 5. The W80b description audit demonstrated the 3 missing targets are downstream consumers in a multi-stage chain whose descriptions correctly do NOT match the canary query's "non-regulated entity workflow" semantic zone — pure cosine cannot find them without dishonest description scaffolding.
- **Hypothesis:** Cross-function edges (writer → column → table → reader) already persisted at `graph:full:<schema>` carry chain semantics that cosine doesn't. 1-hop expansion from the top-3 vector hits should surface the 3 missing downstream-consumer targets; an RRF fusion of cosine rank and graph-edge rank should rank them inside top_k+10 without displacing the W80 v1 / W80b strong cosine hits.
- **Stage 1 diagnostic (PR 0, 2026-05-18 — merged at SHA `fe5de15` parent `f43291a`):** [docs/w80c_diagnostic.md](docs/w80c_diagnostic.md) confirmed the premise. 2,249 `CROSS_FUNCTION_TABLE_FLOW` edges in `graph:full:OFSERM` (1.1 MB msgpack); all 5 canary targets in OFSERM (no cross-schema gap to worry about for this canary); every missing target reachable in exactly 1 hop from at least one surfaced target; the {2}→{4} edge alone carries 5 matching columns (load-bearing data flow). Diagnostic also picked rerank weights (α=1.0 matching_columns, β=0.5 seed_reach, γ=0.5 sub_process, δ=0.0 process) and the RRF fusion shape with k=60 (Cormack & Clarke 2009).
- **Stage 2 PR 1 (2026-05-18, merge SHA `fe5de15`):** Helper module [src/agents/graph_rerank.py](src/agents/graph_rerank.py) — `EdgeIndex` (cached per-schema adjacency map over `graph:full:<schema>`), `expand_one_hop`, `score_candidate`, `rerank_with_rrf`. 11 unit tests in [tests/unit/agents/test_graph_rerank.py](tests/unit/agents/test_graph_rerank.py) including a canary-shaped fixture (the diagnostic's Section 2.B 5-target cluster + 3 graph-isolated decoys) that asserts T3, T4, T5 (vector ranks 8, 12, 18) land in top-5 of the reranked slate. Module shipped unused — no production behaviour change at PR 1.
- **Stage 2 PR 2 (wire-in, branch `fix/w80c-graph-rerank-wire-in`):** New module-level helper `apply_w80c_rerank` in [src/main.py](src/main.py) wraps `rerank_with_rrf` with the gate (VARIABLE_TRACE / COLUMN_LOGIC only + `_graph_redis` available + non-empty `search_results`) and a best-effort try/except. Wired at `main.py` between `_run_scoped_vector_search` and `ensure_anchor_in_search_results` so W95's position-0 injection isn't displaced. Stamps `state["graph_rerank_stats"]` (status + seed_count + expanded_count + kept_count + rank_change_count) and propagates them through the meta SSE event as `meta.graph_rerank` so the canary harness reads them directly. Telemetry: `stage_timer("graph_rerank", ...)` for elapsed_ms plus a second `[GRAPH_RERANK_STATS]` INFO line for the dynamic counts (stage_timer can only carry kwargs known at entry). `/v1/query`'s LangGraph path in [src/pipeline/logic_graph.py](src/pipeline/logic_graph.py) is intentionally NOT wired — `_graph_redis` is constructed in lifespan AFTER `compile_graph`; CLAUDE.md documents `/v1/query` as non-canary so the parity gap is acceptable; revisit if `/v1/query` becomes a canary surface.
- **PR 2 retune (per-seed expansion cap):** First wire-in measurement returned 3 of 5 with `expanded_count=137` from the top-3 seeds touching `FCT_ENTITY_INFO` / `DIM_*` tables — the flood pushed T1 (`CAP_CONSL_NON_REGULATORY_ENTITY_SIGNIFICANT_INVESTMENT_IDENTIFICATION`, vector rank 1) below the `keep_top=25` window despite its strong cosine signal. An α retune (`matching_columns` weight 1.0 → 3.0) was tested and produced **bit-for-bit identical output** because RRF fuses INTEGER ranks: linear scaling of a single weight preserves the `graph_score` sort order, leaving `graph_rank` integers unchanged. The real lever was the expansion blast radius itself. Added a per-seed cap to [`expand_one_hop`](src/agents/graph_rerank.py) (default 20): each seed's neighbor list is sorted by `len(matching_columns)` descending (ties stable in edge-list order) and sliced to the cap before contributing to the output. Plumbed through `rerank_with_rrf` (kwarg `per_seed_cap=20`) and the `apply_w80c_rerank` call site. Bounds expansion at ~`3 * cap` pre-dedupe while keeping every load-bearing edge (5-col T2→T4, 3-col T2→T5, 2-col T3→T2, 1-col T1→T2) within reach.
- **Tests:** 6 wire-in unit tests in [tests/unit/test_main_w80c_wire_in.py](tests/unit/test_main_w80c_wire_in.py) cover the four gates + happy-path mutation + exception handling. 1 new test in [tests/unit/agents/test_graph_rerank.py](tests/unit/agents/test_graph_rerank.py) (`test_expand_one_hop_per_seed_cap_keeps_strongest_matching_edges`) verifies the cap keeps strongest-matching-column edges and drops 0-col passthrough. New integration canary `w80c_significant_investment_trace_post_rerank` in [tests/integration/test_live_stream.py](tests/integration/test_live_stream.py) asserts: recall ≥4 of 5 (W80c target — achieved), `meta.graph_rerank.status == "ok"` (gate opened), `rank_change_count > 0` (rerank moved positions — proves the mechanism engaged rather than coasting on vector ranks). The existing W80 v1 and W80b canaries (floor ≥2/5) are preserved as historical baselines so a future regression below the W80 v1 / W80b ceiling fails distinctly from a W80c regression. Total: 1237 unit tests pass.
- **Empirical outcome — HIGH (4 of 5, 2026-05-18):** Canary measurement on PID 12652 (restarted post-retune): `expanded_count=41` (vs 137 pre-cap), `rank_change_count=25`, `kept_count=25`, elapsed ~30-40 ms (vs 152 ms pre-cap). Matched (4): T1 (`CAP_CONSL_NON_REGULATORY_ENTITY_SIGNIFICANT_INVESTMENT_IDENTIFICATION`, reranked rank 9), T2 (`ABL_SIGNIFCNT_INVSTMNT_IN_NON_REG_CONSL_ENTITY_DATA_POP`, rank 4), T4 (`SIGNIFICANT_INVST_THRESHOLD_TREATMENT_DATA_POP`, rank 1), T5 (`SIGNFCNT_INVSTMNT_CAP_DEDUCTION_EXPOSURES`, rank 7). Missed (1): T3 (`SIGNIFICANT_INVESTMENT_IN_PARTY_FOR_REPORTING_BANK_IDENTIFICATION`) — only meaningful graph edge is T3→T2 with 2 matching columns; T3's graph_score (3.0) doesn't beat the close-cosine siblings at reranked ranks 2-8. **+2 net vs W80b's 2/5 baseline.** Net delta vs W80b: T4 and T5 added (the diagnostic Section 2.B's strongest-matching-column predictions, both via T2's outgoing edges with 5 and 3 cols respectively).
- **Regression canaries (verified unchanged post-retune):**
  - **FN_LOAD_OPS_RISK_DATA** — UNVERIFIED + PARTIAL_SOURCE_INDEXED + NAMED_FUNCTION_NOT_RETRIEVED. **Confirmed pre-existing**, NOT W80c-induced: Redis probe shows `graph:OFSMDM:FN_LOAD_OPS_RISK_DATA` (13,444 B), `graph:source:OFSMDM:FN_LOAD_OPS_RISK_DATA` (15,356 B), `graph:meta:OFSMDM:FN_LOAD_OPS_RISK_DATA` (119 B), and `rtie:vec:OFSMDM:FN_LOAD_OPS_RISK_DATA` all present — W49 PARTIAL_SOURCE detector is firing despite source being indexed. Detector-chain quirk triggered by W85 NAMED_FUNCTION_NOT_RETRIEVED; orthogonal to W80c. Loggable as a separate ticket.
  - **N_EOP_BAL** — W87 unrecognized-term short-circuit before retrieval; `graph_rerank={}` confirms rerank didn't run; no regression.
  - **CAP973** — W80c gate correctly closed for FUNCTION_LOGIC; `graph_rerank={"status":"skipped_query_type"}`; UNVERIFIED is from [W96](#w96-llm-fabricates-december-calendar-gate-on-cap-code-regulatory-adjustment-functions--new-2026-05-18) (December calendar fabrication, pre-W80c).
- **W80c-v2 (T3 chase, FIXED 2026-05-19, branch `fix/w80c-v2-t3-chase`):** Task 1 diagnostic on the PR 2 wire-in run found:
  - `expand_one_hop` already walks bidirectionally — `EdgeIndex._build` records every edge twice (`direction='out'` under `from_function`, `direction='in'` under `to_function`); `neighbors(seed)` returns both. Lever D ("bidirectional") was already in place.
  - Live `graph:full:OFSERM` probe of T3's edges contradicted the diagnostic doc Section 2.B: T3→T2 carries **1 matching col** (not 2 as predicted), T3→T5 carries **0 cols** (not 3). The diagnostic doc was corrected in this PR with a one-line note acknowledging predicted vs live counts.
  - Per-seed neighbor probe: T2 has 103 neighbors with 25 candidates at ≥3 matching cols — already exceeds the cap-20 — so T3 (sort-rank 94 in T2's neighbor list) is **cut by cap=20 at T2** and never expands via T2. T3 DOES reach the expansion pool via T1's 0-col tail (sort-rank 7 in T1's 114-neighbor list, of which only 2 have matching_cols ≥ 1 and the rest are 0-col passthrough).
  - With `debug_log_top_n=50` probe live: T3 landed at RRF rank **30** (vector_rank=21 → expansion-only, graph_rank=23, RRF score 0.02439). PR 2's `keep_top=25` cut T3 by 5 ranks.
- **Lever chosen — B (raise `keep_top` from `top_k+10` to `top_k+20`):** Smallest blast radius — doesn't shift any RRF rank, just keeps 10 more candidates. Lever A (bump `seed_reach` 0.5 → 1.0) was the fallback but unused; α-style scaling is sort-order-invariant for the canary fixture (see PR 2 retune note above). Lever C (transitive 2-hop) doesn't apply — T3 reaches T1 and T2 directly. Lever D (bidirectional) was already in place.
- **Empirical outcome — 5 of 5 (2026-05-19):** Canary measurement on PID 5100 with `keep_top = top_k + 20` (35 for COLUMN_LOGIC). All five canary targets matched: T1 (RRF rank 9), T2 (rank 4), T3 (**rank 30** — landed inside the new keep_top=35 with 5 slots to spare), T4 (rank 1), T5 (rank 7). `kept_count=35`, `expanded_count=41` (unchanged from PR 2 — cap=20 still applied), elapsed similar to PR 2. Lever B confirmed sufficient; Lever A not needed.
- **W80c-v2 tests:** Existing `test_main_w80c_wire_in.py::test_variable_trace_invokes_rerank_and_stamps_stats` updated to assert `keep_top=40` (was 30) for VARIABLE_TRACE. Integration canary `w80c_significant_investment_trace_post_rerank` floor lifted ≥4 → ≥5 to lock the achieved value. `debug_log_top_n` kwarg added to `rerank_with_rrf` (default 0 → off; zero runtime cost; available as a future probe surface).
- **Regression canaries after Lever B:**
  - **FN_LOAD_OPS_RISK_DATA** — failure mode CHANGED. Previously UNVERIFIED with `NAMED_FUNCTION_NOT_RETRIEVED` + `PARTIAL_SOURCE_INDEXED` (function absent from `functions_analyzed`). Now UNVERIFIED with `GROUNDING-HIGH` + `GROUNDING-ANCHOR-MISMATCH-HIGH`: the larger keep_top=35 window now includes FN_LOAD_OPS_RISK_DATA in `functions_analyzed` (it lands at rank 30 by vector signal alone), but the LLM's response anchored on `PREV_QTR_CET1_STANDARD_ACCT_HEAD_DATA_POP` (the top-ranked retrieval entry) instead of the W76-anchored target. Same UNVERIFIED badge — not a W80c-v2 regression per se, but a NEW failure surface logged separately as [W97](#w97-w70w76-anchor-block-insufficient-against-top-ranked-retrieval-preference--fixed-2026-05-19-merge-sha-3e9f29a) (FIXED 2026-05-19 by anchor-promote-to-front).
  - **N_EOP_BAL** — W87 short-circuit, unchanged.
  - **CAP973** — W80c gate closed (FUNCTION_LOGIC), unchanged.
- **Scope limits:** No changes to anchor_resolution, vector store, SQL Guardian, computation router, classifier, embedding logic, or frontend. `graph_rerank.py` edits scoped to (1) PR 1 surface, (2) per-seed cap (PR 2), (3) `debug_log_top_n` kwarg (W80c-v2) — all authorized as tuning / instrumentation extensions; the algorithm shape is unchanged. Cross-schema reachability (OFSMDM↔OFSERM edges) remains deferred per diagnostic Q1.
- **Merge SHAs:** `211303e` (PR 2 merge, 2026-05-18); W80c-v2 merge SHA `e9ad402` (2026-05-19).

---

## W80 — Cross-table graph-traversal (original umbrella scope)

- **W80 family structure:** This entry is the original umbrella — cross-table multi-stage VARIABLE_TRACE retrieval via graph-edge traversal — still open. Slices that have shipped or are tracked separately:
  - [W80 v1](#w80-v1-vector-retrieval-embedding-input-poisoning--fixed-2026-05-15-merge-sha-f2945c5) — Vector retrieval embedding input poisoning (FIXED 2026-05-15). Narrow slice: stop poisoning the embedding input with the classifier's enriched blob. Does NOT address graph-traversal retrieval.
  - W80a — Regenerate stunted vector-store descriptions (<500 chars). Logged inside W80 v1's "Out of scope" block.
  - W80b — Hybrid BM25 + KNN retrieval with per-query-type adaptive top-K. Logged inside W80 v1's "Out of scope" block.
  - [W80c](#w80c-hybrid-graph--vector-retrieval-rerank--fixed-2026-05-18--2026-05-19-pr-2-at-211303e-v2-merge-sha-e9ad402) — Hybrid graph + vector retrieval rerank. PR 1 helper merged 2026-05-18 (SHA `fe5de15`); PR 2 wire-in merged 2026-05-18 (SHA `211303e`); W80c-v2 T3 chase (Lever B: keep_top top_k+10 → top_k+20) merged 2026-05-19 → **5 of 5 recall on the canary**.
- **Original scope (Run 8):** ~25% retrieval miss on VARIABLE_TRACE queries. Documented as a known failure surface but framed as a partial coverage gap.
- **Actual scope (stakeholder test 2, 2026-05-14):** Closer to 100% retrieval miss on cross-table multi-stage VARIABLE_TRACE queries. The `N_SIGNIFICANT_INVST_AMT` trace returned 10 functions, 0 matching Cowork's correct 5-function pipeline. Pure name-similarity matching missed every upstream function operating on different table names. Evidence preserved at [scratch/stakeholder_test_2_2026-05-14_chain_ordering.md](scratch/stakeholder_test_2_2026-05-14_chain_ordering.md).
- **Implication for the fix:** Semantic search by name-similarity alone is not sufficient. The umbrella fix must consider graph-edge traversal (writer → column → table → reader) as a signal complementary to semantic search, not a replacement. Multi-stage chains span sub-processes named differently from the target variable; the only reliable retrieval signal across those boundaries is the manifest-anchored graph itself. W80 v1 (embedding-input cleanup) is a prerequisite — it removes one source of noise — but does NOT itself address cross-table chains.
- **Relationship to W89:** Orthogonal. W89 (chain ordering) only guarantees that whatever functions retrieval returned are presented in execution order. The umbrella W80 fix changes which functions retrieval returns. Both must land for stakeholder-style queries like the test_2 trace to produce a Cowork-equivalent answer.

---

## W90. Distributed citation-padding (HIGH tier) — NEW 2026-05-14

- **Discovered:** Stakeholder test 2 (2026-05-14). GROUNDING-LOW fired on "Line 24 cited 4 times" (the W57 padding detector working as designed at LOW tier). But the actual padding pattern was 27 distinct empty-text citations at the same line across multiple SQL blocks — distributed padding at scale, not just same-line repetition.
- **Today:** LOW tier, advisory only, badge stays VERIFIED.
- **Fix needed:** When over a threshold (e.g. 10+ empty-text citations, or all citations point to a single line within a single function), escalate to HIGH and flip badge. The signal is qualitatively the same as W57's same-line repeat detector but operates on the broader count.
- **Priority:** Bundle with W82 (similar surface — both are fabrication-style detectors).

---

## W91. `(SCHEMA)` placeholder leak in markdown — FIXED 2026-05-20 (merge SHA ef8b498)

- **Discovered:** Stakeholder test 2 (2026-05-14). Response heading shows literal `(SCHEMA)` — a template placeholder that wasn't substituted with the actual schema name. Also surfaced in Q9 of the 2026-05-12 stakeholder test, so this is reproducible.
- **Root cause:** `VARIABLE_TRACE_PROMPT` in [src/agents/variable_tracer.py](src/agents/variable_tracer.py) instructs the LLM to `Start with: ## {VARIABLE_NAME} in `FUNCTION_NAME` (SCHEMA)`. The LLM treats `(SCHEMA)` as literal text rather than a variable to fill. The prompt should either pre-substitute the schema or remove the bracketed token.
- **Fix:** Changed the FORMAT line's `(SCHEMA)` token to `{SCHEMA}` and threaded `state["schema"]` through `explain_chain` and `stream_chain` so the substitution happens in Python before `SystemMessage` is built. Used `str.replace("{SCHEMA}", schema_label)` rather than `str.format` because the template also contains `{VARIABLE_NAME}` as an LLM-facing literal that `.format` would `KeyError` on. Empty-schema fallback is the literal `"the schema"`. Five unit tests in `tests/unit/agents/test_w91_schema_placeholder.py` lock down the template invariant, the substitution on both code paths, and the untouched `(OFSMDM)` example block in `UNGROUNDED_IDENTIFIER_PROMPT` (W92 remains out of scope — primary-anchor schema label only).

---

## W92. Response schema-label mismatch — FIXED 2026-05-20 (merge SHA a900cdf)

- **Discovered:** Stakeholder test 2 (2026-05-14). `data.schema: "OFSMDM"` in the response payload, but every table cited in the response body (FSI_NON_REG_CONSL_ENTITY_INVST, etc.) is OFSERM. `schema_searched` correctly lists both schemas; only the single-schema label is wrong.
- **Root cause:** Confirmed the hypothesis. The FUNCTION_LOGIC meta event at [src/main.py:1244](src/main.py#L1244) and the `/v1/query` Renderer output at [src/agents/renderer.py:57](src/agents/renderer.py#L57) both stamp `state.get("schema", "")` — the orchestrator's primary-anchor schema, set by `classify_query` / W76 / BI routing / ALL-mode top-rank fallback. Post-Phase-3 each `multi_source[fn]` entry carries its own `schema` field (set by `MetadataInterpreter.fetch_multi_logic`), so the response builder has the truth available but never aggregates from it. Compounding: the FUNCTION_LOGIC done_payload had no `schema` key at all, so the App.jsx merge at [frontend/src/App.jsx:135-141](frontend/src/App.jsx#L135-L141) (spreads meta into data after finalPayload) made the meta event the sole source of `data.schema`. The W91 heading source had the same shape — `state["schema"]` instead of the W97-promoted multi_source position-0 entry's schema — so the heading and body could disagree even after W91's placeholder fix.
- **Fix:** Architecture B (annotated primary + `cited_schemas`). `data.schema` stays the orchestrator's primary anchor (single string, backward-compatible with the W89 canary driver and any consumer that reads it as a string). New `cited_schemas` list — sorted distinct values of `multi_source[fn]["schema"]` — added to both meta and done events across all three streaming routes (FUNCTION_LOGIC at [src/main.py:1242-1261, 1671-1692](src/main.py), Phase 2 at [src/main.py:1826-1849, 1861-1876](src/main.py), DATA_QUERY at [src/main.py:2209-2222, 2245-2276](src/main.py)) and the `/v1/query` Renderer at [src/agents/renderer.py:54-67](src/agents/renderer.py). The two fields answer different questions — "anchored on" vs "actually talks about" — and can legitimately differ post-Phase-3. FUNCTION_LOGIC done_payload now also carries `schema` so the App.jsx merge becomes a no-op for that field, closing the observability gap where `data.schema` could come from one half of the SSE stream and not the other. The W91 heading source switched from `state["schema"]` to the `w70_anchor`-promoted multi_source position-0 entry's schema at [src/main.py:1581-1601](src/main.py), aligning with W97's promote-to-front contract; a one-line comment at the call site documents this so future readers don't revert it to lowest-score. `_compute_cited_schemas` helper in `main.py` is the only new derivation; `LogicState` gets `cited_schemas: list` at [src/pipeline/state.py](src/pipeline/state.py). `_variable_tracer.stream_chain` / `explain_chain` signatures are unchanged — only the value passed at the call site changes — so the existing W91 unit tests stay verbatim-green.
- **Tests:** 21 new unit tests in [tests/unit/agents/test_w92_schema_label_consistency.py](tests/unit/agents/test_w92_schema_label_consistency.py) cover `_compute_cited_schemas` (empty / single / multi / defensive-skip / determinism), payload shape across FUNCTION_LOGIC / Phase 2 / DATA_QUERY meta+done and the Renderer output, the FUNCTION_LOGIC done_payload `schema` regression (now present), W92 heading selection (5 cases — anchor-present / no-anchor-fallback / phantom-anchor-fallback / case-insensitive / missing-schema-fallback), and an end-to-end W91+W92 test that drives `VariableTracer.stream_chain` with a fake LLM and asserts the rendered system prompt names the anchor's schema (`(OFSERM)`) not the orchestrator default (`(OFSMDM)`). All 5 W91 tests at [tests/unit/agents/test_w91_schema_placeholder.py](tests/unit/agents/test_w91_schema_placeholder.py) pass verbatim. Broader unit suite: 1285 pass, 3 fail in [tests/unit/parsing/test_abl_car_cstm_v4_contract.py](tests/unit/parsing/test_abl_car_cstm_v4_contract.py) — pre-existing manifest task-count assertions against an in-flight Stage 1-3 corpus expansion, unrelated to W92.
- **Manual SSE validation:** Deferred per user direction (live probe was not required to ship Sections 1-3; static path confirmed the bug and the fix). When a cross-schema query lands in the next stakeholder pass, expect `data.schema` to still be the anchored schema, `data.cited_schemas` to list every schema present in `multi_source` (sorted), and the VARIABLE_TRACE heading parenthetical `({SCHEMA})` to match the function being explained, not the orchestrator default.
- **Code SHA:** 4d252fe.
- **Merge SHA:** a900cdf.

---

> **Priority queue note (2026-05-14):** Stakeholder test 2 surfaced W89 (fixed this PR) + W90 + W91 + W92 + W80 scope expansion. Updated priority queue reflects the new tickets. Calibration evidence preserved at [scratch/stakeholder_test_2_2026-05-14_chain_ordering.md](scratch/stakeholder_test_2_2026-05-14_chain_ordering.md).
>
> **[reconciled 2026-05-29: W89/W91/W92 merged; W90 remains]** Status of the items this note listed as "next":
> - W89 — done (merged `334054e`, 2026-05-14)
> - W91 — done (merged `ef8b498`, 2026-05-20)
> - W92 — done (merged `a900cdf`, 2026-05-20)
> - W80 scope expansion — `W80 v1` / `W80b` / `W80c` slices all merged; the cross-table umbrella (`W80`) remains open
> - W90 — still open (no merge found) — the only genuinely-next item

---

## W87. Orchestrator entity-extraction fallback — FIXED 2026-05-15 (merge SHA d311890)

- **Discovered:** Stakeholder test 1 (2026-05-12) Q11 — "what is the threshold value for G Test". Transcript was paste-only context in the chat, not committed to scratch/. RTIE stamped `object_name` with the concatenated enriched-query blob ("what is the threshold value for G Test Find the threshold value used for the G Test check G Test G_T"), passed that to semantic search, anchored on `CS_THRESHOLD_TREATMENT_AGGREGATE_THRESHOLD_ASSIGNMENT`, and fabricated a December gate that W83a caught as UNVERIFIED. Cowork's reference response was an honest "I don't know — please clarify what 'G Test' maps to."
- **Root cause:** [src/agents/orchestrator.py:669](src/agents/orchestrator.py#L669) (`classify_query`) sets `state["object_name"] = enriched_query` where `enriched_query = f"{query} {result.intent} {' '.join(result.search_terms)}"`. When no orchestrator-stage resolver — function-name extraction (W58 filter), W76 named-function anchor, or BI literal-index routing — successfully resolves the query, this concatenated blob is what reaches the embedding call at [src/main.py:1084](src/main.py#L1084). Semantic search then returns name-similar but unrelated functions and the narrative LLM anchors on one of them. The trust-violation chain (semantic search → narrative LLM → W83a fabrication catch) is downstream of this initial fallback.
- **Fix:** New `_detect_unrecognized_term_query` gate at [src/agents/orchestrator.py:1339](src/agents/orchestrator.py#L1339), wired between `apply_bi_routing` and the embedding call at [src/main.py:1018-1064](src/main.py#L1018-L1064). Fires when `query_type ∈ {FUNCTION_LOGIC, COLUMN_LOGIC, VARIABLE_TRACE}` AND `extract_function_candidates(raw_query)` is empty AND `state["bi_routing"]` is absent AND the W76 anchor record has no function AND any classifier-set `target_variable` fails `schemas_for_column` lookup. Builds a deterministic UNVERIFIED clarification body via `build_unrecognized_term_response` ([orchestrator.py:1404](src/agents/orchestrator.py#L1404)) — mirrors W37's `build_function_not_found_response` shape but with `badge="UNVERIFIED"`, `confidence=0.2`, and a `UNRECOGNIZED_TERM: '{term}' not in indexed corpus` warning. Streamed via `_stream_unrecognized_term_response` at [src/main.py:2397](src/main.py#L2397) (stage → meta → tokens → done). W87 is an architectural sibling of W37 (pre-search, deterministic body) — NOT W45/W49 (which are post-retrieval). Term extraction prefers the classifier's `target_variable`, falls back to quoted phrase, then multi-word capitalized run, then longest single capitalized non-stopword token; returns None when no term can be isolated, which falls through to the existing classifier-`partial_flag` clarification path.
- **Tests:** 35 unit tests in [tests/unit/agents/test_w87_unrecognized_term.py](tests/unit/agents/test_w87_unrecognized_term.py) cover the gate (positive G-Test reproduction, target_variable-vs-heuristic priority, VARIABLE_TRACE query type, quoted phrase, unfindable business concept) and negatives (known function, CAP-code BI routing, W76 anchor, empty W76 anchor, resolved column, DATA_QUERY / UNSUPPORTED / VALUE_TRACE / empty query types, mixed-with-known-function, column-check raises). Term-extraction edge cases and variation-generation are pinned. Response-builder shape pinned (badge, validated, confidence, warnings, type, status, requested_term, message vs explanation.markdown sync, honest naming of indices RTIE actually consults). 3 integration tests appended to [tests/integration/test_live_stream.py](tests/integration/test_live_stream.py) — fires on Q11 reproduction, no-fire on FN_LOAD_OPS_RISK_DATA, no-fire on CAP973. All pass against live backend. Manual canaries captured at [scratch/w87_canary_a.txt](scratch/w87_canary_a.txt) / [b.txt](scratch/w87_canary_b.txt) / [c.txt](scratch/w87_canary_c.txt).
- **Merge SHA:** d311890 (2026-05-15).

---

## W95. Anchor-resolved function missing from search results downstream of vector retrieval — FIXED 2026-05-18 (merge SHA 0244f37)

- **Discovered:** Diagnostic pass on CAP973 routing symptom (2026-05-18) after the W36 Phase 5 work was paused — W35 Phases 5/6/7 had already shipped under different branding, so the symptom ("How is CAP973 calculated?" lands on the loader rather than the computer) had to be re-traced against current main. Probe: `graph:literal:OFSERM:CAP973` contains the correct 2 records — `CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT` (role `case_when_target`) and `REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP` (role `in_list_member`). Live `/v1/stream` trace: meta event reports `object_name=CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT` and `schema=OFSERM` (BI routing fired correctly), but `functions_analyzed` lists 10 sibling functions — the computer is absent. Done event: badge `UNVERIFIED`, warning `GROUNDING-HIGH: cited function 'CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT' not in retrieved sources`. The explanation markdown stamps the right title (from the W70 anchor) but the SQL body comes from a sibling `_DATA_POP` loader the vector search ranked above the computer.
- **Root cause:** Anchor resolution (W76 `apply_named_function_anchor`, BI `apply_bi_routing`) stamps `state["object_name"]` and `state["schema"]` and — via `resolve_search_query` — biases the embedding input toward the anchored function. But the embedding input is only a *bias*, not a guarantee: when the anchored function's vector-store description doesn't rank inside the top-K for that input, the source-fetch pipeline at [src/main.py:1162-1166](src/main.py#L1162-L1166) and [src/pipeline/logic_graph.py:178-191](src/pipeline/logic_graph.py#L178-L191) iterates `state["search_results"]` only and never loads the anchored function's body. The explainer is then handed sibling bodies with an anchor block that names a function it cannot see — produces a hallucinated answer that W57 correctly catches as `GROUNDING-HIGH` / `UNVERIFIED`, but the user still sees a wrong-bodied answer with a downgrade badge instead of the correct content with a `VERIFIED` badge. Architectural sibling of [W43](docs/w43_findings.md) (graph pipeline ignoring the routed schema) and W80 v1 (embedding input being the classifier blob): each was a place where the anchor decision failed to propagate one stage further downstream.
- **Architectural principle established:** Anchor resolution must be reflected in downstream **retrieval**, not just **embedding bias**. Embedding bias is best-effort — it nudges the ranking but does not guarantee inclusion. When a deterministic upstream signal (literal-index hit, explicit function name) names the function the user wants, every consumer past that point must see it. Hardening the principle in code rather than relying on the embedding to "usually" cooperate.
- **Fix:** New `ensure_anchor_in_search_results` helper at [src/agents/anchor_resolution.py](src/agents/anchor_resolution.py) (adjacent to `resolve_search_query`, the W80 sibling that biases the embedding input from the same anchor signal). Reads `state["w76_anchor"]["function"]` then `state["bi_routing"]["function"]` (mirrors the `determine_primary_anchor` cascade priority — W76 wins on confidence). When non-empty and not already in `state["search_results"]` (case-insensitive `function_name` match), force-injects a minimal sentinel record at position 0 with `anchor_injected: True` for telemetry. Schema priority: `bi_routing.schema` → `state["schema"]` → empty (the `metadata_interpreter.fetch_multi_logic` Phase 3 resolver looks up the owning schema per-function from Redis regardless, so the hint is non-load-bearing). Wired at two call sites in lockstep — [src/main.py](src/main.py) after `_run_scoped_vector_search`, [src/pipeline/logic_graph.py](src/pipeline/logic_graph.py) inside the `semantic_search` node. The local `results` reference in main.py is refreshed from `state["search_results"]` after injection so the stage-event preview and `fn_names` stay consistent with what `fetch_multi_logic` will iterate.
- **Tests:** 10 unit tests in [tests/unit/agents/test_phase7_bi_routing.py](tests/unit/agents/test_phase7_bi_routing.py) `TestEnsureAnchorInSearchResults` class — BI-routed injection (CAP973 reproduction at unit scale), W76-anchored injection (5-sibling-shadow reproduction), W76 priority over BI when both stamped (matches `determine_primary_anchor`), idempotent when anchor already in results (no duplicate, existing metadata preserved), case-insensitive match, no-op without anchor signal, no-op with W76 alias-fallback-cleared empty function (mechanism 2 path at [orchestrator.py:510](src/agents/orchestrator.py#L510)), inject-into-empty-results edge case, schema fallback chain, chainable return. Two integration canaries appended to [tests/integration/test_live_stream.py](tests/integration/test_live_stream.py): TEST 3 (CAP973 — flipped from `badge != VERIFIED` to assert `VERIFIED` + computer in `functions_analyzed` + no GROUNDING-HIGH warning for the anchored function) and TEST 3b (CAP943 — same plus the Phase 6 derivation banner `**CAP943 = CAP309 - CAP863**` present in markdown, since CAP943's `case_when_target` record carries an embedded derivation).
- **Pre-existing CAP973 failures left untouched:** TEST 11 (W45 structured "Not Found" markdown) and TEST 16 (W49 "W45 branch still wins") assert the W45 ungrounded-identifier response shape for CAP973. They were already failing on current main per the W83C operational observations — CAP973 IS in the literal index, so the W45 branch doesn't fire. W95 does not change that. Resolution belongs to a separate W45/W49 calibration ticket, not this fix.
- **Out of scope:** Integration canary for the W76 round-trip case. Identifying a real query where W76 anchors on a function AND vector search ranks 5 siblings above it would require empirical search through the corpus; the unit test mocks this scenario directly. If a customer-visible W76-anchor-missing-from-retrieval symptom surfaces, the fix is already wired — it just needs a regression canary.
- **Follow-up surfaced during validation:** Both CAP973 and CAP943 traces post-W95 show the W57 `GROUNDING-HIGH` warning has shifted from `cited function not in retrieved sources` (the W95-targeted symptom, now resolved) to `cited source does not support template phrase 'only runs when the reporting month is december'`. The anchor function is correctly at position 0 of `functions_analyzed`; W83a is correctly catching a content fabrication. Logged as [W96](#w96) — the LLM defaults to a December reporting-month framing on CAP-code regulatory adjustment functions even when the cited source contains no calendar gate. Likely an explainer-prompt issue (over-generalized template the LLM learned from sibling functions that DO have December-only gates), NOT a detector gap — W83a is doing its job correctly. Backlog only; does not block W95.
- **Merge SHA:** 0244f37 (2026-05-18).

---

## W96. LLM fabricates December calendar gate on CAP-code regulatory adjustment functions — NEW 2026-05-18

**Status:** Backlog. Surfaced during W95 canary verification (2026-05-18). Not blocking W95 — the W95 architectural fix lands correctly; this is a content-fabrication ticket downstream of retrieval.

**Failure surface.** Both CAP973 (`CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT`) and CAP943 (`CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION`) post-W95 land with badge `UNVERIFIED` and warning `GROUNDING-HIGH: response contains template phrase 'only runs when the reporting month is december' but cited source for '<anchor>' does not support it`. The cited source is now correctly loaded into multi_source (W95 fix), the `functions_analyzed` array leads with the BI-resolved computer (position 0), and W57's "not in retrieved sources" check passes. The remaining `GROUNDING-HIGH` warning is W83a / W83b territory: the explainer narrative includes `"This entire function ONLY runs when the reporting month is December"` even though the actual function body has no such calendar gate.

**Likely root cause.** Explainer prompt issue, not a detector gap. Several CAP-code regulatory adjustment functions in OFSERM (the sibling computers W83a catches) DO have December-only gates (`IF reporting_month = 12 THEN ...`). The LLM is plausibly over-generalizing this pattern — applying the December framing to every CAP-code computer it explains, including ones whose actual source contains no such gate. W83a is correctly detecting the mismatch between explainer output and cited source. The fix is upstream: the explainer system prompt needs either (a) a calendar-grounding instruction ("only describe a calendar gate if the cited source has an explicit `IF month = N` predicate") or (b) a worked example showing that the absence of a calendar predicate means the function runs unconditionally.

**Why not blocking W95.** Architecturally distinct: W95 closes the retrieval gap (anchor → search_results → multi_source). W96 closes a content gap (explainer narrative → cited source consistency). The W95 fix is independently validated — `functions_analyzed[0]` is now the BI-resolved computer for both CAP973 and CAP943, and the `cited function not in retrieved sources` warning is gone. W96 will lift the badge from UNVERIFIED to VERIFIED for these two queries, but that's a separate trust violation.

**Detection signal.** Already firing — the existing W83a / W83b checks at [src/agents/logic_explainer.py](src/agents/logic_explainer.py) catch the December template phrase against the cited source body. No new detector needed.

**Next step.** Audit the explainer system prompt (W57's `SEMANTIC_EXPLANATION_PROMPT` and the W70 anchor block at [src/agents/anchor_resolution.py:153-184](src/agents/anchor_resolution.py#L153-L184)) for any phrasing that biases toward calendar gates. Add a calendar-grounding clause if the audit doesn't surface one. Verify the fix against the same two canaries (TEST 3, TEST 3b in [tests/integration/test_live_stream.py](tests/integration/test_live_stream.py)) — after W96 lands, those tests should clear AND the badge should reach VERIFIED.

---

## W97. W70/W76 anchor block insufficient against top-ranked retrieval preference — FIXED 2026-05-19 (merge SHA 3e9f29a)

**Status:** FIXED 2026-05-19 by promoting the anchored function to `multi_source` position 0 in `src/main.py` after `fetch_multi_logic` (and after W89's `reorder_multi_source`). New helper: `promote_anchor_to_front` in `src/agents/anchor_resolution.py`, sibling to W95's `ensure_anchor_in_search_results`. Surfaced during W80c-v2 canary verification (2026-05-19); not a regression — W80c-v2's wider retrieval window simply exposed an existing prompt-prominence gap.

**Failure surface.** Query `"How does FN_LOAD_OPS_RISK_DATA work?"` on the W80c-v2 build returns badge `UNVERIFIED` with warnings:

* `GROUNDING-HIGH: user asked about 'FN_LOAD_OPS_RISK_DATA' (0 mentions) but response primarily cites 'PREV_QTR_CET1_STANDARD_ACCT_HEAD_DATA_POP' (3 mentions, >2x ratio)`
* `GROUNDING-ANCHOR-MISMATCH-HIGH: response anchors on 'PREV_QTR_CET1_STANDARD_ACCT_HEAD_DATA_POP' but user asked about 'FN_LOAD_OPS_RISK_DATA'`

Pre-W80c-v2 (`keep_top = top_k + 10`), the same query failed differently: `NAMED_FUNCTION_NOT_RETRIEVED` + `PARTIAL_SOURCE_INDEXED` (the function wasn't in `functions_analyzed` at all). Lever B's bigger window (`keep_top = top_k + 20`) now retrieves FN_LOAD_OPS_RISK_DATA at functions_analyzed rank 30, but the LLM explainer drifts to the top-ranked retrieval entry instead of the W76-anchored target.

**Original root-cause hypothesis (partially wrong, corrected below).** The W70 anchor block at [src/agents/anchor_resolution.py:153-184](src/agents/anchor_resolution.py#L153-L184) prepends a `PRIMARY FUNCTION: <fn>` directive to `SEMANTIC_EXPLANATION_PROMPT`. The original hypothesis was that the LLM treats the top retrieval entry as a stronger signal than the anchor directive when the top entry has substantial source content and the anchor's content is sparser.

**Corrected root-cause (post-canary, 2026-05-19).** Live canary verification revealed the hypothesis was *partially* wrong. The LLM is NOT ignoring the anchor directive — it correctly follows whatever `PRIMARY FUNCTION: <fn>` says. The actual failure mode for the FN_LOAD_OPS_RISK_DATA query is **upstream**: the W70 anchor cascade resolves to the WRONG function (`PREV_QTR_CET1_STANDARD_ACCT_HEAD_DATA_POP`, via layer 4 semantic top-1), not to `FN_LOAD_OPS_RISK_DATA`. The query pattern `"How does <FN> work?"` doesn't trigger W76 prefix (no `In <X>, ...` syntax), doesn't populate a clean `object_name` from the classifier, and isn't a BI code — so cascade layers 1-3 all miss and layer 4 picks the strongest vector hit. The LLM then dutifully anchors on the wrong function, and W85 fires `ANCHOR-MISMATCH-HIGH` because the resolved anchor differs from the asked-about function. This cascade-resolution gap is logged as [W98](#w98-anchor-cascade-missing-scan-raw_query-for-known-function-names-layer--new-2026-05-19).

**W97's architectural correctness still holds.** When the cascade resolves the right anchor (BI-routing branch: `CAP973 → CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT`), W97 verifiably promotes it to `multi_source[0]` and the LLM anchors correctly end-to-end. The CAP973 canary [`w97_anchor_promotion_cap973`](tests/integration/test_live_stream.py) locks this contract. W97 closes the prompt-prominence half of the anchor architecture independently of whether the cascade resolution itself is correct.

**Why not blocking W80c-v2.** Architecturally distinct: W80c-v2 closes the retrieval-coverage gap (T3 now surfaces); W97 is an anchor-strength gap (the LLM doesn't honor the W76/W70 anchor block hard enough when retrieval surfaces stronger-evidence alternatives). Both canary states are UNVERIFIED — the badge didn't regress. W97 may have existed pre-W80c-v2 but was masked by the function never reaching retrieval.

**Detection signal.** Already firing — both `GROUNDING-HIGH` (asked vs cited token ratio) and `GROUNDING-ANCHOR-MISMATCH-HIGH` catch the drift. No new detector needed.

**Fix — Lever H4 (anchor-promote, structural):** New helper `promote_anchor_to_front(multi_source, anchor)` in [src/agents/anchor_resolution.py](src/agents/anchor_resolution.py) rebuilds `multi_source` with the anchor's entry first when the anchor IS in the retrieved set but at a non-zero position. Idempotent / no-op for missing anchor, missing entry, or already-at-front. Wired into [src/main.py](src/main.py) after `fetch_multi_logic` and AFTER `reorder_multi_source` (W89) so anchor-first wins over manifest task_order when they disagree — answering the function the user asked about beats showing the chain in execution order.

**Why H4 over H1 (language) / H2 (placement) / H3 (filter):**

* **H1 rejected** — anchor block already directive ("MUST describe THIS function ... other functions are reference material only ... say so explicitly rather than describing a different function"). The directive sits ~3500 tokens before the source pile in the system message; sharpening language doesn't bridge that distance.
* **H2 deferred** — adding a one-line `ANCHOR: {fn}` reminder at the end of the user message (recency-of-instruction) is layered as a follow-on if H4 alone is insufficient. Not needed if the canary lands VERIFIED.
* **H3 rejected** — slicing `multi_source` to anchor + top-N siblings would lose W80c-v2's recall gains and interact with W89's chain-reorder for VARIABLE_TRACE.
* **H4 chosen** — primacy-of-appearance in the source pile, same architectural pattern as W95 (anchor at index 0). Prompt-only effect, no shape change to `multi_source`, no detector change, no retrieval change.

**Architectural principle (recorded by W97):** Anchor resolution must dominate both retrieval coverage AND prompt prominence. W95 closed coverage (anchor IN search_results); W97 closes prominence (anchor at multi_source position 0 → LLM reads its source first). The two together form the complete contract: when a deterministic upstream signal (W76 prefix, BI routing, clean object_name) names the function the user wants, that function must be both retrieved AND placed first in the source pile the LLM reads.

**Tests:** 13 unit tests in [tests/unit/agents/test_w97_promote_anchor.py](tests/unit/agents/test_w97_promote_anchor.py) covering happy path, case-insensitive match, already-at-front no-op, anchor-not-in-multi_source no-op, defensive no-ops (None / empty function / missing key), and idempotency. Integration canary `w97_anchor_promotion_cap973` in [tests/integration/test_live_stream.py](tests/integration/test_live_stream.py) asserts CAP973 → `functions_analyzed[0] == CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT` AND `diagnostic.w70_anchor` matches the front entry. Badge is **not** asserted — CAP973 still trips the unrelated W96 December calendar fabrication. The originally-proposed FN_LOAD canary was reframed to CAP973 because the FN_LOAD query exercises a cascade-resolution gap (W98) orthogonal to W97's prompt-prominence contract.

**Regression canaries (must remain unchanged post-W97):**

* **W80c significant-investment trace** — recall 5/5; rerank still runs; rank_change_count > 0.
* **CAP973 BI-routing** — UNVERIFIED retained (W96 December calendar fabrication still pending); BI routing path still resolves a function and `functions_analyzed` is non-empty.
* **N_EOP_BAL** — W87 unrecognized-term short-circuit still fires; rerank skipped.

**Scope limits:** No changes to detectors (W37/W45/W49/W57/W83a/B/C/W85/W86/W87/W88/W89/W93/W95), vector store, graph_rerank, computation router, classifier, embedding logic, SQL Guardian, or frontend. `apply_w70_anchor` is now called twice per request (once in main.py for W97 promote, once in `stream_semantic` for anchor block) — idempotent because `determine_primary_anchor` is deterministic on a fixed state; the duplicate is the smallest blast radius edit.

**Merge SHA:** 3e9f29a (2026-05-19).

---

## W98. Anchor cascade missing "scan raw_query for known function names" layer — FIXED 2026-05-19 (merge SHA 9553923)

**Status:** Fixed. Surfaced during W97 canary verification (2026-05-19) when the FN_LOAD_OPS_RISK_DATA query exposed that the W70 anchor cascade resolves to the WRONG function for the `"How does <FN> work?"` pattern. Not blocking W97 — W97 closes the prompt-prominence half of the anchor architecture independently of cascade correctness; W98 closes the resolution-input half.

**Failure surface.** Query `"How does FN_LOAD_OPS_RISK_DATA work?"` returns badge `UNVERIFIED` with `GROUNDING-ANCHOR-MISMATCH-HIGH: response anchors on 'PREV_QTR_CET1_STANDARD_ACCT_HEAD_DATA_POP' but user asked about 'FN_LOAD_OPS_RISK_DATA'`. The cascade in [src/agents/anchor_resolution.py:63-150](src/agents/anchor_resolution.py#L63-L150) `determine_primary_anchor` flows through:

* Layer 1 (`w76_anchor.function`) — null (no `In <X>, ...` prefix syntax).
* Layer 2 (clean `object_name`) — not firing (classifier does not emit clean function name for this pattern).
* Layer 3 (`bi_routing.function`) — null (not a CAP code / business identifier).
* Layer 4 (semantic top-1 from `multi_source`) — picks `PREV_QTR_CET1_STANDARD_ACCT_HEAD_DATA_POP` (the top vector hit), which is NOT what the user asked about.

The LLM correctly follows the (wrong) anchor directive; W85's `ANCHOR-MISMATCH-HIGH` detector catches the divergence between cascade-resolved anchor and asked-about function. W97's promote-to-front faithfully promotes the wrong anchor to `multi_source[0]`, which is W97's contract (promote whatever the cascade said) — not a W97 bug.

**Root cause.** The cascade has no layer for "user mentioned a function name in plain text, without W76 prefix or BI code". `_w57_resolve_primary_function` (W76b) and `extract_function_candidates` already exist and scan raw_query for function-name candidates — the cascade just doesn't consult them. A 5th layer (or extending layer 2 to consult raw_query when `object_name` is empty/non-clean) would close the gap.

**Regression sub-finding (architectural principle).** The W84 integration test at [test_live_stream.py:556-590](tests/integration/test_live_stream.py#L556-L590) was passing at its commit time (203e091, 2026-05-12) — but NOT because a `raw_query`-scan cascade layer existed. It passed because the pre-W80 vector-search embedding used the classifier-enriched blob (which contains the literal function name plus restatement noise), and that embedding poisoning pulled `FN_LOAD_OPS_RISK_DATA` to semantic top-1, so Layer 4 happened to resolve correctly by accident. W80 v1 ([f0029a3](https://github.com/anthropics/RTIE/commit/f0029a3)) correctly cleaned the embedding input to `object_name or raw_query`, but that removed an **implicit safety net** the W84 test was silently depending on. The general principle: **removing implicit dependencies on classifier-side noise requires replacing them with explicit cascade layers, not just deleting them**. W98 makes that safety net explicit — a `raw_query`-scan layer in the cascade — rather than relying on retrieval ranking to surface the named function at top-1.

**Fix landed (W98 v1).** New Layer 4 in [`determine_primary_anchor`](src/agents/anchor_resolution.py#L96-L168) between current Layers 3 (bi_routing) and 5 (semantic top-1):
```python
# Layer 4 (W98): scan raw_query for known function names.
raw_query = state.get("raw_query") or ""
if raw_query and multi_source:
    candidates = extract_function_candidates(raw_query)
    if candidates:
        ms_upper_to_actual = {k.upper(): k for k in multi_source.keys()}
        matched = [ms_upper_to_actual[c.upper()] for c in candidates if c.upper() in ms_upper_to_actual]
        if len(matched) == 1:
            return {"function": matched[0], "source": "raw_query_scan", "confidence": "high"}
```
Validates against `multi_source.keys()` (case-insensitive) rather than `function_exists_in_graph` — the cascade runs AFTER `fetch_multi_logic`, so a candidate whose body isn't in multi_source can't be anchored on anyway (`promote_anchor_to_front` would no-op and the explainer would have no source body to describe). Tying the diagnostic stamp to retrieval keeps `w70_anchor` honest. Multi-candidate / zero-survivor cases fall through to Layer 5 semantic top-1 and let W85 ANCHOR-MISMATCH-HIGH catch any drift. No signature change — `determine_primary_anchor(state) -> Optional[Dict]` unchanged.

**W84 test as the spec.** [test_live_stream.py:556-590](tests/integration/test_live_stream.py#L556-L590) `test_w84_diagnostic_single_function` asserts both:
```python
"w70_anchor_resolves_to_asked_function": diag.get("w70_anchor") == "FN_LOAD_OPS_RISK_DATA",
"w76_anchor_null_for_no_prefix_query":  diag.get("w76_anchor") is None,
```
for the query `"How does FN_LOAD_OPS_RISK_DATA work?"`. The `w76_anchor is None` half was load-bearing for the architecture choice — W98 fixes resolution in `determine_primary_anchor` (cascade) rather than in `apply_named_function_anchor` (which would have stamped `w76_anchor` and broken the second half of the test). Post-W98 both assertions pass for the right reason.

[test_live_stream.py:1400-1423](tests/integration/test_live_stream.py#L1400-L1423) `w80_anchored_function_regression` covers the end-to-end outcome on the same query — VERIFIED badge + `FN_LOAD_OPS_RISK_DATA` in `functions_analyzed`. Post-W98 the cascade picks FN_LOAD, W97 promotes it to position 0, the explainer anchors correctly.

**Detection signal.** `GROUNDING-ANCHOR-MISMATCH-HIGH` (W85) catches cascade-anchor vs asked-function divergence — pre-W98 this was the only thing keeping the bad answer from being labelled VERIFIED. Post-W98 the detector keeps firing as a defense-in-depth backstop but the cascade no longer produces the mismatch in the first place.

**Scope.**

* Changed: [src/agents/anchor_resolution.py](src/agents/anchor_resolution.py) (new Layer 4 + docstring renumbering), [tests/unit/agents/test_w70_anchor_injection.py](tests/unit/agents/test_w70_anchor_injection.py) (cascade test additions). The two pre-existing live tests (W84, W80-anchored-function-regression) flipped from failing → passing without modification — they were the W98 spec.
* Not changed: any detector, vector store, retrieval / rerank, W76 detect_named_function_anchor, W95 ensure_anchor_in_search_results, W97 promote_anchor_to_front, classifier prompt, embedding logic, signature of `determine_primary_anchor` / `apply_w70_anchor`.

**Priority.** Medium → Done. W97 had already reduced the FN_LOAD-style failure mode's blast radius (anchor still wrong, but at least position-0 and anchor agreed so the LLM didn't drift further); W98 makes the anchor right in the first place.

---

## W93b. `cli.py index` should default to loader-validated path — FIXED 2026-05-18 (merge SHA 2c8553f)

- **Discovered:** W93 verification run (2026-05-16). Running `python cli.py index --force` to re-attempt the four sentinel docs called `IndexerAgent.index_all_modules` → `index_module("ABL_CAR_CSTM_V4", force=False)`, which walks `db/modules/*` on disk and indexes every `.sql` file regardless of whether the loader accepted it. The OFSERM vector-store corpus jumped 178 → 281 docs mid-run. Cleanup required deleting 116 `rtie:vec:OFSERM:<fn>` docs that had no corresponding `graph:OFSERM:<fn>` backing.
- **Root cause:** [cli.py:42-68](cli.py#L42-L68) `cmd_index` called `index_all_modules` unconditionally. That's the **disk-walking** path — it embeds the raw file set under `db/modules/`, including functions the loader rejected. The Phase-3 path `index_all_loaded` at [src/agents/indexer.py:302](src/agents/indexer.py#L302) iterates `graph:<schema>:<fn>` keys directly and matches what the rest of RTIE serves answers from. The lifespan was already using it ([src/main.py:562-578](src/main.py#L562-L578)); the CLI was the one stale call site.
- **Fix:** Default switched to `index_all_loaded`. `cmd_index(from_disk=False, force=...)` builds a sync `redis.Redis` client (same shape the lifespan uses) and passes it through, then prints the per-schema summary line (`Auto-index <schema>: N indexed, N skipped, N errors`) that mirrors the lifespan log. `--from-disk` preserved as an opt-in escape hatch for rebuilds outside the loader's view, with a warning in the help text. Help is the module docstring (no argparse rewrite); `--help` / `-h` short-circuits to print it without running the indexer. When `index_all_loaded` returns zero schemas (loader not yet run), the CLI prints actionable guidance — start the backend once or use `--from-disk` — instead of silently exiting with no work done.
- **Tests:** 9 unit tests in [tests/unit/test_cli_index_surface.py](tests/unit/test_cli_index_surface.py) cover (a) docstring surface (mentions `--from-disk`, loader prerequisite, default-is-safe framing) and (b) arg parsing routing (`index` → default, `--force` keeps default, `--from-disk` flips, both compose, `--help` short-circuits before constructing clients, bare invocation prints doc). `cmd_index`'s Redis / IndexerAgent body is intentionally not exercised in unit tests — that surface belongs to manual smoke and the boot-time auto-index path the lifespan already covers.
- **Out of scope:** The `/index-module` / `/index-all` admin slash commands at [src/main.py:2606-2609](src/main.py#L2606-L2609) still call the disk-walk methods. Left alone deliberately — those are explicit-name handlers that callers invoke when they want disk-walk semantics; not the same ergonomic footgun the bare `cli.py index` was. Cold-start ergonomic (user runs `python cli.py index` on fresh Redis, sees "no schemas discovered" message) is documented in the help and prints actionable guidance but is not auto-handled — wiring the loader into the CLI is logged as [W93c](w93c_cli_cold_start_loader_invocation.md), not urgent.
- **Merge SHA:** 2c8553f (2026-05-18).

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

---

## Architectural corrections — surfaced during the ABL_CAR_CSTM_V4 extraction passes (2026-05-19)

Two facts about the existing extraction pipeline that contradicted assumptions baked into prompts. Recording so future extraction or indexing work uses the right source and accounts for the existing-file heterogeneity.

### T2T resolved SQL bodies live in LOAD DATA, not Transform Data

The intuitive read of OFSAA's three-log split is that "Transform Data" holds the resolved T2T transformations. It doesn't. T2T resolved bodies (the `INSERT INTO target(...) SELECT /*+PARALLEL (4)*/ ... LOG ERRORS INTO target$ ('jobid') REJECT LIMIT ...` statements that the 190 SELECT-form `.sql` files were extracted from) live in `ofserminfo_logs/LOAD DATA/*_T2TCPP.log`. The trio per task is:

- `LOAD DATA_<jobid>_REVLOADER.log` — task name in `RevLoader : Parameters : OFSERMINFO,Table To Table,<SRC>,<TASK_NAME>,…`
- `LOAD DATA_<jobid>_T2T.log` — source/target metadata only
- `LOAD DATA_<jobid>_T2TCPP.log` — resolved SQL body (Pattern A: single-line `INSERT … SELECT …`; Pattern B: parameterised `VALUES(:c1,:c2,…)` INSERT plus separate `directLoad, Select Query : SELECT …` line that must be concatenated to reconstruct the full statement)

`ofserminfo_logs/TRANSFORM DATA/*` is for CSTM_DT stored-procedure invocations only. Those logs show `BEGIN :D := Dt_<NAME>_<n>(…)` and the return code — the procedure *body* is never echoed; it lives in Oracle's `ALL_SOURCE` / `DBA_SOURCE` against the OFSERM schema. CSTM_DT extraction therefore needs an Oracle query, not log parsing (tracked as W102).

`ofserminfo_logs/RULE_EXECUTION/*` is TYPE3 rules and out of scope for code-body extraction — RTIE doesn't index rule bodies via this pipeline.

### The 372 existing `functions/*.sql` are not all the same shape

Convention split as of 2026-05-19 (pre-Stage 1 addition):

- **190 files** = SELECT-form T2T inserts extracted from T2TCPP logs (matches the Section 2 pattern above). `REJECT LIMIT 0` is the dominant tail clause.
- **182 files** = TYPE3-style rule SQL — typically `UPDATE … SET … CASE WHEN …` for risk-weight / capital classification rules. These were NOT extracted from logs in the original pass; their source is Oracle metadata (likely `RM_RULES` or the OFSAA rule-definition tables) or a runchart-driven generator that pre-dates this extraction pipeline. They share the same 35-line wrapper template as the 190 T2T files, which masks the source-difference.

Practical consequences:

- Anyone writing future extraction logic should not assume all 372 came from logs. Re-extracting "everything" from logs will silently miss 182 rule files.
- Tooling that keys on `LOG ERRORS INTO … REJECT LIMIT` (e.g., a parser that infers the target table from the error-log clause) will only work on the 190 T2T files. The 182 rule files have no such clause.
- The Stage 1 additions on 2026-05-19 (11 reconstructed Pattern B files) match the SELECT-form 190-file convention. Easy to identify them by `Wrapped: 2026-05-19` versus the prior pass's `Wrapped: 2026-04-23`.

These corrections override the assumption in the original Stage 1 prompt that "Transform Data log (← T2T resolved SQL bodies live here)" and the implicit assumption that the 372 files share one extraction provenance. Both were caught during execution; reflect them in any future prompt that touches `ABL_CAR_CSTM_V4/functions/`.

---

## W107. Loader doesn't gate on `task.active`; missing inactive source files crash the indexer — FIXED 2026-05-20

**Status:** Implemented and merged on `fix/w107-loader-active-gate`. Live in the per-file parse loop in [src/parsing/loader.py](src/parsing/loader.py).

**Architectural principle.** When the manifest validator and the loader both make decisions about active/inactive entries, they must encode the *same* rule. The two layers see the same manifest objects; drift between their interpretations is a hidden, time-delayed bug — the manifest passes validation under one rule and then crashes under the other, exactly the W107 failure mode.

**Failure surface.** Today's post-restart indexer run (2026-05-20 15:02:45) reported:

```
Module ABL_CAR_CSTM_V4: loaded 0, skipped 324, failed 23 (status=partial)
```

All 23 failures were `FileNotFoundError` traces from [loader.py:339](src/parsing/loader.py) opening `.sql` files that the manifest listed but were not on disk. Diagnostic confirmed all 23 manifest entries carried `active: false` with a coherent `inactive_reason: removed_from_batch_per_run_chart`. The manifest was internally consistent — these were correctly authored inactive entries.

Why the inconsistency surfaced now: W101 (the validator relaxation that landed earlier today) and W104 (manifest naming normalization) let inactive entries keep their populated `source_file:` fields without complaint. The loader was never re-taught to gate on `active`, so it inherited an outdated invariant — *"inactive ⇒ `source_file` is empty (because the on-disk SQL was removed alongside)"* — that the new authoring workflow doesn't satisfy. The 23 entries are inactive with a populated `source_file:` that points at a file the corpus no longer carries.

The manifest validator at [manifest.py:_validate_task line 716](src/parsing/manifest.py#L716) already encoded the correct rule:

```python
# Inactive tasks are retained in the manifest for audit (via
# inactive_reason) but not executed. If the on-disk SQL was removed when
# the task was dropped, skip file validation — there is nothing to parse.
if not task.active or not task.source_file:
    return
```

The loader's parse loop did not mirror it.

**Why a narrower fix than the obvious one.** The first instinct was to swap [loader.py:278](src/parsing/loader.py)'s `iter_all_tasks()` → `iter_active_tasks()` (or add `or not task.active` to the filter). That would remove inactive functions from the graph corpus entirely. Several downstream sites depend on those graphs:

1. **[logic_explainer.py:2935-2942](src/agents/logic_explainer.py)** — when a user asks about an inactive function, prepends a "_Note: This task is marked inactive…_" header and explains what the function *would* do. Requires the graph to exist.
2. **[query_engine.py:158-179](src/parsing/query_engine.py)** — `resolve_variable_nodes(include_inactive=True)` is an opt-in mode that traverses inactive nodes too. Requires them to be indexed.
3. **[query_engine.py:_is_inactive_node](src/parsing/query_engine.py)** — filters by `hierarchy.active == False`. Built on the assumption that inactive nodes are *present and filtered out by default*, not absent.
4. **[test_loader_manifest.py:213-215](tests/unit/parsing/test_loader_manifest.py)** — explicit assertion: `# Inactive task's graph is still built, but flagged.`

The Option-B fix would have broken all four. The diagnostic-first stop pattern caught it before any code was written — the first audit (callers of `iter_all_tasks()` at the manifest API) cleared, but the second audit (callers of inactive *graphs* in Redis) tripped the STOP.

**Fix (Option C).** Pre-flight `os.path.isfile()` check inside the per-file parse loop, before `open()`:

```python
# Mirror the manifest validator's rule (manifest.py:_validate_task line 716):
# an inactive task may keep its source_file populated for audit even after
# the on-disk .sql has been removed from the batch. Skip those quietly here
# so the indexer reports them under `skipped` rather than `failed`.
if manifest is not None and not os.path.isfile(sql_file):
    task = manifest.get_task_by_file(sql_file)
    if task is not None and not task.active:
        skipped_count += 1
        logger.info(
            "Skipped (inactive, source file absent) %s — manifest "
            "lists %s but no .sql on disk",
            func_name, os.path.basename(sql_file),
        )
        continue
```

Active tasks with missing files still fall through to the existing failure path. In practice they don't even reach the loader: `load_manifest` runs `_validate_task` at load time, which raises `ManifestValidationError` for active-with-missing-file. The pre-flight skip is a belt-and-suspenders defense for any future scenario where validation gets bypassed (race condition between manifest load and parse; programmatic `BatchManifest` construction that skips the validator).

**Tests** ([tests/unit/parsing/test_loader_manifest.py](tests/unit/parsing/test_loader_manifest.py)):

- `test_w107_inactive_task_with_missing_source_file_is_skipped_quietly` — manifest with one active+present and one inactive+missing entry. Asserts `functions_failed == 0`, the missing entry counts under `functions_skipped`, the active entry parses normally, and the diagnostic log line `"Skipped (inactive, source file absent)"` is emitted.
- `test_w107_active_task_with_missing_source_file_still_fails_loud` — active task whose file is deleted between manifest authoring and load. Asserts `ManifestValidationError` is raised by `load_all_functions` (validator catches it first, the whole load aborts).
- `test_module_with_manifest_annotates_graph_with_hierarchy` (pre-existing, preserved) — inactive task whose file *does* exist still parses and produces a graph with `hierarchy.active is False`. This is the downstream-graph capability the simpler fix would have broken; the pre-flight check leaves it intact.

All 7 tests in `test_loader_manifest.py` pass. The pre-existing 3 failures in `test_abl_car_cstm_v4_contract.py` (`test_iter_*_tasks_count`) are stale pinned counts invalidated by yesterday's b68918a corpus expansion (expects 203/166/37, actual 460/323/137); unrelated to W107 and unchanged by this branch.

**Expected post-restart behavior.** Next `python run.py` should report `failed=0` and the 23 previously-failing inactive entries should appear under `skipped` (the count of total skipped rises by ~23 from yesterday's baseline — they were not previously contributing to either bucket because they crashed on `open()`).

---

## W106. ABL_CAR_CSTM_V4 contract-test pinned counts stale after corpus expansion — FIXED 2026-05-20

**Status:** Constant rebaseline on `fix/w106-contract-test-rebaseline`. Updated three module-level constants in [tests/unit/parsing/test_abl_car_cstm_v4_contract.py](tests/unit/parsing/test_abl_car_cstm_v4_contract.py).

**Failure surface.** Three tests in `test_abl_car_cstm_v4_contract.py` (`test_iter_all_tasks_count`, `test_iter_active_tasks_count`, `test_iter_inactive_tasks_count`) failed against the current `ABL_CAR_CSTM_V4/manifest.yaml`:

| Constant | Pinned value | Actual |
|---|---:|---:|
| `EXPECTED_TOTAL` | 203 | 460 |
| `EXPECTED_ACTIVE` | 166 | 323 |
| `EXPECTED_INACTIVE` | 37 | 137 |

**Root cause.** Yesterday's b68918a (Stage 1-3 corpus expansion: 372 → 402 `.sql` files, +30 net-new active functions) widened the manifest's task coverage across multiple processes but didn't update these contract pins in the same commit. The file header explicitly states this is the workflow — *"If the real manifest legitimately changes, update the constants here in the same commit so the new contract is explicit"* — so the pinned drift was a missed step in b68918a, not a manifest authoring bug. The tripwire fired exactly as designed; it just hadn't been re-armed.

**Fix.** Rebaseline the three constants to the post-expansion counts (`460/323/137`). The contract guard (`test_flat_process_tasks_visible`) that proved its worth in the original `_walk_tasks` regression — where process-level flat tasks were silently dropped — remains intact and unchanged. No production-code logic touched.

The comment block above the constants was rewritten to:
- record W106 as the rebaseline event with the new counts
- explain that the growth landed across multiple processes (so no per-container enumeration of the delta is attempted)
- re-affirm the "update constants in the same commit as the manifest change" workflow

**Tests.** All 4 tests in `test_abl_car_cstm_v4_contract.py` pass.

**Out-of-scope.** Tighter coupling between the manifest and the contract pin (e.g., a pre-commit hook that recounts on `manifest.yaml` change and rejects mismatched pins) would prevent the next drift. Not implemented here — workflow discipline is the current control, and W106's surfacing during the W107 close-out shows the existing safety net (CI + the file header guidance) does catch the drift quickly. Worth re-evaluating if a third rebaseline lands in the next quarter.

---

## W108. Explainer multi-source concat exceeds LLM context after corpus expansion — FIXED 2026-05-20

**Status:** Implemented and merged on `fix/w108-multi-source-token-cap`. Live in `LogicExplainer._build_capped_concat_sections` and `stream_semantic` in [src/agents/logic_explainer.py](src/agents/logic_explainer.py).

**Architectural principle.** Prompt-size budgeting at the LLM call site cannot live solely in retrieval. Retrieval can cap function count and graph_rerank can cap node count, but the *source-body* size of each function is unbounded — a corpus expansion that adds large functions to the retrieval surface can blow the prompt past the model's context window even when the function count is stable. The explainer is the boundary between "data we retrieved" and "data the model can ingest"; it owns the final cap.

**Failure surface.** Post-corpus-expansion canary run (2026-05-20 16:52:44, after b68918a Stage 1-3 added 30 net-new active functions and refreshed several Stage 3 raw-`FUNCTION` source bodies):

```
correlation_id: 40553ec0-15b8-4fee-8bff-f37786d2fc0b
query:          "How does FN_LOAD_OPS_RISK_DATA work?"
stage:          stream_semantic (raw-source fallback)
LLM:            openai gpt-4o-mini
result:         openai.BadRequestError: Error code: 400 -
                "This model's maximum context length is 128000 tokens.
                 However, your messages resulted in 134652 tokens.
                 Please reduce the length of the messages."
```

Yesterday's W98 canary baseline (2026-05-19 12:48) had Canary A passing with badge=VERIFIED on the same 35-function retrieval surface; today the same surface produced a 134,652-token prompt (+5% over the gpt-4o-mini 128K limit) and the explainer surfaced DECLINED with `type=llm_api_error`. The W70 anchor cascade fired correctly (`apply_w70_anchor: anchored on FN_LOAD_OPS_RISK_DATA, confidence=high`) and the W97 promote-anchor-to-front moved it from position 29 to 0 as designed — both upstream of the OpenAI 400. The regression was confined to the explainer's concat-and-send path.

Attribution: corpus-expansion side effect from b68918a, not introduced by W107 or W106 (loader/test only). Every broad "How does X work?" query that pulled a large multi_source was bricked. The same retrieval surface also affects Stage 3 sample queries (`ABL_MKT_RISK_GENRSK_IR`, `FN_STRESS_DATALOAD_CSTM`, `FN_G_TEST_CSTM`) and any other COLUMN_LOGIC query that falls through to the raw-source path (cf. W109 — the orchestrator's question-text-as-target_variable bug causes the graph-resolve path to return 0 nodes, forcing this fallback for almost all explainer queries).

**Fix (Path 1 — token-budget cap).** Defensive char-budget cap on the multi_source concatenation, applied just before the prompt is built. Module-level constant `SOURCE_CONCAT_CHAR_BUDGET = 400_000` (≈100K tokens at the ~4 chars/token ratio observed in the failing prompt — 134,652 tokens for ~540K chars). Headroom budget for the rest of the request:

| Component | Tokens |
|---|---:|
| System prompt + W70 anchor block | ~2,000 |
| Raw-source concat (capped) | ≤100,000 |
| User query | ~200 |
| Response (`max_tokens=4096`) | 4,096 |
| **Total** | **~106,300** |

Comfortably under the 128K limit with ~21K tokens of headroom for tokenizer drift across PL/SQL idioms.

Helper signature:

```python
def _build_capped_concat_sections(
    self,
    multi_source: dict,
    char_budget: int = SOURCE_CONCAT_CHAR_BUDGET,
) -> tuple[list[str], int, list[str], int]:
    """Returns (sections, kept_count, dropped_names, total_chars)."""
```

Iterates `multi_source` in dict-order (which `promote_anchor_to_front` has set to anchor-first per W97). Accumulates sections until the next section would push past `char_budget`, then drops the rest. **Position 0 is exempt** — the anchor section is always retained even when it alone exceeds the budget, because an explainer response without the anchor is functionally useless. When the cap fires, `stream_semantic` emits a `logger.warning` with `kept_count`, total chars, budget, dropped count, and the first five dropped names — visibility for monitoring without polluting INFO-level logs.

**Tests** ([tests/unit/agents/test_w108_token_cap.py](tests/unit/agents/test_w108_token_cap.py)):

- `TestNoCapNeeded::test_small_multi_source_passes_through_unchanged` — 5 small functions, total well under budget → no drops, no warning, original order preserved.
- `TestNoCapNeeded::test_empty_multi_source_returns_empty_lists` — defensive edge case.
- `TestCapFires::test_lower_ranked_dropped_when_budget_exceeded` — 10 functions, 20K-char budget → partial keep, dropped tail in order.
- `TestCapFires::test_anchor_preserved_even_when_its_own_section_exceeds_budget` — W97 contract: position 0 survives even at oversized; followers all drop.
- `TestCapFires::test_cap_default_uses_module_constant` — default arg matches `SOURCE_CONCAT_CHAR_BUDGET`; documents the public-default contract.
- `TestBudgetBoundary::test_follower_fitting_within_budget_is_kept` — strict-inequality boundary pin (catches future `>` ↔ `>=` flips).

All 6 W108 tests pass. 166 adjacent tests in the explainer suites (`test_w70_anchor_injection.py`, `test_w97_promote_anchor.py`, `test_w91_schema_placeholder.py`, `test_w92_schema_label_consistency.py`, `test_grounding.py`, `test_w57_grounding.py`) pass unchanged.

**Expected post-restart canary behavior.** Canary A ("How does FN_LOAD_OPS_RISK_DATA work?") should return badge=VERIFIED with the W70 anchor surfacing in `diag.w70_anchor`. The `app.log` should show one new line when the cap fires:

```
stream_semantic: W108 source-concat cap fired — kept N of 35 functions
  (X chars, budget 400000), dropped M lower-ranked (first: …)
```

`N` will land around 25-28 once the largest Stage 3 raw-FUNCTION bodies are dropped. The retained functions preserve the W97 anchor at position 0 and the highest-scored followers, which is the relevant context for explaining what FN_LOAD_OPS_RISK_DATA does.

**Out-of-scope (deliberately).** Three deeper fixes remain on the backlog and were considered but not pursued today:

1. **Swap the explainer LLM to a 200K-context model** (claude-3-5-sonnet, gpt-4-turbo). Would obviate the cap for current corpus growth but the cap is still a good defensive boundary regardless; chose Path 1 alone for minimal blast radius. Track as a follow-up if the cap starts firing on >50% of queries.
2. **Fix W109** (see entry below) — orchestrator passes question text as `target_variable`, forcing nearly every explainer query through the raw-source concat path even when the graph path would work. Real architectural fix; intentionally deferred — Path 1 unblocks canaries without touching orchestrator semantics.
3. **Real-tokenizer budgeting** (use the model's tokenizer to count exact tokens instead of the 4 chars/token heuristic). Would let the cap operate closer to the actual limit. Not pursued because (a) the chars heuristic has known ~25-30% conservative slack already built in to the 400K budget, and (b) adding tiktoken or a model-specific tokenizer dependency to the explainer hot path raises latency. Re-evaluate if cap-fired warnings show false-positive drops on small-but-information-dense corpora.

---

## W109. Orchestrator passes question text as `target_variable` to graph-resolve — PLANNED

**Status:** Logged; not scheduled.

**Surface.** When an explainer query names a specific function (e.g. "How does FN_LOAD_OPS_RISK_DATA work?"), the orchestrator currently passes the full question text as `target_variable` to `resolve_query_to_nodes`. The W43 diagnostic instrumentation shows this verbatim:

```
[W43_DIAG] stage=resolve_query_to_nodes_entry query_type='variable'
  qt='variable' target_variable='How does FN_LOAD_OPS_RISK_DATA work?'
  function_name=None table_name=None schema='OFSMDM'
[W43_DIAG] stage=graph_resolve_nodes_result node_ids_count=0
  fallback_triggered=True
Graph returned no nodes, falling back to raw source for query: …
```

The resolver cannot match the question prose against a column or alias, returns 0 nodes, and the pipeline falls through to `stream_semantic`'s raw-source concatenation path — which is where W108 lives. So today nearly every explainer query takes the broad-multi-source path; W108's cap is what keeps that path safe.

**Correct shape.** For explainer queries that name a specific function, the orchestrator should pass `function_name=FN_LOAD_OPS_RISK_DATA, target_variable=None` so the graph-resolve path can return the anchor function's nodes directly. That avoids the broad multi_source concat in the first place and means W108's cap should rarely fire in normal operation.

**Why not today.** Touches orchestrator routing + query_engine entry contracts + the downstream `LogicState` field semantics. Risk of regressing the existing W43 diagnostic and the W76 prefix-anchor cascade. The W108 cap is a sufficient unblocker for the canary battery and the broader Stage 3 verification work; W109 is a quality improvement, not a correctness fix.

**Backlog gate.** Re-prioritise W109 if either:

- the W108 cap-fired warning starts appearing on >50% of explainer queries in `app.log` (signal: the corpus has grown enough that even the cap is dropping useful context), or
- a future prompt requires deeper graph-pipeline reasoning that today's raw-source fallback can't deliver.

---

## Backlog — informal observation (not ticketed yet)

**Trace N_EOP_BAL retrieves wrong functions despite N_EOP_BAL being indexed.** Surfaced during the 2026-05-20 W107 post-restart validation pass.

Redis probe (`graph:index:OFSERM`, 2,737 columns total) shows N_EOP_BAL is correctly indexed against 4 functions:

```
- ABL_BANKING_BILLS_EXPOSURE_DATA_CREATION:..._N1
- ABL_BANKING_LC_EXPOSURE_DATA_CREATION:..._N1
- BANKING_OD_EXPOSURE_DATA_CREATION:..._N1
- ABL_BANKING_LOAN_EXPOSURE_DATA_CREATION:..._N1
```

Live `/v1/stream` on "Trace N_EOP_BAL" retrieves a 5-function multi_source consisting of the unrelated significant-investment cluster (ABL_INSIGNFCNT_INVSTMNT_*, CAP_CONSL_*, ABL_LEV_RATIO) — none of the 4 actual N_EOP_BAL-bearing functions are pulled. The LLM still cites the correct functions (ABL_BANKING_BILLS_EXPOSURE_DATA_CREATION and ABL_BANKING_LC_EXPOSURE_DATA_CREATION appear in its response) — likely from the W70 anchor cascade or general knowledge — but they're not in `multi_source`, so the W57 grounding overlay correctly flags them as GROUNDING-HIGH.

This is a retrieval-side coverage gap, latent and pre-existing (W43 instrumentation has been logging the question-as-target_variable pattern for a while; cf. W109). Worth investigating eventually — likely a query-engine path that prefers embedding-similarity over column-index direct-lookup for VARIABLE_TRACE on widely-used columns — but not today.

---

## Dedup-chain weakness — sibling check defers to earlier check via dedup contract

**Pattern, not a single weakness.** RTIE's W57 post-generation grounding overlay has at least three sites where a later check defers to an earlier check via a dedup contract:

  - **W83a → Check 5** ([src/agents/logic_explainer.py:2275-2276](src/agents/logic_explainer.py#L2275-L2276)) — `_w57_check_december_paraphrase` skips when any literal phrase from `_W57_CHECK5_DECEMBER_LITERAL_PHRASES` is present in the body. The intent: Check 5 names the exact literal phrase, which is more informative than W83a's paraphrase warning.
  - **W83b → Check 5 OR W83a** ([src/agents/logic_explainer.py:2363-2369](src/agents/logic_explainer.py#L2363-L2369)) — `_w57_check_calendar_gating_grounded` skips when either a Check 5 literal phrase OR a W83a paraphrase pattern matches the body. The intent: a narrower-shape check already covered the claim.
  - **`seen_cited_fn` dedup** ([src/agents/logic_explainer.py:1778-1811](src/agents/logic_explainer.py#L1778-L1811)) — Check 1's three sub-checks (1.0a prose framing, 1.0b heading framing, 1.0b responsibility framing) share a `seen_cited_fn` set so the same fabricated function name cited in multiple framings fires exactly one warning. The intent: avoid trust-banner noise when the same fabrication appears in heading AND prose.

**Rule the pattern implies (and why future detector work must respect it).** A sibling check that defers to an earlier check **inherits that earlier check's bugs invisibly**. If the earlier check's predicate becomes lenient or broken — too narrow a phrase set, a validator that returns True-supported when it shouldn't, an over-eager `seen_cited_fn` add — the deferring sibling silently skips on the same body and the trust-contract signal disappears entirely.

W137 was exactly this failure mode. Pre-W137 Check 5's December-literal lambda used a lenient substring predicate (`"EXTRACT(MONTH" in src OR "TO_CHAR" in src) AND "12" in src`). `TO_CHAR` appears in nearly every OFSAA function (skey conversion in INSERTs); the literal `"12"` appears in arithmetic (`365/12`), stage counters (`LV_STAGE := 12`), and constants. The predicate returned True-supported for most of the corpus. Because both W83a and W83b dedup to Check 5 when a literal December phrase is in the body, a False-positive supported signal at Check 5 silently suppressed both downstream checks. P1 query B4 surfaced the result: response asserted December gating, no calendar warning fired, badge VERIFIED on a fabrication. W137 replaced Check 5's predicate with the strict `_w57_calendar_gate_supports_claim` gate.

**Constraint for new detector work.** A new sibling check that follows the dedup-chain pattern must **independently verify the earlier check's predicate against the same body**, not just rely on its dedup contract. Two acceptable shapes:

1. The new check re-runs the earlier check's predicate on the body itself, and only defers when the earlier check would have fired correctly — i.e., predicate True AND the earlier check's source-content gate confirmed.
2. The new check does not dedup against the earlier check at all (fires independently), and dedup is handled at the enforce-grounding layer via the existing set-based exact-message dedup.

The current sites (W83a→Check 5, W83b→Check 5/W83a) take option 1's "defer to narrower check" stance — acceptable as long as the narrower check's predicate stays strict. W137 hardened Check 5's predicate accordingly. Future calendar-class detectors that want to defer to Check 5 must keep this invariant alive.

**Out of scope.** `seen_cited_fn` dedup is a different shape (same-message dedup across structurally-similar sub-checks within Check 1, not predicate-deferring across distinct detectors), but is listed above because the failure mode is structurally similar — a fabrication added to the seen set on one path skips the other paths regardless of whether they would have produced a more informative warning. Worth a separate audit if the seen-set construction proves load-bearing for a future fabrication class.
