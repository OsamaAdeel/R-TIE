// Collision-flip placement for the portaled conversation menu.
import { describe, it, expect } from 'vitest';
import { computeMenuPlacement } from './menuPlacement.js';

const VP = { viewportH: 800, viewportW: 1200 };
const rect = (top, { h = 20, right = 250 } = {}) => ({ top, bottom: top + h, left: right - 24, right });

describe('computeMenuPlacement', () => {
  it('opens DOWN for a conversation near the top (room below)', () => {
    const p = computeMenuPlacement({ buttonRect: rect(60), ...VP });
    expect(p.placement).toBe('down');
    expect(p.top).toBe(60 + 20 + 4); // bottom + gap
  });

  it('flips UP for a conversation near the bottom (clipped below)', () => {
    // button bottom at 790 in an 800px viewport → no room below, room above
    const p = computeMenuPlacement({ buttonRect: rect(770), ...VP, menuH: 132 });
    expect(p.placement).toBe('up');
    expect(p.top).toBe(770 - 132 - 4); // top - menuH - gap
  });

  it('stays DOWN when below is tight but above is even tighter', () => {
    // tiny viewport, button near top: spaceBelow small but spaceAbove smaller
    const p = computeMenuPlacement({ buttonRect: rect(10), viewportH: 120, viewportW: 1200, menuH: 132 });
    expect(p.placement).toBe('down');
  });

  it('right-aligns to the button and clamps within the viewport', () => {
    const p = computeMenuPlacement({ buttonRect: rect(60, { right: 250 }), ...VP, menuW: 170 });
    expect(p.left).toBe(250 - 170);          // right edge aligned to button.right
    expect(p.left).toBeGreaterThanOrEqual(8); // within margin
  });

  it('clamps left to the margin when the button sits near the left edge', () => {
    const p = computeMenuPlacement({ buttonRect: rect(60, { right: 40 }), ...VP, menuW: 170 });
    expect(p.left).toBe(8); // would be negative; clamped to margin
  });
});
