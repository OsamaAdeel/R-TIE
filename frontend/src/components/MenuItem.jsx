import clsx from 'clsx';

// Shared dropdown menu row — used by the per-conversation menu (Sidebar
// ConvRow) and the settings menu (SettingsMenu). `danger` paints the
// destructive (burgundy) treatment.
export default function MenuItem({ icon, onClick, danger, disabled, children }) {
  return (
    <button
      role="menuitem"
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={clsx(
        'w-full flex items-center gap-2.5 px-3 py-2 text-[13px] font-medium tracking-tight text-left transition-colors',
        disabled && 'opacity-40 cursor-not-allowed',
        danger
          ? 'text-burgundy hover:bg-burgundy/10'
          : 'text-ivory hover:bg-hover',
      )}
      style={{ fontFamily: 'var(--font-sans)', fontFeatureSettings: "'cv11', 'ss01'" }}
    >
      <span className={clsx('shrink-0', danger ? 'text-burgundy/80' : 'text-ivory-faint')}>{icon}</span>
      <span>{children}</span>
    </button>
  );
}
