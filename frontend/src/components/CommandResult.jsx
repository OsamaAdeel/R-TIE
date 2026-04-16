import { Terminal, CheckCircle, AlertCircle, XCircle } from 'lucide-react';

export default function CommandResult({ result, correlationId }) {
  if (!result) return null;

  const statusIcon = {
    refreshed: <CheckCircle size={14} className="text-success" />,
    completed: <CheckCircle size={14} className="text-success" />,
    ok: <CheckCircle size={14} className="text-success" />,
    cached: <CheckCircle size={14} className="text-success" />,
    cleared: <AlertCircle size={14} className="text-warning" />,
    not_found: <AlertCircle size={14} className="text-warning" />,
    not_cached: <AlertCircle size={14} className="text-text-muted" />,
    error: <XCircle size={14} className="text-error" />,
  };

  return (
    <div className="bg-bg-secondary border-2 border-border rounded-2xl rounded-bl-sm overflow-hidden shadow-sm">
      {/* Header */}
      <div className="flex items-center gap-2 px-5 py-3 border-b border-border bg-purple-light">
        <Terminal size={14} className="text-purple" />
        <span className="text-xs font-bold text-purple uppercase tracking-wide">Command Result</span>
        <span className="ml-auto text-xs font-medium text-text-secondary bg-white px-2 py-0.5 rounded-full">{result.status}</span>
        {statusIcon[result.status]}
      </div>

      {/* Body */}
      <div className="px-5 py-4">
        {/* Schema diff report */}
        {result.report && (
          <pre className="text-sm text-text-primary whitespace-pre-wrap font-mono bg-bg-tertiary rounded-xl p-4 mb-3 border border-border">
            {result.report}
          </pre>
        )}

        {/* Key-value pairs */}
        <div className="space-y-2">
          {Object.entries(result)
            .filter(([k]) => !['status', 'report'].includes(k))
            .map(([key, value]) => (
              <div key={key} className="flex items-start gap-2 text-sm">
                <span className="text-text-muted min-w-[120px] shrink-0 font-medium">{key}:</span>
                <span className="text-text-primary break-all">
                  {typeof value === 'object' ? (
                    <pre className="text-xs bg-bg-tertiary rounded-lg p-3 mt-1 overflow-x-auto border border-border">
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
        <div className="px-5 py-2.5 border-t border-border bg-bg-tertiary">
          <span className="text-xs text-text-muted font-mono">Correlation: {correlationId}</span>
        </div>
      )}
    </div>
  );
}
