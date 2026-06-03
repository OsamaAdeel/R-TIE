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
