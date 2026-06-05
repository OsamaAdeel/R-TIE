import { describe, it, expect } from 'vitest';
import { buildPipelineSteps } from './pipelineSteps.js';

const stateOf = (steps, key) => steps.find((s) => s.key === key)?.state;

describe('buildPipelineSteps', () => {
  it('marks the current stage active while streaming', () => {
    const steps = buildPipelineSteps({
      stage: { stage: 'search' }, data: null, streaming: true, loading: false,
    });
    expect(stateOf(steps, 'classify')).toBe('done');
    expect(stateOf(steps, 'search')).toBe('active');
    expect(stateOf(steps, 'fetch')).toBe('');
  });

  it('marks all steps done when finished and validated', () => {
    const steps = buildPipelineSteps({
      stage: { stage: 'explain' }, data: { validated: true }, streaming: false, loading: false,
    });
    expect(steps.every((s) => s.state === 'done')).toBe(true);
  });

  // Regression: clicking Stop early (before any token) leaves `stage` pointing
  // at the interrupted step. Without the cancelled guard, that step stays
  // `active` and its spinner animates forever even though loading is cleared.
  it('drops the in-progress step (no active spinner) when cancelled with no payload', () => {
    const steps = buildPipelineSteps({
      stage: { stage: 'classify' }, data: null, streaming: false, loading: false, cancelled: true,
    });
    expect(steps.some((s) => s.state === 'active')).toBe(false);
    // classify was the interrupted (active) step → not shown.
    expect(stateOf(steps, 'classify')).toBe('');
  });

  it('keeps already-completed steps when cancelled mid-pipeline', () => {
    const steps = buildPipelineSteps({
      stage: { stage: 'fetch' }, data: null, streaming: false, loading: false, cancelled: true,
    });
    expect(steps.some((s) => s.state === 'active')).toBe(false);
    expect(stateOf(steps, 'classify')).toBe('done');
    expect(stateOf(steps, 'search')).toBe('done');
    expect(stateOf(steps, 'fetch')).toBe('');   // the interrupted step
    expect(stateOf(steps, 'verify')).toBe('');
  });
});
