// Maps the four backend stage events emitted by /v1/stream
// (RTIE/src/main.py: classify | search | fetch | explain) to natural
// verb-phrase labels — "Understanding your question…", "Searching…", etc.
// The 5th step "Verifying claims" is derived from the final `validated`
// flag on the done payload (TODO(backend): emit a `verify` SSE stage so
// we can surface real timing).
const STEP_DEFS = [
  { key: 'classify', label: 'Understanding your question' },
  { key: 'search',   label: 'Searching for relevant functions' },
  { key: 'fetch',    label: 'Reading source code' },
  { key: 'explain',  label: 'Generating explanation' },
  { key: 'verify',   label: 'Verifying claims' },
];

// Build the 5-element step array from current `stage` event + final-state
// signals. Active step's `liveDetail` is replaced with the live SSE
// `stage.message` when present, so the in-progress thinking line tracks
// what the backend is actually doing in real time.
export function buildPipelineSteps({ stage, data, streaming, loading, cancelled }) {
  const currentKey = stage?.stage;
  const liveMessage = stage?.message;
  const finished = !!data && !streaming && !loading;
  const validated = data?.validated;
  const stageIdx = STEP_DEFS.findIndex((s) => s.key === currentKey);
  const activeIdx = stageIdx >= 0 ? stageIdx : (loading || streaming ? 0 : -1);

  return STEP_DEFS.map((def, i) => {
    if (def.key === 'verify') {
      if (finished) return { ...def, state: validated === false ? 'warn' : 'done' };
      return { ...def, state: '' };
    }
    if (finished) return { ...def, state: 'done' };
    // A cancelled response (Stop clicked) is terminal even without a final
    // payload. Freeze the steps that had already completed and drop the
    // in-progress one — otherwise its `active` spinner keeps animating
    // forever, since `stage` still points at the interrupted step.
    if (cancelled) return { ...def, state: i < activeIdx ? 'done' : '' };
    if (i < activeIdx) return { ...def, state: 'done' };
    if (i === activeIdx) {
      return { ...def, state: 'active', liveDetail: liveMessage || def.label };
    }
    return { ...def, state: '' };
  });
}
