import { useEffect } from 'react';

// ----------------------------------------------------------------------------
// Modal shell — fixed, viewport-centred, dimmed backdrop. Escape and backdrop
// click both invoke onClose. Shared by the settings menu's confirm/about
// dialogs and the conversation rename dialog so they read identically.
// ----------------------------------------------------------------------------
export default function Modal({ labelledBy, onClose, children }) {
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose?.(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby={labelledBy}
      // The modal can be mounted from inside a click-to-select row (conversation
      // rename); stop clicks here from bubbling to that row's handler.
      onClick={(e) => e.stopPropagation()}
    >
      <div className="absolute inset-0 bg-ink/70 backdrop-blur-sm" onClick={onClose} aria-hidden />
      <div className="relative z-10 w-full max-w-sm rounded-xl border border-line-strong bg-panel rtie-menu-shadow p-5">
        {children}
      </div>
    </div>
  );
}
