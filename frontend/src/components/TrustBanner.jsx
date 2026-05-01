import { useState } from 'react';
import { Check, AlertTriangle, XCircle, Info, ChevronRight, ExternalLink } from 'lucide-react';
import clsx from 'clsx';

// Severity is inferred client-side from the warning's prefix code, since the
// backend ships warnings as a flat List[str] (RTIE/src/agents/renderer.py
// `warnings`, RTIE/src/agents/data_query.py `sanity_warnings`,
// RTIE/src/phase2/explainer.py). When the backend grows a structured
// severity field, this lookup table can be retired.
const SEVERITY_BY_CODE = {
  CONTRADICTION: 'error',
  UNGROUNDED_IDENTIFIERS: 'error',
  NAMED_FUNCTION_NOT_RETRIEVED: 'error',
  CITATIONS: 'error',
  LLM_API_ERROR: 'error',
  HALLUCINATED_FUNCTION: 'error',
  UNKNOWN_FUNCTION: 'error',
  INVENTED_NUMERIC_VALUE: 'error',
  PARTIAL_SOURCE_INDEXED: 'warn',
  RELEVANCE: 'warn',
  OUTPUT: 'warn',
  SUSPICIOUS_ZERO_RESULT: 'warn',
  FORBIDDEN_PHRASE: 'warn',
  ETL_SOURCE_NOT_MENTIONED: 'warn',
  COUNT_PRE_CHECK_FAILED: 'info',
};

// Verdict pills. REJECTED is in the design but the current backend never
// emits it (TODO(backend): confirm or drop) — kept for forward compat.
const BADGE_META = {
  VERIFIED:   { sev: 'ok',    label: 'Verified',     Icon: Check },
  REVIEW:     { sev: 'warn',  label: 'Needs review', Icon: AlertTriangle },
  UNVERIFIED: { sev: 'warn',  label: 'Unverified',   Icon: AlertTriangle },
  REJECTED:   { sev: 'error', label: 'Rejected',     Icon: XCircle },
  DECLINED:   { sev: 'error', label: 'Declined',     Icon: XCircle },
};

function parseTrustFlag(raw) {
  // Match "CODE: detail" / "code: detail" / colonless prose
  const m = String(raw).match(/^([A-Za-z][A-Za-z0-9_]*):\s*(.+)$/s);
  if (!m) {
    return { code: 'NOTE', displayCode: 'Note', severity: 'warn', message: String(raw), related: [] };
  }
  const display = m[1];
  const norm = display.toUpperCase();
  const message = m[2];
  const related = [];
  const reTick = /`([^`]+)`/g;
  const reCaps = /\b([A-Z][A-Z0-9_]{3,})\b/g;
  let mm;
  while ((mm = reTick.exec(message))) related.push(mm[1]);
  while ((mm = reCaps.exec(message))) if (!related.includes(mm[1])) related.push(mm[1]);
  return {
    code: norm,
    displayCode: display,
    severity: SEVERITY_BY_CODE[norm] || 'warn',
    message,
    related: related.slice(0, 4),
  };
}

function deriveBadge(data) {
  if (!data) return null;
  if (data.badge && BADGE_META[data.badge]) return data.badge;
  if (data.validated === true) return 'VERIFIED';
  if (data.validated === false) return 'UNVERIFIED';
  return null;
}

export default function TrustBanner({ data }) {
  const [open, setOpen] = useState(false);

  if (!data) return null;
  const badge = deriveBadge(data);
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const sanity = Array.isArray(data.sanity_warnings) ? data.sanity_warnings : [];
  const flags = [...warnings, ...sanity].map(parseTrustFlag);

  // Hide entirely on a clean Verified-with-no-flags response.
  if (!badge || (badge === 'VERIFIED' && flags.length === 0)) return null;

  const meta = BADGE_META[badge] || BADGE_META.UNVERIFIED;
  const counts = flags.reduce((a, f) => ({ ...a, [f.severity]: (a[f.severity] || 0) + 1 }), {});

  const sources = data?.source_citations || [];

  return (
    <div className={clsx(
      'rounded-[10px] border mb-3 transition-colors',
      meta.sev === 'ok'    && 'border-line-gold bg-gold-soft/40',
      meta.sev === 'warn'  && 'border-amber/40 bg-amber/5',
      meta.sev === 'error' && 'border-burgundy/50 bg-burgundy/10'
    )}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="w-full flex items-center gap-2 px-3 py-2 text-left"
      >
        <ChevronRight
          size={12}
          className={clsx('shrink-0 transition-transform text-ivory-faint', open && 'rotate-90')}
        />
        <span className={clsx(
          'shrink-0',
          meta.sev === 'ok'    && 'text-gold',
          meta.sev === 'warn'  && 'text-amber',
          meta.sev === 'error' && 'text-burgundy'
        )}>
          <meta.Icon size={13} />
        </span>
        <span className="text-[12.5px] font-semibold text-ivory">{meta.label}</span>
        {open && flags.length > 0 && (
          <>
            <span className="text-ivory-faint">·</span>
            <span className="text-[11.5px] text-ivory-dim">
              {flags.length} trust note{flags.length > 1 ? 's' : ''}
            </span>
            <span className="flex items-center gap-1.5 ml-1.5 text-[11px]">
              {counts.error && <span className="text-burgundy">{counts.error} error{counts.error > 1 ? 's' : ''}</span>}
              {counts.warn  && <span className="text-amber">{counts.warn} warn{counts.warn > 1 ? 's' : ''}</span>}
              {counts.info  && <span className="text-ivory-dim">{counts.info} info</span>}
            </span>
          </>
        )}
        {open && <span className="ml-auto text-[11px] text-ivory-faint">Hide</span>}
      </button>

      {open && (
        <ul className="px-3 pb-3 pt-1 space-y-2">
          {flags.length === 0 ? (
            <li className="flex items-start gap-2 text-[12px] text-ivory-dim">
              <Info size={12} className="mt-0.5 shrink-0 text-ivory-faint" />
              <span>
                No structured notes attached. Verdict <strong className="text-ivory">{meta.label}</strong>{' '}
                was set by the pipeline without an accompanying reason code.
              </span>
            </li>
          ) : (
            flags.map((f, i) => <FlagRow key={i} flag={f} sources={sources} />)
          )}
        </ul>
      )}
    </div>
  );
}

function FlagRow({ flag, sources }) {
  const { displayCode, severity, message, related } = flag;
  return (
    <li className="flex items-start gap-2 text-[12px]">
      <span className={clsx(
        'mt-0.5 shrink-0',
        severity === 'error' && 'text-burgundy',
        severity === 'warn'  && 'text-amber',
        severity === 'info'  && 'text-ivory-dim'
      )}>
        {severity === 'error' ? <XCircle size={12} /> : severity === 'warn' ? <AlertTriangle size={12} /> : <Info size={12} />}
      </span>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 mb-0.5">
          <span className="font-mono text-[11px] font-semibold text-ivory">{displayCode}</span>
          <span className={clsx(
            'text-[10px] uppercase tracking-wider',
            severity === 'error' && 'text-burgundy',
            severity === 'warn'  && 'text-amber',
            severity === 'info'  && 'text-ivory-faint'
          )}>
            {severity}
          </span>
        </div>
        <div className="text-ivory-dim leading-relaxed break-words">{message}</div>
        {related.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 mt-1.5">
            <span className="text-[10px] uppercase tracking-wider text-ivory-faint">related</span>
            {related.map((r, j) => {
              // Cross-ref against retrieved source citations. Backend's
              // citation shape is {line, text, context, source} — we
              // best-effort match `r` against `text`/`context`. When the
              // citation shape grows a `title`/`name` field, this gets
              // simpler. (See "backend gap B" in the migration plan.)
              const matched = sources.findIndex((s) =>
                (s.text || '').includes(r) || (s.context || '').includes(r)
              );
              return (
                <span
                  key={j}
                  className={clsx(
                    'inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10.5px] font-mono border',
                    matched >= 0
                      ? 'bg-gold-soft text-gold border-line-gold'
                      : 'bg-panel-2 text-ivory-dim border-line'
                  )}
                >
                  {r}
                  {matched >= 0 && (
                    <span className="inline-flex items-center gap-0.5">
                      <ExternalLink size={9} />source {matched + 1}
                    </span>
                  )}
                </span>
              );
            })}
          </div>
        )}
      </div>
    </li>
  );
}
