// W151 Phase 4 — tests for the done-equality suppression predicate.
import { describe, it, expect } from 'vitest';
import { shouldRenderDiagram } from './diagramGate.js';

describe('shouldRenderDiagram', () => {
  it('renders when emitted and grounding equals the body badge (VERIFIED)', () => {
    expect(shouldRenderDiagram({
      diagram_emitted: true, diagram_grounding: 'VERIFIED', badge: 'VERIFIED',
    })).toBe(true);
  });

  it('renders an UNVERIFIED diagram under an UNVERIFIED badge (they agree)', () => {
    expect(shouldRenderDiagram({
      diagram_emitted: true, diagram_grounding: 'UNVERIFIED', badge: 'UNVERIFIED',
    })).toBe(true);
  });

  it('suppresses when grounding disagrees with the badge (assembler/transport bug)', () => {
    expect(shouldRenderDiagram({
      diagram_emitted: true, diagram_grounding: 'VERIFIED', badge: 'UNVERIFIED',
    })).toBe(false);
    expect(shouldRenderDiagram({
      diagram_emitted: true, diagram_grounding: 'UNVERIFIED', badge: 'VERIFIED',
    })).toBe(false);
  });

  it('suppresses when no diagram was emitted', () => {
    expect(shouldRenderDiagram({
      diagram_emitted: false, diagram_grounding: null, badge: 'VERIFIED',
    })).toBe(false);
  });

  it('suppresses when diagram_grounding is absent (e.g. no diagram event)', () => {
    expect(shouldRenderDiagram({ diagram_emitted: true, badge: 'VERIFIED' })).toBe(false);
  });

  it('suppresses on a null / empty done payload', () => {
    expect(shouldRenderDiagram(null)).toBe(false);
    expect(shouldRenderDiagram(undefined)).toBe(false);
    expect(shouldRenderDiagram({})).toBe(false);
  });
});
