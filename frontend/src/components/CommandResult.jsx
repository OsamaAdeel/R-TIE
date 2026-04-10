import { Terminal, CheckCircle, AlertCircle, XCircle } from 'lucide-react';
import clsx from 'clsx';

export default function CommandResult({ result, correlationId }) {
  if (!result) return null;

  const statusIcon = {
    refreshed: <CheckCircle size={14} className="text-success" />,
    completed: <CheckCircle size={14} className="text-success" />,
    ok: <CheckCircle size={14} className="text-success" />,
    cached: <CheckCircle size={14} className="text-success" />,
    cleared: <CheckCircle size={14} className="text-warning" />,
    not_found: <AlertCircle size={14} className="text-warning" />,
    not_cached: <AlertCircle size={14} className="text-text-muted" />,
    error: <XCircle size={14} className="text-error" />,
  };

  return (
    <div className="bg-bg-secondary border border-border rounded-2xl rounded-bl-sm overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-border bg-bg-tertiary">
        <Terminal size={14} className="text-accent" />
        <span className="text-xs font-medium text-accent">Command Result</span>
        <span className="ml-auto text-xs text-text-muted">{result.status}</span>
        {statusIcon[result.status]}
      </div>

      {/* Body */}
      <div className="px-4 py-3">
        {/* Schema diff report */}
        {result.report && (
          <pre className="text-sm text-text-primary whitespace-pre-wrap font-mono bg-bg-tertiary rounded-lg p-3 mb-3">
            {result.report}
          </pre>
        )}

        {/* Key-value pairs */}
        <div className="space-y-1.5">
          {Object.entries(result)
            .filter(([k]) => !['status', 'report'].includes(k))
            .map(([key, value]) => (
              <div key={key} className="flex items-start gap-2 text-sm">
                <span className="text-text-muted min-w-[120px] shrink-0">{key}:</span>
                <span className="text-text-primary break-all">
                  {typeof value === 'object' ? (
                    <pre className="text-xs bg-bg-tertiary rounded p-2 mt-1 overflow-x-auto">
                      {JSON.stringify(value, null, 2)}
                    </pre>
                  ) : (
                    String(value)
                  )}
                </span>
              </div>
            ))}
        </div>
      </div>

      {/* Footer */}
      {correlationId && (
        <div className="px-4 py-2 border-t border-border">
          <span className="text-xs text-text-muted">Correlation ID: {correlationId}</span>
        </div>
      )}
    </div>
  );
}
