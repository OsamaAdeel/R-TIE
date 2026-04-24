import { useState } from 'react';
import {
  ShieldCheck,
  ChevronDown,
  ChevronRight,
  Database,
  GitBranch,
  BookOpen,
  Hash,
  Zap,
} from 'lucide-react';
import clsx from 'clsx';
import CodeBlock from './CodeBlock';
import CallTree from './CallTree';

export default function ResponseCard({ data }) {
  if (!data || !data.explanation) {
    return (
      <div className="bg-bg-secondary border-2 border-border rounded-2xl rounded-bl-sm px-5 py-4 shadow-sm">
        <p className="text-sm text-text-secondary">
          {data?.message || 'No explanation available.'}
        </p>
      </div>
    );
  }

  const { explanation, confidence, object_name, object_type, schema, cache_hit, source_citations, correlation_id } = data;

  return (
    <div className="bg-bg-secondary border-2 border-border rounded-2xl rounded-bl-sm overflow-hidden shadow-sm">
      {/* Header */}
      <div className="flex items-center gap-3 px-5 py-3.5 border-b border-border bg-gradient-to-r from-accent-soft to-bg-secondary">
        <div className="flex items-center gap-2 flex-1 min-w-0">
          <div className="w-7 h-7 rounded-lg bg-accent/10 flex items-center justify-center">
            <Database size={14} className="text-accent" />
          </div>
          <span className="font-mono text-sm font-bold text-text-primary truncate">
            {schema}.{object_name}
          </span>
          <span className="text-[10px] font-semibold px-2 py-0.5 rounded-full bg-accent-light text-accent uppercase tracking-wide">
            {object_type}
          </span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {cache_hit && (
            <span className="text-xs font-medium px-2.5 py-1 rounded-full bg-info-light text-info">
              <Zap size={10} className="inline mr-1" />cached
            </span>
          )}
          <ConfidenceBadge confidence={confidence} />
        </div>
      </div>

      {/* Summary */}
      <div className="px-5 py-4 border-b border-border">
        <p className="text-sm text-text-primary leading-relaxed">{explanation.summary}</p>
      </div>

      {/* Sections */}
      <div className="divide-y divide-border">
        {explanation.step_by_step?.length > 0 && (
          <CollapsibleSection
            title="Step-by-Step Breakdown"
            icon={<BookOpen size={14} className="text-accent" />}
            count={explanation.step_by_step.length}
            defaultOpen
          >
            <div className="space-y-4">
              {explanation.step_by_step.map((step, i) => (
                <div key={i} className="flex gap-3">
                  <div className="w-7 h-7 rounded-full bg-gradient-to-br from-accent to-blue-500 flex items-center justify-center shrink-0 mt-0.5 shadow-sm">
                    <span className="text-xs font-bold text-white">{step.step || i + 1}</span>
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-text-primary font-medium">{step.description}</p>
                    {step.lines?.length > 0 && (
                      <p className="text-xs text-text-muted mt-1 font-mono">
                        Lines: {step.lines.join(', ')}
                      </p>
                    )}
                    {step.code_snippet && (
                      <CodeBlock code={step.code_snippet} language="sql" />
                    )}
                  </div>
                </div>
              ))}
            </div>
          </CollapsibleSection>
        )}

        {explanation.formulas?.length > 0 && (
          <CollapsibleSection title="Formulas" icon={<Hash size={14} className="text-purple" />} count={explanation.formulas.length}>
            <div className="space-y-3">
              {explanation.formulas.map((f, i) => (
                <div key={i} className="bg-purple-light rounded-xl p-4 border border-purple/10">
                  <p className="text-sm font-semibold text-text-primary mb-2">{f.name}</p>
                  <code className="text-sm text-purple block bg-white rounded-lg p-3 my-2 font-mono border border-purple/10">
                    {f.formula}
                  </code>
                  {f.lines?.length > 0 && (
                    <p className="text-xs text-text-muted font-mono">Lines: {f.lines.join(', ')}</p>
                  )}
                  {f.variables && Object.keys(f.variables).length > 0 && (
                    <div className="mt-2 space-y-1">
                      {Object.entries(f.variables).map(([k, v]) => (
                        <p key={k} className="text-xs text-text-secondary">
                          <span className="font-mono font-semibold text-purple">{k}</span> — {v}
                        </p>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </CollapsibleSection>
        )}

        {explanation.dependencies_used?.length > 0 && (
          <CollapsibleSection title="Dependencies" icon={<GitBranch size={14} className="text-info" />} count={explanation.dependencies_used.length}>
            <div className="space-y-2">
              {explanation.dependencies_used.map((dep, i) => (
                <div key={i} className="flex items-start gap-3 bg-info-light rounded-xl p-3 border border-info/10">
                  <span className="font-mono text-xs text-info font-bold shrink-0 bg-white px-2 py-0.5 rounded-md">{dep.name}</span>
                  <span className="text-xs text-text-secondary flex-1">{dep.purpose}</span>
                  {dep.called_at_lines?.length > 0 && (
                    <span className="text-xs text-text-muted ml-auto shrink-0 font-mono">
                      L{dep.called_at_lines.join(', L')}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </CollapsibleSection>
        )}

        {explanation.regulatory_refs?.length > 0 && (
          <CollapsibleSection title="Regulatory References" icon={<ShieldCheck size={14} className="text-success" />}>
            <ul className="space-y-1.5">
              {explanation.regulatory_refs.map((ref, i) => (
                <li key={i} className="text-sm text-text-secondary flex items-start gap-2 bg-success-light rounded-lg px-3 py-2 border border-success/10">
                  <ShieldCheck size={12} className="text-success mt-0.5 shrink-0" />
                  {ref}
                </li>
              ))}
            </ul>
          </CollapsibleSection>
        )}

      </div>

      {/* Footer */}
      <div className="px-5 py-3 border-t border-border bg-bg-tertiary flex items-center justify-between text-xs text-text-muted">
        <span className="font-mono">Correlation: {correlation_id}</span>
        <span className="font-medium">{source_citations?.length || 0} source citations</span>
      </div>
    </div>
  );
}

function ConfidenceBadge({ confidence }) {
  const pct = Math.round((confidence || 0) * 100);
  const color = pct >= 85 ? 'bg-success-light text-success' : pct >= 70 ? 'bg-warning-light text-warning' : 'bg-error-light text-error';
  return (
    <span className={clsx('text-xs font-mono font-bold px-2.5 py-1 rounded-full', color)}>
      {pct}%
    </span>
  );
}

function CollapsibleSection({ title, icon, children, count, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-5 py-3 text-sm text-text-secondary hover:text-text-primary hover:bg-bg-tertiary/50 transition-all duration-200"
      >
        {icon}
        <span className="font-semibold">{title}</span>
        {count && (
          <span className="text-[10px] font-bold bg-bg-tertiary text-text-muted px-1.5 py-0.5 rounded-full">{count}</span>
        )}
        <span className="ml-auto">
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
      </button>
      {open && <div className="px-5 pb-4">{children}</div>}
    </div>
  );
}
