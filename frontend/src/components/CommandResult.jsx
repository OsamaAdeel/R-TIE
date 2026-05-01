import { Terminal, CheckCircle, AlertCircle, XCircle } from 'lucide-react';
import clsx from 'clsx';

// Slash-command result panel. Backend slash handlers (RTIE/src/main.py
// `_handle_command`) return arbitrary `result` dicts whose shape varies
// per command. We render `report` (when present) as a pre-formatted block
// and the rest as key/value rows.
const STATUS_META = {
  refreshed:  { tone: 'ok',    Icon: CheckCircle },
  completed:  { tone: 'ok',    Icon: CheckCircle },
  ok:         { tone: 'ok',    Icon: CheckCircle },
  cached:     { tone: 'ok',    Icon: CheckCircle },
  cleared:    { tone: 'warn',  Icon: AlertCircle },
  not_found:  { tone: 'warn',  Icon: AlertCircle },
  not_cached: { tone: 'info',  Icon: AlertCircle },
  error:      { tone: 'error', Icon: XCircle },
};

export default function CommandResult({ result, correlationId }) {
  if (!result) return null;
  const meta = STATUS_META[result.status] || { tone: 'info', Icon: Terminal };

  return (
    <div className="rounded-[10px] border border-line bg-panel/60 overflow-hidden">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-line bg-panel-2">
        <Terminal size={13} className="text-gold" />
        <span className="text-[11px] font-semibold uppercase tracking-widest text-gold">
          Command Result
        </span>
        <span className="ml-auto inline-flex items-center gap-1.5 text-[11px] text-ivory-dim">
          <span className={clsx(
            meta.tone === 'ok'    && 'text-emerald',
            meta.tone === 'warn'  && 'text-amber',
            meta.tone === 'info'  && 'text-ivory-dim',
            meta.tone === 'error' && 'text-burgundy'
          )}>
            <meta.Icon size={12} />
          </span>
          <span className="font-mono">{result.status}</span>
        </span>
      </div>

      <div className="px-4 py-3">
        {result.report && (
          <pre className="text-[12px] text-ivory whitespace-pre-wrap font-mono bg-ink-2 rounded-md p-3 mb-3 border border-line overflow-x-auto">
            {result.report}
          </pre>
        )}

        <div className="space-y-1.5">
          {Object.entries(result)
            .filter(([k]) => !['status', 'report'].includes(k))
            .map(([key, value]) => (
              <div key={key} className="flex items-start gap-2 text-[12.5px]">
                <span className="text-ivory-faint min-w-[120px] shrink-0 font-medium">{key}</span>
                <span className="text-ivory break-all">
                  {typeof value === 'object' ? (
                    <pre className="text-[11px] bg-ink-2 rounded-md p-2.5 mt-1 overflow-x-auto border border-line text-ivory-dim">
                      {JSON.stringify(value, null, 2)}
                    </pre>
                  ) : String(value)}
                </span>
              </div>
            ))}
        </div>
      </div>

      {correlationId && (
        <div className="px-4 py-2 border-t border-line bg-panel-2">
          <span className="text-[11px] text-ivory-faint font-mono">Correlation: {correlationId}</span>
        </div>
      )}
    </div>
  );
}
