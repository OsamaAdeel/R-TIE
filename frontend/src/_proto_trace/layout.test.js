// W151 Phase 2 — unit tests for computeLayout (topology in, coordinates out).
// Fixtures mirror the Phase-1 assembler output shape (build_trace_diagram):
// {nodes, edges, groups} with NO geometry.
import { describe, it, expect } from 'vitest';
import { computeLayout } from './layout.js';

// fan-in → N_STD_ACCT_HEAD_AMT (cited writer + two alternatives + a gap)
const FAN_IN = {
  target: 'N_STD_ACCT_HEAD_AMT',
  trace_kind: 'fan-in',
  nodes: [
    { id: 'CITED_WRITER', label: 'CS_Regulatory_PhaseIn', kind: 'derived-column' },
    { id: 'A_OUT', label: 'cand A (filtered)', kind: 'derived-column' },
    { id: 'B_OUT', label: 'cand B (unfiltered)', kind: 'derived-column' },
    { id: 'GAP_SRC', label: 'STG_UNMAPPED_FEED', kind: 'source-table' },
    { id: 'N_STD_ACCT_HEAD_AMT', label: 'N_STD_ACCT_HEAD_AMT', kind: 'target-column' },
  ],
  edges: [
    { from: 'CITED_WRITER', to: 'N_STD_ACCT_HEAD_AMT', kind: 'writes' },
    { from: 'A_OUT', to: 'N_STD_ACCT_HEAD_AMT', kind: 'candidate-writes' },
    { from: 'B_OUT', to: 'N_STD_ACCT_HEAD_AMT', kind: 'candidate-writes' },
    { from: 'GAP_SRC', to: 'CITED_WRITER', kind: 'reads', ungroundedGap: true },
  ],
  groups: [{
    kind: 'alternative',
    label: 'ALTERNATIVE WRITERS',
    members: ['A_OUT', 'B_OUT'],
    candidates: [
      { label: 'A', nodes: ['A_OUT'] },
      { label: 'B', nodes: ['B_OUT'] },
    ],
    divergence: { between: ['A_OUT', 'B_OUT'], note: 'legal-vehicle filter' },
  }],
};

// CAP943 = CAP309 − CAP863
const CAP_DAG = {
  target: 'CAP943',
  trace_kind: 'derivation-dag',
  nodes: [
    { id: 'CAP943', label: 'CAP943', kind: 'cap-literal' },
    { id: 'CAP309', label: 'CAP309', kind: 'cap-literal' },
    { id: 'CAP863', label: 'CAP863', kind: 'cap-literal' },
  ],
  edges: [
    { from: 'CAP309', to: 'CAP943', kind: 'subtract-operand', label: '+' },
    { from: 'CAP863', to: 'CAP943', kind: 'subtract-operand', label: '−' },
  ],
  groups: [],
};

const centerX = (r) => r.x + r.w / 2;
const rightMost = (layout) =>
  Object.entries(layout.nodes).sort((a, b) => centerX(b[1]) - centerX(a[1]))[0][0];

describe('computeLayout — fan-in', () => {
  it('assigns finite {x,y,w,h} to every node', () => {
    const layout = computeLayout(FAN_IN);
    for (const n of FAN_IN.nodes) {
      const r = layout.nodes[n.id];
      expect(r).toBeDefined();
      for (const k of ['x', 'y', 'w', 'h']) {
        expect(Number.isFinite(r[k])).toBe(true);
      }
      expect(r.w).toBeGreaterThan(0);
      expect(r.h).toBeGreaterThan(0);
    }
  });

  it('places the target sink right-most (LR rank)', () => {
    const layout = computeLayout(FAN_IN);
    expect(rightMost(layout)).toBe('N_STD_ACCT_HEAD_AMT');
  });

  it('group frame bounding-box contains all member rects', () => {
    const layout = computeLayout(FAN_IN);
    const grp = layout.groups[0];
    expect(grp.frame).toBeTruthy();
    for (const id of ['A_OUT', 'B_OUT']) {
      const r = layout.nodes[id];
      expect(grp.frame.x).toBeLessThanOrEqual(r.x);
      expect(grp.frame.y).toBeLessThanOrEqual(r.y);
      expect(grp.frame.x + grp.frame.w).toBeGreaterThanOrEqual(r.x + r.w);
      expect(grp.frame.y + grp.frame.h).toBeGreaterThanOrEqual(r.y + r.h);
    }
  });

  it('produces an OR divider and a divergence anchor for two candidates', () => {
    const layout = computeLayout(FAN_IN);
    const grp = layout.groups[0];
    expect(grp.candidates).toHaveLength(2);
    expect(grp.divider).toBeTruthy();
    expect(Number.isFinite(grp.divider.x)).toBe(true);
    expect(grp.divergence).toBeTruthy();
    expect(grp.divergence.between).toEqual(['A_OUT', 'B_OUT']);
  });

  it('reports positive canvas bounds', () => {
    const { bounds } = computeLayout(FAN_IN);
    expect(bounds.w).toBeGreaterThan(0);
    expect(bounds.h).toBeGreaterThan(0);
  });

  it('is deterministic across runs', () => {
    expect(JSON.stringify(computeLayout(FAN_IN)))
      .toBe(JSON.stringify(computeLayout(FAN_IN)));
  });
});

describe('computeLayout — CAP derivation DAG', () => {
  it('places the target literal right-most and lays out both operands', () => {
    const layout = computeLayout(CAP_DAG);
    expect(rightMost(layout)).toBe('CAP943');
    for (const id of ['CAP309', 'CAP863', 'CAP943']) {
      expect(layout.nodes[id]).toBeDefined();
    }
    expect(layout.groups).toEqual([]);
    expect(layout.bounds.w).toBeGreaterThan(0);
  });
});

describe('computeLayout — robustness', () => {
  it('ignores edges referencing unknown nodes without throwing', () => {
    const topo = {
      nodes: [{ id: 'A', label: 'A', kind: 'cap-literal' }],
      edges: [{ from: 'A', to: 'GHOST' }, { from: 'GHOST', to: 'A' }],
      groups: [],
    };
    expect(() => computeLayout(topo)).not.toThrow();
    expect(computeLayout(topo).nodes.A).toBeDefined();
  });

  it('handles empty topology', () => {
    const layout = computeLayout({ nodes: [], edges: [], groups: [] });
    expect(layout.nodes).toEqual({});
    expect(layout.groups).toEqual([]);
  });
});
