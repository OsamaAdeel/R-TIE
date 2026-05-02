import { useState } from 'react';
import { ChevronRight, Check, Loader2, AlertTriangle } from 'lucide-react';
import clsx from 'clsx';

// Two modes:
//   • Streaming → single live "thinking" line. Spinner + the current
//     stage's verb-phrase (or the live SSE message). When the active
//     stage changes, the text re-mounts via React `key` to retrigger
//     the fade-up animation, so it reads as in-place replacement.
//   • Done → small collapsible disclosure with the completed step log,
//     for power users who want to inspect what the agent did. Collapsed
//     by default so the answer stays the visual focus.
export default function AgentActivity({ steps, sourceCount }) {
  const active = steps.find((s) => s.state === 'active');
  const startedSteps = steps.filter((s) => s.state);
  const finished = startedSteps.length > 0 && !active;

  if (active) return <ThinkingLine step={active} />;
  if (finished) return <CompletedDisclosure steps={startedSteps} sourceCount={sourceCount} />;
  return null;
}

function ThinkingLine({ step }) {
  const phrase = step.liveDetail || step.label;
  return (
    <div className="flex items-center gap-2.5 mb-3 text-[13px] text-ivory-dim">
      <Loader2 size={14} className="text-gold animate-spin shrink-0" />
      {/* `key` flips on phrase change → React remounts the span → CSS
          animation runs again. Old text is replaced by new in place. */}
      <span key={phrase} className="rtie-row-appear truncate">
        {phrase}
      </span>
    </div>
  );
}

function CompletedDisclosure({ steps, sourceCount }) {
  const [open, setOpen] = useState(false);
  const total = steps.length;
  return (
    <div className="rounded-[10px] border border-line bg-panel/60 mb-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="w-full flex items-center gap-2 px-3 py-2 text-left text-ivory-dim hover:text-ivory transition-colors"
      >
        <ChevronRight size={12} className={clsx('shrink-0 transition-transform', open && 'rotate-90')} />
        <span className="text-[12px] font-medium text-ivory">Agent activity</span>
        <span className="ml-auto text-[11px] text-ivory-faint">
          {total} step{total === 1 ? '' : 's'}
          {typeof sourceCount === 'number' && sourceCount > 0 && ` · ${sourceCount} source${sourceCount === 1 ? '' : 's'}`}
        </span>
      </button>
      {open && (
        <div className="px-3 pb-3 pt-0.5 space-y-1">
          {steps.map((s) => <CompletedRow key={s.key} step={s} />)}
        </div>
      )}
    </div>
  );
}

function CompletedRow({ step }) {
  const isWarn = step.state === 'warn';
  return (
    <div className="flex items-center gap-2.5 text-[12.5px] py-1">
      <span className="shrink-0 w-4 h-4 grid place-items-center">
        {isWarn ? (
          <AlertTriangle size={12} className="text-amber" />
        ) : (
          <Check size={12} className="text-emerald" strokeWidth={2.5} />
        )}
      </span>
      <span className={clsx('font-medium tracking-tight', isWarn ? 'text-amber' : 'text-ivory-dim')}>
        {step.label}
      </span>
    </div>
  );
}
