// ============================================================================
// Cited trace diagram (custom React + SVG path) — W151 Phase 4.
//
// Relocated from the _proto_trace sandbox into the live app. The trust-critical
// rendering — groundingGuard and the Edges/bezier dispatch — is VERBATIM from
// the validated prototype; only the surrounding chrome changed: the standalone
// page wrapper, the adversarial toolbar (downgrade / forceSolid), and the
// legend are gone. There is NO client-side grounding override: `forceSolid` is
// hardcoded false and the prototype's `downgrade` data-transform is removed, so
// trust flows straight from the payload's grounding.
//
// The trust contract is enforced IN CODE here (see groundingGuard / Edges): a
// solid edge is impossible to draw unless grounding === "VERIFIED". The
// renderer is dumb — it draws what grounding says and never infers it.
// ============================================================================
import { useMemo, useState } from 'react';
import clsx from 'clsx';
import { computeLayout } from '../lib/layout.js';
import {
  Check, AlertTriangle, Database,
  FunctionSquare, Filter, GitBranch, Target, FileCode,
} from 'lucide-react';

// --- design tokens are inherited from ../index.css (role-based: ivory = text,
//     gold = accent, emerald/amber/burgundy = status). We never hard-code hex;
//     we read the same CSS vars the live app uses. -------------------------
const VAR = (name) => `var(--color-${name})`;

// ----------------------------------------------------------------------------
// THE TRUST GUARANTEE, IN CODE.  (VERBATIM — do not alter.)
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
// adversarial toggle (always false in the app): it tries to push EVERY edge
// through the solid path to prove the guard — unverified edges throw, get
// caught, and fall back to dashed with a "GUARD BLOCKED" marker.
//
// The grounding dispatch below is VERBATIM. The only addition is a cosmetic
// +/−/= operand label (edge.label, present on derivation-dag edges) rendered
// at the edge midpoint — it never touches the solid/dashed decision.
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
            {/* W151 Phase 4: cosmetic operand sign for derivation-dag edges
                (+/−/=). Additive only — does NOT affect the solid/dashed
                grounding decision above. Skipped when an ungrounded "?" disc
                already occupies the midpoint. */}
            {edge.label && !edge.ungroundedGap && (
              <g>
                <rect x={mid.x - 9} y={mid.y - 9} width="18" height="18" rx="4"
                  fill={VAR('ink')} stroke={VAR(color)} strokeWidth="1.25" opacity="0.95" />
                <text x={mid.x} y={mid.y + 4} textAnchor="middle" fontSize="12" fontWeight="700" fill={VAR(color)}>
                  {edge.label}
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
// NODE (HTML, absolutely positioned). Styling keys off kind + grounding
// (VERBATIM). Phase 4 adds click-to-select (additive) so the citation excerpt
// can expand below the canvas — it does not touch the grounding-driven style.
// ----------------------------------------------------------------------------
const KIND_ICON = {
  'target-column': Target,
  'derived-column': FunctionSquare,
  'source-table': Database,
  'filter': Filter,
  'absent-filter': GitBranch,
};

function Node({ node, dimmed, selected, onSelect }) {
  const verified = node.citation?.grounding === 'VERIFIED';
  const ghost = node.kind === 'absent-filter';
  const Icon = KIND_ICON[node.kind] || Database;
  const { function: fn, lines } = node.citation || {};
  const lineLabel = lines && (lines[0] || lines[1]) ? `${fn} [${lines[0]}–${lines[1]}]` : fn;

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onSelect?.(node.id)}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect?.(node.id); } }}
      className={clsx(
        'absolute rounded-[10px] px-3 py-2 text-left transition-colors cursor-pointer',
        ghost
          ? 'border border-dashed border-line-strong bg-transparent'
          : verified
            ? 'border border-emerald/55 bg-emerald/5'
            : 'border border-amber/45 bg-amber/[0.04]',
        node.isDivergence && !ghost && 'ring-2 ring-violet/70 ring-offset-2 ring-offset-[var(--color-ink)]',
        selected && !node.isDivergence && 'ring-2 ring-gold/70 ring-offset-2 ring-offset-[var(--color-ink)]',
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
// Every rectangle is COMPUTED by computeLayout from the members' laid-out
// positions (bounding boxes), not hand-placed. (VERBATIM from Phase 2.)
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
// CITATION EXCERPT (Q3) — click a node to expand its bounded citation.text
// (already in the payload) below the canvas. NO fetch: the embedded excerpt is
// what renders; overflow beyond the line cap is marked and owned by Phase 5
// (/v1/source). Uses the app's expand/close idiom.
// ----------------------------------------------------------------------------
function CitationPanel({ node, onClose }) {
  const cit = node.citation || {};
  const { function: fn, lines, text, truncated, grounding } = cit;
  const lineLabel = lines && (lines[0] || lines[1]) ? `${fn} [${lines[0]}–${lines[1]}]` : fn;

  return (
    <div className="mt-2 rounded-[10px] border border-line bg-panel-2/60">
      <div className="flex items-center gap-2 px-3 py-2">
        <FileCode size={12} className="shrink-0 text-ivory-faint" />
        <span className="font-mono text-[11.5px] font-semibold text-ivory truncate" title={lineLabel}>{lineLabel}</span>
        <GroundingPill grounding={grounding} />
        <button
          type="button"
          onClick={onClose}
          className="ml-auto text-[11px] text-ivory-faint hover:text-ivory transition-colors"
        >
          Close
        </button>
      </div>
      {text ? (
        <pre className="px-3 pb-2 text-[11.5px] font-mono text-ivory-dim whitespace-pre-wrap break-words leading-relaxed">{text}</pre>
      ) : (
        <div className="px-3 pb-2 text-[11.5px] text-ivory-faint italic">
          No source excerpt in the payload for this element.
        </div>
      )}
      {truncated && (
        <div className="px-3 pb-3 text-[10.5px] text-amber">
          Excerpt truncated to the embedded line cap — full source in a later phase.
        </div>
      )}
    </div>
  );
}

// ----------------------------------------------------------------------------
// MAIN — lean app embed. Consumes the assembler's payload verbatim
// ({nodes, edges, groups, target, trace_kind}); no toolbar, no page chrome, no
// client-side grounding override. Sized to the computed layout bounds and
// horizontally scrollable so a wide fan-in never breaks the chat column.
// ----------------------------------------------------------------------------
export default function CitedTraceDiagram({ data }) {
  const [selectedId, setSelectedId] = useState(null);

  // Stable references (data is set once at `done`), so the layout memo below
  // doesn't recompute every render — and keeps react-hooks/exhaustive-deps
  // happy about the `|| []` fallbacks.
  const { nodes, edges, groups } = useMemo(() => ({
    nodes: data?.nodes || [],
    edges: data?.edges || [],
    groups: data?.groups || [],
  }), [data]);

  // topology IN, coordinates OUT. computeLayout is trust-blind.
  const layout = useMemo(
    () => computeLayout({ nodes, edges, groups }),
    [nodes, edges, groups],
  );

  // Merge computed rects onto the nodes so Edges/Node read pos/w/h exactly as
  // in the prototype — the edge layer and groundingGuard dispatch are untouched.
  const positioned = useMemo(() => nodes.map((n) => {
    const r = layout.nodes[n.id] || { x: 0, y: 0, w: 200, h: 50 };
    return { ...n, pos: { x: r.x, y: r.y }, w: r.w, h: r.h };
  }), [nodes, layout]);

  const selected = useMemo(
    () => positioned.find((n) => n.id === selectedId) || null,
    [positioned, selectedId],
  );

  if (!nodes.length) return null;

  const kindLabel = data?.trace_kind === 'derivation-dag' ? 'Derivation' : 'Fan-in';

  return (
    <div className="mt-3">
      <div className="mb-1.5 flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-ivory-faint">
        <GitBranch size={11} className="text-gold" />
        <span>Trace diagram · {kindLabel} →</span>
        <span className="font-mono text-gold normal-case tracking-normal">{data?.target}</span>
      </div>
      {/* canvas — fixed to the computed bounds, scrolls horizontally if wide */}
      <div className="overflow-x-auto rounded-[12px] border border-line bg-panel/40">
        <div className="relative" style={{ height: layout.bounds.h, width: layout.bounds.w }}>
          <LayoutFrames groups={layout.groups} />
          <Edges nodes={positioned} edges={edges} forceSolid={false}
            width={layout.bounds.w} height={layout.bounds.h} />
          {positioned.map((n) => (
            <Node
              key={n.id}
              node={n}
              selected={n.id === selectedId}
              onSelect={(id) => setSelectedId((cur) => (cur === id ? null : id))}
            />
          ))}
        </div>
      </div>
      {selected && <CitationPanel node={selected} onClose={() => setSelectedId(null)} />}
    </div>
  );
}
