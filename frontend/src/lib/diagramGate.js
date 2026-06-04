// W151 Phase 4 — the done-equality suppression rule (trust-critical).
//
// The frontend renders a trace diagram ONLY when the `done` event says the
// diagram was emitted AND its grounding equals the body badge. The backend
// already makes diagram_grounding == badge by construction (body-badge-as-
// render-ceiling), so this check should always pass for a correctly-built
// diagram — it is the belt-and-suspenders backstop that suppresses on an
// assembler/transport bug rather than ship a diagram that disagrees with the
// authoritative prose. On suppression the UI renders NO diagram and degrades
// cleanly to the prose answer.
//
// Pure function (no DOM, no React) so it is unit-testable in the node-env
// vitest the repo already runs — no jsdom.

/**
 * @param {object|null|undefined} done - the SSE `done` payload.
 * @returns {boolean} true iff a diagram should render.
 */
export function shouldRenderDiagram(done) {
  if (!done) return false;
  if (done.diagram_emitted !== true) return false;
  // DECLINED never reaches here (no diagram is emitted on that path), but the
  // equality check below also covers it: a null/absent diagram_grounding can
  // never equal a real badge.
  return done.diagram_grounding === done.badge;
}
