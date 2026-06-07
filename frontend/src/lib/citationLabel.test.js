// W162 Tier 1 — citation line-label formatting.
import { describe, it, expect } from 'vitest';
import { formatLineLabel } from './citationLabel.js';

describe('formatLineLabel', () => {
  it('collapses an equal span to singular "Line N" (megaline)', () => {
    // A real OFSAA megaline write resolves to [24, 24] — must read "Line 24",
    // not "[24–24]".
    expect(formatLineLabel('CS_CAPITAL_RATIO', [24, 24])).toBe('CS_CAPITAL_RATIO Line 24');
  });

  it('renders a true multi-line span as "Lines X–Y"', () => {
    expect(formatLineLabel('FN_G_TEST_CSTM', [505, 598])).toBe('FN_G_TEST_CSTM Lines 505–598');
  });

  it('falls back to the bare function name when there is no resolved span', () => {
    expect(formatLineLabel('FN_X', [0, 0])).toBe('FN_X');
    expect(formatLineLabel('FN_X', null)).toBe('FN_X');
    expect(formatLineLabel('FN_X', undefined)).toBe('FN_X');
  });
});
