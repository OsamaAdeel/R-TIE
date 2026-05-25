# W134 — Confidence-vs-Warning-Class Coupling Audit

**Branch:** `audit/w134-confidence-warning-class`
**Date:** 2026-05-25
**Trigger:** P1 quality harness surfaced a 0.95 vs 0.4 confidence split
between B4 and A2 — structurally similar wrong-anchor + W96 December
fabrication failure modes producing very different confidence values.

## Summary

The grounding `confidence` field is a **5-bucket discrete lookup** on
two boolean inputs `(badge, has_citations)`, not a continuous quality
score. The 0.95 vs 0.4 split between B4 and A2 is the formula
faithfully reflecting its two-bit signal: A2's W57 fired
(`GROUNDING-HIGH: cited function not in retrieved sources`) → blocking
warning → UNVERIFIED bucket → 0.4. B4's W57 only fired the
`GROUNDING-LOW:` line-padding advisory → no blocking warning →
VERIFIED bucket → 0.95. The body-content defects (wrong anchor + W96
December fabrication) that the user observed in both responses are
not inputs to the formula — they would have to surface as a non-LOW
W57 warning to register.

This is **GAP A** with **GAP C** undertones. Detection is the
right-shape fix for B4 (tracked under W137 strict-validator + future
W96 detector work). W134 ships a **surgical mitigation** so the
formula at least respects its own LOW advisories. The architectural
rework (continuous confidence with multiple independent inputs) is
deferred to **W144** backlog.

## 1. Confidence formula — current state

Location: [`src/agents/logic_explainer.py:331-347`](../src/agents/logic_explainer.py).

```python
blocking_warnings = [
    w for w in warnings if not w.startswith("GROUNDING-LOW:")
]

if requires_citations and not has_citations:
    badge = "UNVERIFIED"
    confidence = 0.0
elif blocking_warnings:
    badge = "UNVERIFIED"
    confidence = 0.4 if citations else 0.2
else:
    badge = "VERIFIED"
    confidence = 0.95 if citations else 0.8
```

Inputs: `bool(blocking_warnings)`, `bool(citations)`,
`requires_citations`, `has_citations`. **Nothing else.** No
weighting of warning count, no severity gradation beyond HIGH/LOW
binary, no body-length or citation-density signal.

Effective contract (5-value lookup):

| State | Confidence |
|---|---|
| `requires_citations=True, has_citations=False` | `0.0` |
| `UNVERIFIED, no citations` | `0.2` |
| `UNVERIFIED, citations` | `0.4` |
| `VERIFIED, no citations` | `0.8` |
| `VERIFIED, citations` | `0.95` |

Downstream confidence mutations on the `/v1/stream` path:

- `src/main.py:1763` — `PARTIAL_SOURCE_INDEXED` overrides to `0.2`.
- W108-TRUNCATED **deliberately does not override** (main.py:1769-1786);
  it flips the badge to UNVERIFIED and lets the formula route the
  result to `0.4` automatically.

No other site mutates the grounding-dict confidence on the streaming
path. Other `confidence` settings in `src/` are either initial-state
defaults (`0.0`), special-case responses (`DECLINED` /
`UNRECOGNIZED_TERM` / `SCOPE_MISMATCH`), or the `/v1/query` path's
validator (uses `resolved/total` ratio — that path skips the W57
overlay entirely).

## 2. B4 vs A2 trace (from `scratch/quality_harness_results.json`, P1 baseline)

| Field | B4 | A2 |
|---|---|---|
| Query | "What determines if an exposure gets deducted from capital?" | "What's RRP?" |
| Schema | OFSMDM | OFSMDM |
| **Badge** | **VERIFIED** | **UNVERIFIED** |
| **Confidence** | **0.95** | **0.4** |
| Warnings count | 1 | 1 |
| Warning | `GROUNDING-LOW: Line 24 cited 5 times (threshold 3); likely line-by-line padding` | `GROUNDING-HIGH: cited function 'FN_G_TEST_CSTM' not in retrieved sources` |
| `blocking_warnings` length | 0 | 1 |
| `source_citations` | 28 inline line refs | 4 inline line refs |
| `<cite index=>` tags | 0 | 0 |
| Body word count | 629 | 262 |
| `functions_analyzed` | 40 | 35 |
| Anchor in body | `ABL_MARKET_RISK_EXPOSURES_FROM_MRVAR` (wrong) | `FN_G_TEST_CSTM` (wrong) |
| W96 December fabrication in body | yes — "ONLY runs when the reporting month is December, specifically for the date '2026-03-31'" | yes — "ONLY runs when the reporting month is December" |

Manual formula application:

- **B4**: `requires_citations=True`, `has_citations=True`,
  `blocking_warnings=[]` → `else` branch → `VERIFIED, 0.95`. Matches
  captured `0.95`.
- **A2**: `requires_citations=True`, `has_citations=True`,
  `blocking_warnings=['GROUNDING-HIGH: ...']` → `elif` branch →
  `UNVERIFIED, 0.4`. Matches captured `0.4`.

The formula is being applied correctly. The 0.95 vs 0.4 split is not
a bug in the calculation path.

## 3. Gap diagnosis

### Primary: GAP A — no fallback when W57 misses content defects

The formula's only content-correctness input is `blocking_warnings`,
which depends entirely on W57 (or pre-W57 detectors: `CONTRADICTION`,
`UNGROUNDED_IDENTIFIERS`, `NAMED_FUNCTION_NOT_RETRIEVED`,
`CITATIONS`) firing. When W57 misses a content defect — as it did
for B4's wrong-anchor + W96 December fabrication at P1 capture time —
the formula has **no fallback input** that could suppress confidence.
B4 surfaces at the maximum `0.95` not because the response looked
trustworthy, but because the only thing that could have moved
confidence below `0.95` (a non-LOW warning) didn't fire.

This rules out the framings in the original audit prompt:

- It is **not** "confidence ignoring W96-class content checks" —
  W96-class checks aren't wired separately into the formula; they
  must surface as a non-LOW W57 warning to register.
- It is **not** "treating coherent body + many citations as a quality
  signal" — citations are a binary flag and "coherence" is not
  measured. `28 citations` vs `4 citations` makes zero difference
  once both clear the `bool(citations)` threshold.

### Secondary: GAP C — design itself is sparse

An argument can be made that the 0.95 result is "by design" — the
formula does what its inputs dictate. But that design is itself the
gap. A response with one `GROUNDING-HIGH` warning gets the same
`0.4` as a response with five `GROUNDING-HIGH` warnings. A response
with three `GROUNDING-LOW` advisories gets the same `0.95` as a
response with zero advisories. There is no granularity.

## 4. W108 design-rationale sub-finding (and B3 concrete evidence)

W108's "no override" decision
([main.py:1769-1786](../src/main.py), pre-W134) reasoned:

> Confidence is intentionally NOT overridden here ... grounding's own
> confidence calculation should stand.

with the rationale: "grounding's own confidence calculation already
accounts for evidence quality; override would compete with that
signal."

**The rationale does not hold.** The calculation is a 5-bucket
lookup on `(badge, has_citations)`. It does no quality accounting.

The W108 output behavior was *believed* to be correct in practice on
the assumption that `W108-TRUNCATED` would be present in the warnings
array *before* `evaluate_grounding` evaluates `blocking_warnings`,
flipping the badge to UNVERIFIED and routing confidence to 0.4
automatically. **That assumption was wrong.** W108-TRUNCATED is
appended in `main.py:1779-1786` **after** `evaluate_grounding`
returns, so the formula never sees it. The badge gets flipped to
UNVERIFIED externally; confidence stays at whatever the formula
computed pre-truncation (potentially `0.95`).

### B3 concrete evidence (W134 canary sample, post-W137 backend)

Canary B3 ("Where does counterparty reclassification happen?") drove
this gap into a live response:

```
badge      : UNVERIFIED
confidence : 0.95
warnings   :
  - W108-TRUNCATED: response based on 23 of 35 retrieved functions; ...
```

`evaluate_grounding` returned `VERIFIED + 0.95` (no internal W57
warnings). `main.py:1785` then flipped badge to `UNVERIFIED` for the
W108 truncation but did not touch confidence — producing the
structurally inconsistent `UNVERIFIED + 0.95` payload that the SSE
`done` event published to the frontend.

### Mitigation shipped under W134 (Change 2)

`main.py:1785-1789` now also caps confidence at `0.4` alongside the
existing badge override:

```python
grounding["badge"] = "UNVERIFIED"
if grounding["confidence"] > 0.4:
    grounding["confidence"] = 0.4
```

This brings the W108 block in line with what the formula would have
produced had `W108-TRUNCATED` been visible to `blocking_warnings`
(UNVERIFIED + citations → 0.4). The `> 0.4` guard prevents the cap
from inflating an already-lower value if multiple post-hoc warnings
fire on the same response (`PARTIAL_SOURCE_INDEXED` overrides to
`0.2` immediately above this block — its result must survive).

The sub-finding is therefore **discovered AND mitigated under W134**,
not deferred to W144.

### Re-stated rule for future "no override" decisions

> When a new warning category is appended to the warnings array
> *after* `evaluate_grounding` returns and flips badge to UNVERIFIED,
> the same block must also cap confidence at the matching UNVERIFIED
> bucket (`0.4` with citations, `0.2` without). The formula does not
> independently re-evaluate post-hoc warnings; the appending block
> owns the confidence adjustment.

W144 (architectural rework) should remove the need for this rule by
making confidence a function of the final warnings array rather than
a 5-bucket lookup on `(badge, has_citations)`.

## 5. W134 surgical mitigations (this branch)

W134 ships **two surgical changes** addressing the same structural
pattern: confidence should always reflect the worst signal in the
warnings array, regardless of whether the warning was emitted by
`evaluate_grounding`'s own checks or appended downstream by main.py.

**Change 1:** `src/agents/logic_explainer.py:345-358`. When
`badge=VERIFIED` and the **full warnings array** (not just
`blocking_warnings`) is non-empty, cap confidence at `0.85` instead
of `0.95`. Closes the "detected an issue but maximum confidence"
cosmetic gap inside the formula.

**Change 2:** `src/main.py:1779-1789`. The W108-TRUNCATED post-hoc
block now also caps confidence at `0.4` alongside the existing
`badge = "UNVERIFIED"` override. Closes the B3 finding
(UNVERIFIED + 0.95 was shipping when W108 truncated cleanly retrieved
responses). PARTIAL_SOURCE_INDEXED already overrides to `0.2` and is
unchanged.

| Branch | Before | After |
|---|---|---|
| `VERIFIED + citations + no warnings` | `0.95` | `0.95` (unchanged) |
| `VERIFIED + citations + warnings present` | `0.95` | **`0.85`** |
| `VERIFIED + no citations` | `0.80` | `0.80` (unchanged) |
| `UNVERIFIED + citations` | `0.40` | `0.40` (unchanged) |
| `UNVERIFIED + no citations` | `0.20` | `0.20` (unchanged) |
| `CITATIONS-required-but-missing` | `0.00` | `0.00` (unchanged) |

The "warnings present" check uses the **full warnings array**
(including `GROUNDING-LOW:` advisories) — that's the whole point.
LOW advisories should dampen confidence even though they don't flip
badge. Without this guard, the formula was structurally
inconsistent: it had detected a citation-padding / range-repeat
issue but still emitted maximum confidence.

### What this does *not* do

- Does not fix B4's underlying W96 fabrication. (B4's confidence
  drops in the live capture, but that's W137 detection firing, not
  W134's caps.) Where Change 1's cap *would* apply (a VERIFIED
  response with a `GROUNDING-LOW` advisory only), it brings confidence
  from "lying" (0.95) to "visibly less confident, still wrong" (0.85).
- Does not touch badge logic. Both badge transitions
  (VERIFIED→UNVERIFIED in the W108/PARTIAL_SOURCE_INDEXED blocks, and
  the formula's own bucket assignment) are unchanged.
- Does not touch W108's detection or char-budget mechanism. Only the
  confidence value alongside the existing W108 badge flip changes.
- Does not change the UNVERIFIED bucket constants (`0.4` / `0.2`)
  inside `evaluate_grounding`.
- Does not affect the `/v1/query` path (which never enters
  `evaluate_grounding`).

### Regression sample expected behavior

### Live canary sample (post-restart, both changes shipped)

Two runs captured: one with Change 1 only (after the first restart),
one with both changes (after the second restart).

| Canary | P1 baseline (2026-05-22) | Change 1 only | Both changes | Path |
|---|---|---|---|---|
| B4 | `VERIFIED + 0.95` | `UNVERIFIED + 0.4` | `UNVERIFIED + 0.4` | W137 strict-validator now fires `GROUNDING-HIGH` + W108-TRUNCATED → blocking → 0.4. The detection improvement does the work here; W134's caps are not load-bearing for B4. |
| A2 | `UNVERIFIED + 0.4` | `UNVERIFIED + 0.4` | `UNVERIFIED + 0.4` | Unchanged. GROUNDING-HIGH cited-function-missing fires. |
| **B3** | (not in P1 set) | **`UNVERIFIED + 0.95`** ⚠ | **`UNVERIFIED + 0.4`** ✅ | **Change 2's load-bearing case.** Change-1-only run captured B3 with only `W108-TRUNCATED` firing — pre-Change-2 the badge flipped externally but confidence held at 0.95. Post-Change-2 the cap routes confidence to 0.4 alongside the badge flip. (The two-changes run surfaced an additional `GROUNDING-CALENDAR-HIGH: ... March gate` from LLM non-determinism, so the live drop is over-determined — but `tests/unit/agents/test_w134_confidence_warning_class.py::test_w134_post_hoc_w108_truncated_caps_confidence_at_04` pins the W108-only contract.) |
| C3 | (not in P1 set) | `UNVERIFIED + 0.2` | `UNVERIFIED + 0.2` | `GROUNDING-CALENDAR-HIGH` + `W108-TRUNCATED` blocking, no `(Line N)` citations → 0.2 via the existing UNVERIFIED+no-citations branch. |
| E1 | `DECLINED` | `DECLINED` | `UNVERIFIED + 0.4` | LLM non-determinism — E1 routed to DECLINED in one capture and to the streaming logic-explainer in another. Both results are internally consistent. |
| F1 / F2 / F3 | `REJECTED` | `REJECTED` | `REJECTED` | Unchanged. W130 path, untouched by W134. |

## 6. W144 backlog — architectural rework

**Goal:** confidence as a continuous quality score with independent
inputs, not a 5-bucket lookup.

**Candidate inputs (sketch — not a design):**

- Content coherence signal (independent of W57 firing): per-claim
  binding to source, identifier coverage in body vs in retrieved
  sources, named-anchor presence in body.
- Citation density: `<cite>` tag count vs body word count, line-ref
  distribution entropy (catches "Line 24 cited 5 times" without
  needing the explicit `GROUNDING-LOW` advisory).
- Warning count + severity weighting: each `GROUNDING-LOW` shaves a
  small amount; each `GROUNDING-HIGH` shaves a large amount; multiple
  HIGHs accumulate.
- Functions-analyzed coverage: how much of the retrieval matched the
  asked-about anchor.
- Body length normalization: long bodies with no citations are
  *worse* than short bodies with no citations.

**Out of scope for P2-pre and P2-main.** Multi-day effort. Revisit
W108's "no override" decision once W144 lands.

**Trigger to re-prioritize W144:** another P1-shape confidence split
where the formula's two-bit signal disagrees with content reality and
the surgical mitigation here (cap at 0.85 with warnings) is not enough
to make the output honest.

## 7. References

- `src/agents/logic_explainer.py` — `evaluate_grounding` and the
  confidence formula.
- `src/main.py:1722-1748` — `evaluate_grounding` call site.
- `src/main.py:1763` — `PARTIAL_SOURCE_INDEXED` confidence override.
- `src/main.py:1769-1786` — W108 "no override" decision + comment.
- `scratch/quality_harness_results.json` — P1 capture (2026-05-22).
- `tests/unit/agents/test_grounding.py` — existing
  `evaluate_grounding` test surface.
- `tests/unit/agents/test_w134_confidence_warning_class.py` — W134
  regression-guard tests added on this branch.
