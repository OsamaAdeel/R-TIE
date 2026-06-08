// ============================================================================
// PROTOTYPE — W151 Phase 2 auto-layout.
//
// computeLayout(topology) — topology IN, coordinates OUT. The backend emits
// {nodes, edges, groups} with NO geometry (Phase 1 deliberately omits pos/x/y);
// this module computes each node's {x, y, w, h}, the alternative-group frame
// boxes, and the OR / divergence overlay anchors using dagre (a layered DAG,
// ranked left-to-right so sources/operands sit left of the target sink).
//
// It is PURE and TRUST-BLIND: it positions boxes and never reads, derives, or
// touches grounding. This replaces ONLY the prototype's hand-placed pixel
// coordinates — the renderer's groundingGuard dispatch is unaffected.
// ============================================================================
import dagre from '@dagrejs/dagre';

// --- sizing (mirrors the prototype's hand-placed box sizes) -----------------
const CHAR_W = 7;          // ~px per char of the mono node label at ~11.5px
const NODE_PAD_X = 36;     // icon + horizontal padding
const MIN_W = 160;
const MAX_W = 320;
const DEFAULT_H = 50;
const HEIGHT_BY_KIND = {
  'target-column': 70,
  'derived-column': 68,
  'cap-literal': 56,
  'source-table': 50,
  'filter': 50,
  'absent-filter': 50,
  'operand': 50,
  'intermediate': 50,
};

// --- dagre tuning -----------------------------------------------------------
// W151 polish: widened the spacing budget so dense fan-ins (degree-12+) get
// room instead of cramming. In LR, NODESEP is the within-rank (vertical) gap —
// the lever that decongests a tall source rank — and RANKSEP is the between-
// rank (horizontal) gap, which widens the canvas. Both are pure geometry; the
// canvas grows and the container scrolls (overflow-x-auto) so the chat column
// is never broken.
const RANKDIR = 'LR';      // converge to the target sink on the right
const NODESEP = 56;        // gap between nodes in the same rank (vertical in LR)
const RANKSEP = 120;       // gap between ranks (depth; horizontal in LR)
const MARGIN = 24;

// --- group-frame padding ----------------------------------------------------
const GROUP_PAD = 22;
const CANDIDATE_PAD = 12;

function sizeForNode(node) {
  const label = String(node.label || node.id || '');
  const w = Math.max(
    MIN_W,
    Math.min(MAX_W, Math.round(label.length * CHAR_W) + NODE_PAD_X),
  );
  const h = HEIGHT_BY_KIND[node.kind] || DEFAULT_H;
  return { w, h };
}

function _bbox(ids, nodePos, pad) {
  const rects = (ids || []).map((id) => nodePos[id]).filter(Boolean);
  if (!rects.length) return null;
  const minX = Math.min(...rects.map((r) => r.x));
  const minY = Math.min(...rects.map((r) => r.y));
  const maxX = Math.max(...rects.map((r) => r.x + r.w));
  const maxY = Math.max(...rects.map((r) => r.y + r.h));
  return {
    x: Math.round(minX - pad),
    y: Math.round(minY - pad),
    w: Math.round(maxX - minX + 2 * pad),
    h: Math.round(maxY - minY + 2 * pad),
  };
}

// Compute an alternative group's overlay rects from its members' positions —
// NOT hand-placed. The frame is the bounding box of all members; each
// candidate gets its own sub-frame; the OR divider sits between the first two
// candidate frames; the divergence note anchors at the midpoint of the two
// diverging nodes.
function _buildGroupFrame(group, nodePos) {
  const frame = _bbox(group.members || [], nodePos, GROUP_PAD);

  const candidates = (group.candidates || [])
    .map((c) => ({ label: c.label, frame: _bbox(c.nodes || [], nodePos, CANDIDATE_PAD) }))
    .filter((c) => c.frame);

  let divider = null;
  if (candidates.length >= 2) {
    const a = candidates[0].frame;
    const b = candidates[1].frame;
    divider = {
      x: Math.round((Math.max(a.x, b.x) + Math.min(a.x + a.w, b.x + b.w)) / 2),
      y: Math.round((a.y + a.h + b.y) / 2),
    };
  }

  let divergence = null;
  const between = (group.divergence && group.divergence.between) || [];
  if (between.length >= 2) {
    const r0 = nodePos[between[0]];
    const r1 = nodePos[between[1]];
    if (r0 && r1) {
      divergence = {
        between: [between[0], between[1]],
        note: (group.divergence && group.divergence.note) || '',
        x: Math.round(((r0.x + r0.w / 2) + (r1.x + r1.w / 2)) / 2),
        y: Math.round(((r0.y + r0.h / 2) + (r1.y + r1.h / 2)) / 2),
      };
    }
  }

  return { kind: group.kind, label: group.label, frame, candidates, divider, divergence };
}

function _boundsFrom(nodePos) {
  const rects = Object.values(nodePos);
  if (!rects.length) return { w: 0, h: 0 };
  return {
    w: Math.round(Math.max(...rects.map((r) => r.x + r.w)) + MARGIN),
    h: Math.round(Math.max(...rects.map((r) => r.y + r.h)) + MARGIN),
  };
}

/**
 * Lay out a diagram topology.
 *
 * @param {{nodes:Array, edges:Array, groups:Array}} topology - the assembler's
 *   emitted payload (no geometry).
 * @returns {{nodes:Object<string,{x,y,w,h}>, groups:Array, bounds:{w,h}}}
 *   top-left node rects, group overlay rects, and the canvas bounds.
 */
export function computeLayout(topology) {
  const nodes = (topology && topology.nodes) || [];
  const edges = (topology && topology.edges) || [];
  const groups = (topology && topology.groups) || [];

  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: RANKDIR, nodesep: NODESEP, ranksep: RANKSEP, marginx: MARGIN, marginy: MARGIN });
  g.setDefaultEdgeLabel(() => ({}));

  const sizes = {};
  for (const n of nodes) {
    const { w, h } = sizeForNode(n);
    sizes[n.id] = { w, h };
    g.setNode(n.id, { width: w, height: h });
  }
  for (const e of edges) {
    // defensive: only wire edges whose endpoints are real nodes
    if (g.hasNode(e.from) && g.hasNode(e.to)) g.setEdge(e.from, e.to);
  }

  dagre.layout(g);

  // dagre returns node CENTER coords; convert to top-left for absolute layout.
  const nodePos = {};
  for (const n of nodes) {
    const dn = g.node(n.id) || {};
    const { w, h } = sizes[n.id];
    const cx = typeof dn.x === 'number' ? dn.x : 0;
    const cy = typeof dn.y === 'number' ? dn.y : 0;
    nodePos[n.id] = { x: Math.round(cx - w / 2), y: Math.round(cy - h / 2), w, h };
  }

  const layoutGroups = groups.map((grp) => _buildGroupFrame(grp, nodePos));

  const gg = g.graph() || {};
  const fallback = _boundsFrom(nodePos);
  const bounds = {
    w: Math.round(typeof gg.width === 'number' ? gg.width : fallback.w),
    h: Math.round(typeof gg.height === 'number' ? gg.height : fallback.h),
  };

  return { nodes: nodePos, groups: layoutGroups, bounds };
}
