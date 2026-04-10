import { useState } from 'react';
import {
  ShieldCheck,
  ShieldAlert,
  ChevronDown,
  ChevronRight,
  Database,
  GitBranch,
  BookOpen,
  AlertTriangle,
  Hash,
  Zap,
} from 'lucide-react';
import clsx from 'clsx';
import CodeBlock from './CodeBlock';
import CallTree from './CallTree';

export default function ResponseCard({ data }) {
  if (!data || !data.explanation) {
    return (
      <div className="bg-bg-secondary border border-border rounded-2xl rounded-bl-sm px-4 py-3">
        <p className="text-sm text-text-secondary">
          {data?.message || 'No explanation available.'}
        </p>
      </div>
    );
  }

  const { explanation, confidence, validated, warnings, badge, object_name, object_type, schema, cache_hit, source_citations, correlation_id } = data;

  return (
    <div className="bg-bg-secondary border border-border rounded-2xl rounded-bl-sm overflow-hidden">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-border">
        <div className="flex items-center gap-2 flex-1 min-w-0">
          <Database size={14} className="text-accent shrink-0" />
          <span className="font-mono text-sm font-semibold text-text-primary truncate">
            {schema}.{object_name}
          </span>
          <span className="text-xs px-2 py-0.5 rounded-full bg-bg-tertiary border border-border text-text-muted">
            {object_type}
          </span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {cache_hit && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-accent/10 text-accent border border-accent/25">
              <Zap size={10} className="inline mr-1" />cached
            </span>
          )}
          <ConfidenceBadge confidence={confidence} />
          <ValidationBadge badge={badge} validated={validated} />
        </div>
      </div>

      {/* Summary */}
      <div className="px-4 py-3 border-b border-border">
        <p className="text-sm text-text-primary leading-relaxed">{explanation.summary}</p>
      </div>

      {/* Sections */}
      <div className="divide-y divide-border">
        {explanation.step_by_step?.length > 0 && (
          <CollapsibleSection
            title="Step-by-Step Breakdown"
            icon={<BookOpen size={14} />}
            defaultOpen
          >
            <div className="space-y-3">
              {explanation.step_by_step.map((step, i) => (
                <div key={i} className="flex gap-3">
                  <div className="w-6 h-6 rounded-full bg-accent/10 border border-accent/25 flex items-center justify-center shrink-0 mt-0.5">
                    <span className="text-xs font-semibold text-accent">{step.step || i + 1}</span>
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-text-primary">{step.description}</p>
                    {step.lines?.length > 0 && (
                      <p className="text-xs text-text-muted mt-1">
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
          <CollapsibleSection title="Formulas" icon={<Hash size={14} />}>
            <div className="space-y-3">
              {explanation.formulas.map((f, i) => (
                <div key={i} className="bg-bg-tertiary rounded-lg p-3">
                  <p className="text-sm font-medium text-text-primary mb-1">{f.name}</p>
                  <code className="text-sm text-accent block bg-bg-primary rounded p-2 my-2 font-mono">
                    {f.formula}
                  </code>
                  {f.lines?.length > 0 && (
                    <p className="text-xs text-text-muted">Lines: {f.lines.join(', ')}</p>
                  )}
                  {f.variables && Object.keys(f.variables).length > 0 && (
                    <div className="mt-2 space-y-1">
                      {Object.entries(f.variables).map(([k, v]) => (
                        <p key={k} className="text-xs text-text-secondary">
                          <span className="font-mono text-accent">{k}</span> — {v}
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
          <CollapsibleSection title="Dependencies" icon={<GitBranch size={14} />}>
            <div className="space-y-2">
              {explanation.dependencies_used.map((dep, i) => (
                <div key={i} className="flex items-start gap-2 bg-bg-tertiary rounded-lg p-2.5">
                  <span className="font-mono text-xs text-accent font-semibold shrink-0">{dep.name}</span>
                  <span className="text-xs text-text-secondary">{dep.purpose}</span>
                  {dep.called_at_lines?.length > 0 && (
                    <span className="text-xs text-text-muted ml-auto shrink-0">
                      L{dep.called_at_lines.join(', L')}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </CollapsibleSection>
        )}

        {explanation.regulatory_refs?.length > 0 && (
          <CollapsibleSection title="Regulatory References" icon={<ShieldCheck size={14} />}>
            <ul className="space-y-1">
              {explanation.regulatory_refs.map((ref, i) => (
                <li key={i} className="text-sm text-text-secondary flex items-start gap-2">
                  <span className="text-accent mt-1">-</span> {ref}
                </li>
              ))}
            </ul>
          </CollapsibleSection>
        )}

        {warnings?.length > 0 && (
          <CollapsibleSection title={`Warnings (${warnings.length})`} icon={<AlertTriangle size={14} className="text-warning" />}>
            <div className="space-y-1.5">
              {warnings.map((w, i) => (
                <div key={i} className="flex items-start gap-2 text-sm text-warning bg-warning/5 rounded-lg px-3 py-2">
                  <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                  <span>{w}</span>
                </div>
              ))}
            </div>
          </CollapsibleSection>
        )}
      </div>

      {/* Footer */}
      <div className="px-4 py-2.5 border-t border-border flex items-center justify-between text-xs text-text-muted">
        <span>Correlation: {correlation_id}</span>
        <span>{source_citations?.length || 0} source citations</span>
      </div>
    </div>
  );
}

function ConfidenceBadge({ confidence }) {
  const pct = Math.round((confidence || 0) * 100);
  const color = pct >= 85 ? 'text-success' : pct >= 70 ? 'text-warning' : 'text-error';
  return (
    <span className={clsx('text-xs font-mono font-semibold', color)}>
      {pct}%
    </span>
  );
}

function ValidationBadge({ badge, validated }) {
  if (badge === 'VERIFIED' || validated) {
    return (
      <span className="flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-success/10 text-success border border-success/25">
        <ShieldCheck size={10} /> Verified
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-warning/10 text-warning border border-warning/25">
      <ShieldAlert size={10} /> Unverified
    </span>
  );
}

function CollapsibleSection({ title, icon, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-4 py-2.5 text-sm text-text-secondary hover:text-text-primary hover:bg-bg-hover/50 transition-colors"
      >
        {icon}
        <span className="font-medium">{title}</span>
        <span className="ml-auto">
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
      </button>
      {open && <div className="px-4 pb-3">{children}</div>}
    </div>
  );
}
