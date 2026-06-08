// ============================================================================
// W151 diagram polish — hover-focus helpers (pure, render-interaction only).
//
// When the user hovers an edge, that edge AND its two endpoint nodes are
// emphasized while everything else dims. This module computes the focus set
// from the existing {from,to} edge data — no new payload, no layout recompute.
//
// It is PURE and TRUST-BLIND: it decides what to *emphasize*, never what to
// *render* or how grounded it is. The solid/dashed grounding dispatch in
// CitedTraceDiagram is untouched; dimming is opacity only.
// ============================================================================

/**
 * Resolve the hovered edge into a focus descriptor.
 * @param {Array<{id:string, from:string, to:string}>} edges
 * @param {string|null|undefined} edgeId - the currently hovered edge id
 * @returns {{edgeId:string, nodeIds:Set<string>}|null} null when nothing is
 *   hovered (or the id matches no edge) — callers treat null as "no focus".
 */
export function edgeFocus(edges, edgeId) {
  if (edgeId == null) return null;
  const e = (edges || []).find((x) => x.id === edgeId);
  if (!e) return null;
  return { edgeId, nodeIds: new Set([e.from, e.to]) };
}

/** A node is dimmed when a focus is active and it is not an endpoint of it. */
export function isNodeDimmed(focus, nodeId) {
  return !!focus && !focus.nodeIds.has(nodeId);
}

/** The hovered edge itself — emphasized. */
export function isEdgeFocused(focus, edgeId) {
  return !!focus && focus.edgeId === edgeId;
}

/** Every other edge while a focus is active — dimmed. */
export function isEdgeDimmed(focus, edgeId) {
  return !!focus && focus.edgeId !== edgeId;
}
