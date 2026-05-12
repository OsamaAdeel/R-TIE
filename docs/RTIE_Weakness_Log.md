# RTIE Weakness Log

Catalog of known weaknesses in RTIE's grounding/routing layers, with
discovery context. Entries are append-only; closed weaknesses keep
their entry with a status update rather than being deleted.

Numbering matches the W-ticket convention used in branches, code
comments, and PR titles (`refactor/w35-…`, `fix/w83b-…`, etc.).

---

## W85. Anchor-vs-asked-function mismatch — Discovered Run 9 (2026-05-12)

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
