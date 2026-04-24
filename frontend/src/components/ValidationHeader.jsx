import { ShieldCheck, ShieldAlert, ShieldOff, AlertTriangle } from 'lucide-react';

export default function ValidationHeader({ data }) {
  if (!data) return null;

  const { badge, validated, warnings } = data;
  const hasWarnings = Array.isArray(warnings) && warnings.length > 0;

  let badgeState = null;
  if (badge === 'VERIFIED' || badge === 'UNVERIFIED' || badge === 'DECLINED') {
    badgeState = badge;
  } else if (validated === true) {
    badgeState = 'VERIFIED';
  } else if (validated === false) {
    badgeState = 'UNVERIFIED';
  }

  if (!badgeState && !hasWarnings) return null;

  return (
    <div className="mb-3 space-y-2">
      {badgeState && <BadgePill state={badgeState} />}
      {hasWarnings && <WarningList warnings={warnings} />}
    </div>
  );
}

function BadgePill({ state }) {
  if (state === 'VERIFIED') {
    return (
      <span className="inline-flex items-center gap-1 text-xs font-semibold px-2.5 py-1 rounded-full bg-success-light text-success">
        <ShieldCheck size={11} /> Verified
      </span>
    );
  }
  if (state === 'DECLINED') {
    return (
      <span className="inline-flex items-center gap-1 text-xs font-semibold px-2.5 py-1 rounded-full bg-error-light text-error">
        <ShieldOff size={11} /> Declined
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 text-xs font-semibold px-2.5 py-1 rounded-full bg-warning-light text-warning">
      <ShieldAlert size={11} /> Unverified
    </span>
  );
}

function WarningList({ warnings }) {
  return (
    <div className="space-y-1.5">
      {warnings.map((w, i) => {
        const raw = typeof w === 'string' ? w : String(w ?? '');
        const colonIdx = raw.indexOf(':');
        const head = colonIdx > 0 ? raw.slice(0, colonIdx).trim() : '';
        const isCategory = colonIdx > 0 && /^[A-Z][A-Z0-9_]*$/.test(head);
        const category = isCategory ? head : null;
        const message = isCategory ? raw.slice(colonIdx + 1).trim() : raw;

        return (
          <div
            key={i}
            className="flex items-start gap-2 text-xs text-warning bg-warning-light rounded-lg px-3 py-2 border border-warning/15"
          >
            <AlertTriangle size={12} className="mt-0.5 shrink-0" />
            <span className="leading-relaxed">
              {category && <span className="font-mono font-semibold mr-1">{category}:</span>}
              {message}
            </span>
          </div>
        );
      })}
    </div>
  );
}
