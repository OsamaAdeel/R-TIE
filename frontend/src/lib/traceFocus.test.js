// W151 diagram polish — hover-focus state helpers (Item 3).
import { describe, it, expect } from 'vitest';
import { edgeFocus, isNodeDimmed, isEdgeFocused, isEdgeDimmed } from './traceFocus.js';

const EDGES = [
  { id: 'e1', from: 'A', to: 'T' },
  { id: 'e2', from: 'B', to: 'T' },
  { id: 'e3', from: 'C', to: 'A' },
];

describe('edgeFocus', () => {
  it('returns null when nothing is hovered', () => {
    expect(edgeFocus(EDGES, null)).toBeNull();
    expect(edgeFocus(EDGES, undefined)).toBeNull();
  });

  it('returns null for an id that matches no edge', () => {
    expect(edgeFocus(EDGES, 'nope')).toBeNull();
  });

  it('resolves the hovered edge to its two endpoint nodes', () => {
    const f = edgeFocus(EDGES, 'e1');
    expect(f.edgeId).toBe('e1');
    expect([...f.nodeIds].sort()).toEqual(['A', 'T']);
  });

  it('tolerates a missing/empty edge list', () => {
    expect(edgeFocus(undefined, 'e1')).toBeNull();
    expect(edgeFocus([], 'e1')).toBeNull();
  });
});

describe('focus predicates', () => {
  it('no focus → nothing dimmed, nothing focused', () => {
    for (const id of ['A', 'B', 'C', 'T']) expect(isNodeDimmed(null, id)).toBe(false);
    for (const e of EDGES) {
      expect(isEdgeFocused(null, e.id)).toBe(false);
      expect(isEdgeDimmed(null, e.id)).toBe(false);
    }
  });

  it('emphasizes the hovered edge + its endpoints, dims the rest', () => {
    const f = edgeFocus(EDGES, 'e1'); // A → T

    // endpoints stay lit, other nodes dim
    expect(isNodeDimmed(f, 'A')).toBe(false);
    expect(isNodeDimmed(f, 'T')).toBe(false);
    expect(isNodeDimmed(f, 'B')).toBe(true);
    expect(isNodeDimmed(f, 'C')).toBe(true);

    // hovered edge focused, others dimmed
    expect(isEdgeFocused(f, 'e1')).toBe(true);
    expect(isEdgeDimmed(f, 'e1')).toBe(false);
    expect(isEdgeFocused(f, 'e2')).toBe(false);
    expect(isEdgeDimmed(f, 'e2')).toBe(true);
  });
});
