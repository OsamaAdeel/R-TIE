// ============================================================================
// PROTOTYPE — cited trace diagram (custom React + SVG path).
//
// This is a RENDER TEST, not the production feature. It exists to answer one
// yes/no question: does a hand-written React/SVG component render the trust
// model FAITHFULLY on the hard fan-in case — solid only when verified,
// alternatives unmistakably disjoint, ungrounded gaps dashed-with-"?" — and
// is that worth standardising into a grammar, or should we reach for Mermaid?
//
// The trust contract is enforced IN CODE here (see groundingGuard / SolidEdge),
// not by the fixture happening to be consistent. Flip a grounding value in the
// fixture (or via the toolbar) and the render downgrades automatically.
// ============================================================================
import { useMemo, useState } from 'react';
import clsx from 'clsx';
import { computeLayout } from './layout.js';
import {
  Check, AlertTriangle, HelpCircle, ShieldAlert, Database,
  FunctionSquare, Filter, FileWarning, GitBranch, Target,
} from 'lucide-react';

// --- design tokens are inherited from ../../src/index.css (role-based: ivory
//     = text, gold = accent, emerald/amber/burgundy = status). We never
//     hard-code hex; we read the same CSS vars the live app uses so the
//     prototype looks native. ------------------------------------------------
const VAR = (name) => `var(--color-${name})`;

// ----------------------------------------------------------------------------
// THE TRUST GUARANTEE, IN CODE.
//
// A solid edge can ONLY be produced by passing through `groundingGuard`, and
// the guard refuses any edge whose grounding is not exactly "VERIFIED". Even
// if a future caller mis-routes an unverified edge into the solid path, the
// guard throws and the dispatcher falls back to dashed + a visible "blocked"
// chip. The renderer therefore CANNOT show more certainty than the data
// carries — that property holds structurally, not by convention.
// ----------------------------------------------------------------------------
class GroundingViolation extends Error {
  constructor(edge) {
    super(`refused to draw a SOLID edge for grounding="${edge.grounding}" (${edge.id}); solid requires VERIFIED`);
    this.name = 'GroundingViolation';
    this.edge = edge;
  }
}

// Returns the stroke spec for a SOLID edge, or throws. There is no other path
// to a solid stroke in this component.
function groundingGuard(edge) {
  if (edge.grounding !== 'VERIFIED') throw new GroundingViolation(edge);
  return { dasharray: 'none', solid: true };
}

// ----------------------------------------------------------------------------
// geometry helpers — nodes are absolutely-positioned HTML; edges are real SVG
// paths drawn on a layer behind them. Anchors face whichever side points at
// the other box, so curves read naturally without a layout engine.
// ----------------------------------------------------------------------------
const rectOf = (n) => ({ x: n.pos.x, y: n.pos.y, w: n.w, h: n.h, cx: n.pos.x + n.w / 2, cy: n.pos.y + n.h / 2 });

function anchorPair(s, t, opts = {}) {
  const dx = t.cx - s.cx, dy = t.cy - s.cy;
  const horizontal = Math.abs(dx) >= Math.abs(dy);
  let sp, tp;
  if (horizontal) {
    sp = { x: dx > 0 ? s.x + s.w : s.x, y: s.cy };
    const ty = opts.tEntryY != null ? Math.max(t.y + 8, Math.min(t.y + t.h - 8, opts.tEntryY)) : t.cy;
    tp = { x: dx > 0 ? t.x : t.x + t.w, y: ty };
  } else {
    sp = { x: s.cx, y: dy > 0 ? s.y + s.h : s.y };
    tp = { x: t.cx, y: dy > 0 ? t.y : t.y + t.h };
  }
  return { sp, tp, horizontal };
}

function bezier(sp, tp, horizontal) {
  if (horizontal) {
    const mx = (sp.x + tp.x) / 2;
    return `M ${sp.x} ${sp.y} C ${mx} ${sp.y}, ${mx} ${tp.y}, ${tp.x} ${tp.y}`;
  }
  const my = (sp.y + tp.y) / 2;
  return `M ${sp.x} ${sp.y} C ${sp.x} ${my}, ${tp.x} ${my}, ${tp.x} ${tp.y}`;
}

// ----------------------------------------------------------------------------
// EDGE LAYER (SVG). Dispatches strictly on grounding. `forceSolid` is the
// adversarial toggle: it tries to push EVERY edge through the solid path to
// prove the guard — unverified edges throw, get caught, and fall back to
// dashed with a "GUARD BLOCKED" marker.
// ----------------------------------------------------------------------------
function Edges({ nodes, edges, forceSolid, width = 1240, height = 780 }) {
  const byId = useMemo(() => Object.fromEntries(nodes.map((n) => [n.id, n])), [nodes]);

  return (
    <svg className="absolute inset-0 pointer-events-none" width={width} height={height} aria-hidden>
      <defs>
        {['emerald', 'amber', 'burgundy', 'ivory-faint'].map((c) => (
          <marker key={c} id={`arrow-${c}`} viewBox="0 0 10 10" refX="9" refY="5"
            markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill={VAR(c)} />
          </marker>
        ))}
      </defs>

      {edges.map((edge) => {
        const s = byId[edge.from], t = byId[edge.to];
        if (!s || !t) return null;
        const sr = rectOf(s), tr = rectOf(t);
        const entryY = edge.kind === 'candidate-writes' ? sr.cy : null;
        const { sp, tp, horizontal } = anchorPair(sr, tr, { tEntryY: entryY });
        const d = bezier(sp, tp, horizontal);

        // --- resolve stroke STRICTLY from grounding ---------------------
        let blocked = false;
        let style;
        try {
          // Solid only via the guard. Normal dispatch only attempts solid
          // for VERIFIED; forceSolid attempts it for everything (adversarial).
          if (edge.grounding === 'VERIFIED' || forceSolid) {
            style = groundingGuard(edge); // throws unless VERIFIED
          } else {
            style = { dasharray: edge.ungroundedGap ? '2 7' : '6 5', solid: false };
          }
        } catch (err) {
          if (err instanceof GroundingViolation) {
            blocked = true;
            style = { dasharray: edge.ungroundedGap ? '2 7' : '6 5', solid: false };
          } else { throw err; }
        }

        // colour is cosmetic; certainty comes only from `solid`.
        const color = edge.grounding === 'VERIFIED' ? 'emerald'
          : edge.ungroundedGap ? 'burgundy'
          : edge.kind === 'candidate-writes' ? 'amber'
          : 'ivory-faint';

        const mid = { x: (sp.x + tp.x) / 2, y: (sp.y + tp.y) / 2 };

        return (
          <g key={edge.id}>
            <path
              d={d} fill="none"
              stroke={VAR(color)}
              strokeWidth={style.solid ? 2.4 : 1.8}
              strokeDasharray={style.dasharray}
              strokeLinecap="round"
              markerEnd={`url(#arrow-${color})`}
              opacity={style.solid ? 1 : 0.85}
            />
            {/* ungrounded gap → "?" disc at midpoint, never solid */}
            {edge.ungroundedGap && (
              <g>
                <circle cx={mid.x} cy={mid.y} r="11" fill={VAR('ink')} stroke={VAR('burgundy')} strokeWidth="1.5" strokeDasharray="2 3" />
                <text x={mid.x} y={mid.y + 4} textAnchor="middle" fontSize="13" fontWeight="700" fill={VAR('burgundy')}>?</text>
              </g>
            )}
            {/* adversarial proof: guard refused a solid render */}
            {blocked && (
              <g>
                <rect x={mid.x - 58} y={mid.y - 10} width="116" height="20" rx="5"
                  fill={VAR('ink')} stroke={VAR('burgundy')} strokeWidth="1.5" />
                <text x={mid.x} y={mid.y + 4} textAnchor="middle" fontSize="10" fontWeight="700" fill={VAR('burgundy')}>
                  GUARD BLOCKED
                </text>
              </g>
            )}
          </g>
        );
      })}
    </svg>
  );
}

// ----------------------------------------------------------------------------
// NODE (HTML, absolutely positioned). Styling keys off kind + grounding.
// ----------------------------------------------------------------------------
const KIND_ICON = {
  'target-column': Target,
  'derived-column': FunctionSquare,
  'source-table': Database,
  'filter': Filter,
  'absent-filter': GitBranch,
};

function Node({ node, dimmed }) {
  const verified = node.citation?.grounding === 'VERIFIED';
  const ghost = node.kind === 'absent-filter';
  const Icon = KIND_ICON[node.kind] || Database;
  const { function: fn, lines } = node.citation || {};
  const lineLabel = lines && (lines[0] || lines[1]) ? `${fn} [${lines[0]}–${lines[1]}]` : fn;

  return (
    <div
      className={clsx(
        'absolute rounded-[10px] px-3 py-2 text-left transition-colors',
        ghost
          ? 'border border-dashed border-line-strong bg-transparent'
          : verified
            ? 'border border-emerald/55 bg-emerald/5'
            : 'border border-amber/45 bg-amber/[0.04]',
        node.isDivergence && !ghost && 'ring-2 ring-violet/70 ring-offset-2 ring-offset-[var(--color-ink)]',
        dimmed && 'opacity-50',
      )}
      style={{ left: node.pos.x, top: node.pos.y, width: node.w, minHeight: node.h }}
    >
      <div className="flex items-center gap-1.5">
        <Icon size={13} className={clsx('shrink-0', ghost ? 'text-ivory-faint' : verified ? 'text-emerald' : 'text-amber')} />
        <span className={clsx(
          'font-mono text-[11.5px] font-semibold leading-tight break-all',
          ghost ? 'text-ivory-faint italic' : 'text-ivory',
        )}>
          {node.label}
        </span>
      </div>
      {!ghost && (
        <div className="mt-1 flex items-center gap-1.5">
          <GroundingPill grounding={node.citation?.grounding} />
          <span className="font-mono text-[9.5px] text-ivory-faint truncate" title={lineLabel}>{lineLabel}</span>
        </div>
      )}
      {node.isDivergence && (
        <span className="absolute -top-2.5 left-3 px-1.5 py-px rounded text-[8.5px] font-bold uppercase tracking-wider bg-violet text-[var(--color-ink)]">
          divergence
        </span>
      )}
    </div>
  );
}

function GroundingPill({ grounding }) {
  const ok = grounding === 'VERIFIED';
  const Icon = ok ? Check : AlertTriangle;
  return (
    <span className={clsx(
      'inline-flex items-center gap-1 px-1.5 py-px rounded text-[9px] font-bold uppercase tracking-wider',
      ok ? 'bg-emerald/15 text-emerald' : 'bg-amber/15 text-amber',
    )}>
      <Icon size={9} />{ok ? 'Verified' : 'Unverified'}
    </span>
  );
}

// ----------------------------------------------------------------------------
// REGION FRAMES — the "alternatives" frame is what makes the two candidate
// subgraphs read as DISJOINT ("pick one") rather than a branching flow.
//
// W151 Phase 2: every rectangle is now COMPUTED by computeLayout from the
// members' laid-out positions (bounding boxes), not hand-placed. Same visual
// language as the prototype; the coordinates just come from layout.
// ----------------------------------------------------------------------------
function LayoutFrames({ groups }) {
  if (!groups || !groups.length) return null;
  return (
    <>
      {groups.map((g, gi) => (
        <div key={gi}>
          {/* outer alternatives frame (bbox of all members) */}
          {g.frame && (
            <>
              <div className="absolute rounded-[12px] border border-dashed border-violet/45 bg-violet/[0.03]"
                style={{ left: g.frame.x, top: g.frame.y, width: g.frame.w, height: g.frame.h }} />
              <div className="absolute font-bold uppercase tracking-wider text-[10px] text-violet flex items-center gap-1.5"
                style={{ left: g.frame.x + 14, top: Math.max(0, g.frame.y - 18) }}>
                <GitBranch size={12} /> {g.label}
              </div>
            </>
          )}
          {/* candidate sub-cards (separate boxes = visually disjoint) */}
          {g.candidates && g.candidates.map((c, ci) => (
            <div key={ci}>
              <div className="absolute rounded-[10px] border border-line-strong bg-panel-2/40"
                style={{ left: c.frame.x, top: c.frame.y, width: c.frame.w, height: c.frame.h }} />
              <span className="absolute font-mono text-[10px] text-ivory-dim"
                style={{ left: c.frame.x + 12, top: Math.max(0, c.frame.y - 14) }}>{c.label}</span>
            </div>
          ))}
          {/* OR divider between the two disjoint candidates */}
          {g.divider && (
            <div className="absolute flex items-center justify-center rounded-full border border-violet/60 bg-[var(--color-ink)] text-violet font-bold text-[10px]"
              style={{ left: g.divider.x - 14, top: g.divider.y - 14, width: 28, height: 28 }}>OR</div>
          )}
          {/* divergence annotation at the midpoint of the diverging nodes */}
          {g.divergence && (
            <span className="absolute text-[9px] font-bold text-violet bg-[var(--color-ink)] px-1"
              style={{ left: g.divergence.x, top: g.divergence.y }}>≠ diverges here</span>
          )}
        </div>
      ))}
    </>
  );
}

// ----------------------------------------------------------------------------
// MAIN
// ----------------------------------------------------------------------------
// Initial toggle state can be seeded from the URL (?downgrade=1&force=1) so the
// three trust states can be captured headlessly; the checkboxes still drive it.
const qp = typeof window !== 'undefined' ? new URLSearchParams(window.location.search) : new URLSearchParams();

export default function CitedTraceDiagram({ data }) {
  const [downgrade, setDowngrade] = useState(qp.get('downgrade') === '1');   // flip CITED edge → UNVERIFIED
  const [forceSolid, setForceSolid] = useState(qp.get('force') === '1');     // adversarial: try solid on all

  // Apply the toolbar downgrade as a data transform — the renderer below is
  // untouched; it simply re-reads grounding. (Proof: trust flows from data.)
  const edges = useMemo(() => data.edges.map((e) =>
    downgrade && e.id === 'e_verified' ? { ...e, grounding: 'UNVERIFIED', citation: { ...e.citation, grounding: 'UNVERIFIED' } } : e
  ), [data.edges, downgrade]);

  const nodes = useMemo(() => data.nodes.map((n) =>
    downgrade && n.id === 'CITED_WRITER' ? { ...n, citation: { ...n.citation, grounding: 'UNVERIFIED' } } : n
  ), [data.nodes, downgrade]);

  // W151 Phase 2: topology IN, coordinates OUT. computeLayout is trust-blind —
  // grounding flips (downgrade) don't change topology, so layout is stable.
  const layout = useMemo(
    () => computeLayout({ nodes, edges, groups: data.groups || [] }),
    [nodes, edges, data.groups],
  );

  // Merge computed rects onto the nodes so Edges/Node read pos/w/h exactly as
  // before — the edge layer and groundingGuard dispatch are untouched.
  const positioned = useMemo(() => nodes.map((n) => {
    const r = layout.nodes[n.id] || { x: 0, y: 0, w: 200, h: 50 };
    return { ...n, pos: { x: r.x, y: r.y }, w: r.w, h: r.h };
  }), [nodes, layout]);

  const verifiedCount = edges.filter((e) => e.grounding === 'VERIFIED').length;

  return (
    <div className="rtie-chat-wash min-h-screen w-full text-ivory p-6">
      <div className="max-w-[1300px] mx-auto">
        {/* header */}
        <div className="mb-4">
          <h1 className="font-display text-xl font-bold text-ivory">Cited trace — fan-in to <span className="font-mono text-gold">{data.target}</span></h1>
          <p className="text-[13px] text-ivory-dim mt-1 max-w-3xl">
            Prototype render test. One trace, three trust states. The renderer draws what each element's
            <span className="text-emerald font-semibold"> grounding</span> says and never infers it — a solid
            edge is physically impossible to draw unless <span className="font-mono">grounding === "VERIFIED"</span>.
          </p>
        </div>

        {/* toolbar — the grounding-flip demonstration */}
        <div className="flex flex-wrap items-center gap-3 mb-4 p-3 rounded-[10px] border border-line bg-panel-2/60">
          <ShieldAlert size={15} className="text-gold" />
          <span className="text-[12px] font-semibold text-ivory">Trust-test controls:</span>
          <label className="flex items-center gap-1.5 text-[12px] text-ivory-dim cursor-pointer select-none">
            <input type="checkbox" checked={downgrade} onChange={(e) => setDowngrade(e.target.checked)} className="accent-[var(--color-burgundy)]" />
            Downgrade the CITED edge’s grounding → <span className="font-mono text-amber">UNVERIFIED</span>
            <span className="text-ivory-faint">(watch it go solid → dashed)</span>
          </label>
          <label className="flex items-center gap-1.5 text-[12px] text-ivory-dim cursor-pointer select-none">
            <input type="checkbox" checked={forceSolid} onChange={(e) => setForceSolid(e.target.checked)} className="accent-[var(--color-burgundy)]" />
            Adversarial: force every edge through the solid path
            <span className="text-ivory-faint">(guard should block the unverified ones)</span>
          </label>
          <span className="ml-auto text-[11px] font-mono text-ivory-faint">{verifiedCount} verified edge{verifiedCount === 1 ? '' : 's'} of {edges.length}</span>
        </div>

        {/* the diagram — canvas sized to the computed layout bounds */}
        <div className="relative rounded-[12px] border border-line bg-panel/40 overflow-hidden"
          style={{ height: layout.bounds.h, width: layout.bounds.w }}>
          <LayoutFrames groups={layout.groups} />
          <Edges nodes={positioned} edges={edges} forceSolid={forceSolid}
            width={layout.bounds.w} height={layout.bounds.h} />
          {positioned.map((n) => <Node key={n.id} node={n} />)}
        </div>

        <Legend />
      </div>
    </div>
  );
}

function Legend() {
  return (
    <div className="mt-4 flex flex-wrap gap-x-6 gap-y-2 text-[11.5px] text-ivory-dim">
      <LegendRow><svg width="34" height="10"><line x1="0" y1="5" x2="34" y2="5" stroke={VAR('emerald')} strokeWidth="2.4" /></svg> <Check size={12} className="text-emerald" /> Verified — solid (guarded in code)</LegendRow>
      <LegendRow><svg width="34" height="10"><line x1="0" y1="5" x2="34" y2="5" stroke={VAR('amber')} strokeWidth="1.8" strokeDasharray="6 5" /></svg> <AlertTriangle size={12} className="text-amber" /> Unverified candidate — dashed</LegendRow>
      <LegendRow><svg width="34" height="10"><line x1="0" y1="5" x2="34" y2="5" stroke={VAR('burgundy')} strokeWidth="1.8" strokeDasharray="2 6" /></svg> <HelpCircle size={12} className="text-burgundy" /> Ungrounded gap — dashed + “?”</LegendRow>
      <LegendRow><GitBranch size={12} className="text-violet" /> Alternatives — disjoint sub-cards, “pick one”, divergence ringed</LegendRow>
    </div>
  );
}
const LegendRow = ({ children }) => <span className="inline-flex items-center gap-1.5">{children}</span>;
