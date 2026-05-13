# RTIE Weakness Log

Catalog of known weaknesses in RTIE's grounding/routing layers, with
discovery context. Entries are append-only; closed weaknesses keep
their entry with a status update rather than being deleted.

Numbering matches the W-ticket convention used in branches, code
comments, and PR titles (`refactor/w35-…`, `fix/w83b-…`, etc.).

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

## W83C. December-gating overgeneralization — Deferred from W83B (2026-05-12)

**Failure surface.** Run 9 B3 (`FN_LOAD_OPS_RISK_DATA`, CBA branch
question). Body claims `"This entire function ONLY runs when the
reporting month is December"` — but the source contains a *localized*
December conditional (`IF TO_NUMBER (EXTRACT (MONTH FROM TO_DATE
(CQD, 'DD-MON-RR'))) = 12 THEN ...`) that gates ONE nested block,
not the whole function body. The function has branches outside the
IF that run regardless of month.

**Why W83B doesn't catch this.** W83B's source-content gate
(`_w57_source_has_december_gate`) is a binary presence check: any
month-12 logic anywhere in the source → claim is grounded → no fire.
Localized vs whole-function gating is not distinguishable without
control-flow position analysis of the predicate (does the IF wrap
the function body, or does it wrap one branch?). RTIE has the AST
infrastructure for this (`src/parsing/query_engine.py`) but
wiring it into a W57 sub-check is non-trivial.

**Detection sketch (for future work).**

1. Detect the body phrase pattern "entire function only runs …
   December" / "this function only runs … December" / "the function
   runs only … December" — a stronger claim than W83B's hedged
   forms.
2. Locate the December predicate in source (using
   `_W57_DECEMBER_GATE_PATTERNS`).
3. Walk the AST from the predicate up: if the enclosing block
   includes the function's RETURN/COMMIT/end, the predicate gates
   the whole function — claim is grounded. Otherwise the predicate
   wraps a nested block — claim is overgeneralization → fire.

**Trade-off.** W83C's complexity is significantly higher than W83B's.
Run 9 evidence is one case (B3). Defer until either (a) the same
class shows up in another benchmark run, or (b) the AST utilities
are needed for an unrelated piece of work and W83C becomes
cheap-to-add as a side benefit.

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
