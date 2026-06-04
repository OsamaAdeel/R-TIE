# W151 — Trace Diagram: Phase Plan & Status

Design reference: `docs/trace_diagram_grammar_spec.md` (grammar) + the
per-element grounding decision note (Option A: projection, not a change to
`evaluate_grounding`).

This doc tracks the phased build and the items deferred out of each phase.

## Phasing

| Phase | Scope | Status |
|---|---|---|
| **1** | Backend data assembler `build_trace_diagram` + per-element grounding + unit tests. No SSE, no layout, no frontend, no `/v1/source`. | **DONE** (this commit) |
| 2 | Client-side auto-layout (dagre) over emitted topology. | **DONE** |
| 3 | SSE `event: diagram` emission in `/v1/stream` + the `done`-event grounding-equality assertion (derivation-dag path). | **DONE** |
| 3.5 | `tagged_lines → fan_in_steps` adapter (Model A, flat) + fan-in emit branch. **Fallback-path-only coverage** (see finding below). | **DONE** |
| 3.6 | `graph → fan_in_steps` projection — real common-case fan-in (graph/`llm_payload` path). Own Stage-2 plan; modeling task. | not started |
| 4 | Frontend render via the prototype component (`_proto_trace/CitedTraceDiagram.jsx`), fed the event instead of the fixture. | not started |
| 5 | `/v1/source` lazy overflow endpoint (serves citation spans beyond the ~80-line embed cap). | not started |

## Phase 1 — landed

- **Module:** `src/agents/trace_diagram.py` — pure `build_trace_diagram(*, target,
  trace_kind, multi_source, grounding, fan_in_steps=None, derivation_records=None,
  alternatives=None) -> dict | None`. No LLM, no Redis, no `main.py` edit, no SSE
  emit.
- **Tests:** `tests/unit/agents/test_trace_diagram.py` — 16 tests, all passing.
- **Grounding rules (baked in, verbatim from the decision note):**
  1. Node VERIFIED iff function ∈ `multi_source` AND a real resolved `[start,end]`
     span present in that function's `source_code`.
  2. Edge (a flow claim) VERIFIED iff member+span AND not in an `alternative`
     group AND `ungroundedGap == false`. Alternatives/gaps are always UNVERIFIED.
  3. Body badge is a render ceiling: `badge != VERIFIED` downgrades every element
     to UNVERIFIED, so `diagram_grounding == grounding["badge"]` by construction.
  4. `DECLINED` badge → returns `None` (separate response path; explicit guard).
- **Citation atom (W51):** `{function, lines, text}` sliced from the SAME
  `multi_source[fn]["source_code"]` list; never regex-scraped from markdown.
  Text capped at 80 lines with a `truncated` marker (overflow → Phase 5).
- **Grammar extensions ratified into the spec** (`trace_diagram_grammar_spec.md`
  §1.3 / §1.4): the optional edge `label` field (`+`/`−`/`=`) and pass-through
  `groups` semantics.

## Phase 2 — landed

- **Module:** `frontend/src/_proto_trace/layout.js` — pure
  `computeLayout(topology) -> {nodes:{id:{x,y,w,h}}, groups, bounds}`. Topology
  in, coordinates out. Trust-blind: never reads or touches grounding.
- **Approach:** dagre (`@dagrejs/dagre`), `rankdir: 'LR'` (sources/operands left
  → target sink right), dagre center coords converted to top-left. Alternative
  groups are a **computed overlay** — frame = bounding box of member rects;
  OR-divider + divergence anchors derived from member positions (never
  hand-placed).
- **Renderer wiring:** `_proto_trace/CitedTraceDiagram.jsx` consumes the computed
  rects (`Frames` → `LayoutFrames`); SVG canvas + container sized to
  `bounds`. The `groundingGuard` dispatch and the edge bezier are untouched —
  only the coordinate source changed.
- **Tests:** `frontend/src/_proto_trace/layout.test.js` — 9 Vitest tests
  (`npm test` → `vitest run`), all passing. Lint clean.
- **Visual check:** headless render confirmed the alternatives frame reads as
  disjoint "pick one" (two stacked lanes + OR divider + ringed divergence), with
  the VERIFIED cited path solid and outside the frame.
- **Dependencies added:** `@dagrejs/dagre` (runtime), `vitest` (dev).
- **Still sandboxed** in `_proto_trace/`; Phase 4 relocates `layout.js` to a
  shared lib when the renderer leaves the sandbox.

> **npm audit note (left as-is, per decision).** The Phase-2 installs surfaced
> **1 moderate-severity transitive advisory** in the frontend dependency tree.
> It is unrelated to RTIE code. `npm audit fix` was deliberately **not** run
> (could shift transitive versions out from under the lockfile). Revisit during
> a dedicated frontend dependency pass, not mid-feature.

## Phase 3 — landed (derivation-dag path)

- **Orchestration:** `src/agents/trace_diagram.py` →
  `diagram_from_bi_routing(bi_routing, multi_source, grounding, graph_lookup)`.
  Pure: injected `graph_lookup` callable `(schema, function) -> graph | None`
  keeps it Redis-free and unit-testable. Reads the **full** derivation records
  from the per-function graph (`graph["derivations"]`, loader.py:448-449) — NOT
  the literal-index summary on `bi_routing["derivation"]`.
- **Emit seam (`src/main.py`):** `event: diagram` is yielded **after the caveat
  stream and before `event: done`** (once `grounding["badge"]` is final, post
  W49/W108 overrides). A **defensive pre-emit assert** verifies
  `diagram["diagram_grounding"] == grounding["badge"]` (true by Phase-1 rule 3);
  on mismatch it suppresses + logs. The whole block is best-effort
  (try/except) — a diagram failure never breaks the stream. The Redis read is
  at the caller (`get_function_graph` on the existing `graph:{schema}:{fn}`
  keyspace), so the assembler stays pure.
- **`done` payload:** adds `diagram_emitted` (bool) and `diagram_grounding`
  (badge-or-null) for the Phase-4 frontend suppression check.
- **DECLINED:** structurally never reaches the emit point (it's built in the
  `except` branch), so no diagram is produced — automatic.
- **Fan-in stash (inert):** `vt_tagged` is hoisted in the VARIABLE_TRACE branch
  and marked `# W151 Phase 3.5 consumes this` — landed now, consumed in 3.5.
- **Tests:** +7 unit tests in `tests/unit/agents/test_trace_diagram.py`
  (solid / ceiling / retrieval-gap-dashed / no-routing / incomplete /
  no-derivations / DECLINED). Full file 23/23 passing.
- **Round-trip checkpoint:** confirmed `graph["derivations"]` survives the
  MessagePack round-trip both at the codec level and against real stored Redis
  data (4 live OFSERM derivation graphs decoded intact).
- **End-to-end canary (`scratch/w151_canary_derivation_dag.py`, untracked):**
  `"How is CAP943 derived?"` → `event: diagram` `derivation-dag`,
  `CAP943 ← CAP309 (+) / CAP863 (−)`, all VERIFIED; **CASE = SOLID** (resolved
  function in `functions_analyzed`); invariant `diagram_grounding == done.badge`
  holds. The canary reports SOLID vs DASHED(retrieval-gap) vs CEILING so a
  dashed result reads as designed degradation, not a bug. (`CAP943 = CAP309 −
  CAP863` is a real corpus record in `CS_DEFERRED_TAX_ASSET_NET_OF_DTL_
  CALCULATION`.)

## Phase 3.5 — landed (fan-in adapter, FALLBACK-PATH-ONLY coverage)

- **Adapter:** `src/agents/trace_diagram.py` → `fan_in_steps_from_tagged_lines(
  tagged_lines, target_variable, *, gap=2)`. Pure, in the diagram module (no
  tracer changes). Model A, flat: writer→sink, read→own-function-writer (first
  writer by line), no cross-function chaining; writer-less-function reads
  dropped; `COMMENTED_OUT`/`TRANSFORM`/`PARAMETER` excluded as writers;
  same-`(function, operation)` tags within `gap=2` lines coalesced into one
  node with `[line_start, line_end]`.
- **Emit branch (`src/main.py`):** derivation-dag wins; else, if `vt_tagged` was
  stashed, project it and build a fan-in diagram. Reuses the Phase-3
  assert/emit/done logic verbatim.
- **Tests:** +11 unit tests (34/34 in the file pass), incl. end-to-end through
  `build_trace_diagram` (solid under VERIFIED, ceiling-clamped under UNVERIFIED).

> ### ⚠️ KNOWN GAP — Phase 3.5 covers only the fallback path
> The `tagged_lines` stash (`vt_tagged`) is set **only** in the variable-tracer
> streaming branch (`main.py` ~1706), which runs **only when the graph pipeline
> produced no `llm_payload`**. The dispatch chain checks
> `elif state.get("llm_payload")` (~1694) **first**, so every graph-resolvable
> VARIABLE_TRACE query is streamed by the graph-payload branch
> (`stream_semantic`) and **never sets `vt_tagged`** → **no fan-in diagram**.
>
> **Evidence (2026-06-03 canary):** all 6 `"How is X calculated?"` candidates
> routed `VARIABLE_TRACE` but emitted **no** diagram; the stage-timer log showed
> the last 8 streams were all `llm_stream_semantic_graph`, and lifetime
> `llm_stream_variable_trace` had fired only 13× vs 42 graph + 193
> semantic-fallback. So the variable-tracer branch is the **rare** path.
>
> **Conclusion:** for the common case, the real fan-in source is the **graph**
> (`llm_payload` / `graph_node_ids` / per-function graphs), **NOT** `tagged_lines`.
> Phase 3.5's adapter is correct and unit-tested but, by design, fires only on
> the (rare) graph-unresolvable fallback path. A deliberately graph-unresolvable
> variable was **NOT** used to fake a live green — that would validate only the
> rare path and read as misleading coverage. Live fan-in on the common path is
> Phase 3.6.

## Phase 3.6 — graph → fan_in_steps (the real common-case fan-in)

**Scope (own Stage-2 plan required — a modeling task, not a quick add-on).**
Project the graph the common VARIABLE_TRACE path already builds (the assembled
`llm_payload` / `graph_node_ids` and the per-function `graph:{schema}:{fn}`
nodes+edges) into `fan_in_steps`, so a fan-in diagram fires on the
graph-resolvable path. Keep **Model A, flat** (writer→sink, read→own-function
context, **no cross-function inference**) — the same locally-grounded topology
rule as 3.5, now sourced from graph nodes instead of `tagged_lines`. Reuses the
Phase-1 assembler + Phase-3 emit seam unchanged. The 3.5 `tagged_lines` adapter
remains as the fallback-path producer. Open modeling questions (node grouping
from graph nodes, writer-vs-read classification on graph node types, multi-line
coalescing equivalent) to be settled in the 3.6 Stage-2 plan before code.

## Deferred items (gate later phases)

### (A) Flag B — variable-trace path must stash STRUCTURED steps
**Wiring-phase prerequisite (blocks Phase 3 for the fan-in shape).**
Today the variable-trace producer stores only `state["variable_chain"]
["chain_text"]` — prose, not topology (`variable_tracer.py:1345-1354`). The
assembler's `fan_in_steps` needs structured nodes (`node_id, function,
node_type/operation, line_start, line_end`). Reconstructing them from
`chain_text` would re-introduce the markdown-scrape coupling W51 exists to kill.
**Action when Phase 3 wiring starts:** have the variable-trace path stash a
structured step list into state (or have the caller read the per-function graph
nodes `graph:{schema}:{fn}`, which already carry `id/type/line_start/line_end`),
and feed that to `build_trace_diagram`. The Phase-2 value path already has the
structure via `proof_builder.steps[]` — use the graph node's `line_start/line_end`,
not the step's stringified `source_ref` (`proof_builder.py:267-272`).

### (B) Alternative-group derivation has no producer — tie to W150 near-twin
**Deferred to a later phase.** The Phase-1 assembler takes `alternatives` as an
explicit input and only *passes them through* (normalizing shape, propagating
`members` into Rule 2). Nothing in the pipeline currently *derives* alternative
groups. The natural producer is the **W150 near-twin disambiguation path** — the
near-twin siblings cohort (`functions_analyzed` / `near_twin_siblings`, surfaced
around `main.py:2926-2938`) is exactly the "two candidate writers, pick one"
structure the `alternative` group models, and the spec's invariant requires twin
descriptions to come from `rtie:vec`, never generated. **Action in a later
phase:** build an `alternatives`-from-near-twin adapter feeding
`build_trace_diagram`, sourcing candidate labels/descriptions from the vector
store, not the LLM.

## HOLD
Phase 1 only. Do not start Phase 2 (layout), Phase 3 (SSE), Phase 4 (frontend),
or Phase 5 (`/v1/source`) without explicit go-ahead.
