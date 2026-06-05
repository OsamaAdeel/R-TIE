import { useEffect, useRef, useState } from 'react';
import { Settings, Sun, Moon, Download, Info, Trash2 } from 'lucide-react';
import clsx from 'clsx';
import MenuItem from './MenuItem';
import BrandMark from './BrandMark';
import Modal from './Modal';
import ConfirmDialog from './ConfirmDialog';
import { downloadAllSessionsAsMarkdown } from '../lib/exportMarkdown';

// Build identity. There is no real semver yet (package.json is 0.0.0), so the
// version is the git short-hash + build date, injected by Vite `define`
// (see vite.config.js). The typeof guards keep this safe under vitest, where
// the defines may be absent.
const GIT_HASH = typeof __GIT_HASH__ !== 'undefined' ? __GIT_HASH__ : 'dev';
const BUILD_DATE = typeof __BUILD_DATE__ !== 'undefined' ? __BUILD_DATE__ : '';

// Live connector state mirrors the sidebar rail — the only "connected state"
// trivially available to the client. Schema/index counts are NOT exposed to
// the frontend, so About omits them rather than show a stale number.
const CONNECTORS = [
  { key: 'oracle', name: 'Oracle DB' },
  { key: 'postgres', name: 'Postgres' },
  { key: 'redis', name: 'Redis cache' },
];
const CONN_LABEL = { ok: 'operational', degraded: 'degraded', down: 'down', unknown: 'checking…' };
function mapHealthState(raw) {
  if (raw === 'ok') return 'ok';
  if (raw === 'error') return 'down';
  return 'unknown';
}

// ----------------------------------------------------------------------------
// Settings menu — opens off the footer gear. Consolidates the theme toggle
// (moved here from the standalone footer icon), bulk export, About, and the
// destructive "Delete all chats". The delete NEVER fires on the menu click;
// it only opens a confirmation modal, and the store is cleared solely on an
// explicit Confirm.
// ----------------------------------------------------------------------------
export default function SettingsMenu({ theme, onToggleTheme, sessions, onDeleteAll, health, collapsed }) {
  const [open, setOpen] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [aboutOpen, setAboutOpen] = useState(false);
  const wrapRef = useRef(null);

  // Close the menu on outside-click / Escape (mirrors the ConvRow menu).
  useEffect(() => {
    if (!open) return;
    const onDoc = (e) => { if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false); };
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const isDark = theme === 'dark';

  const handleExportAll = () => {
    setOpen(false);
    downloadAllSessionsAsMarkdown(sessions);
  };

  // Menu click only OPENS the confirm modal — it never deletes.
  const handleDeleteClick = () => {
    setOpen(false);
    setConfirmOpen(true);
  };

  // The single path to actual deletion: explicit Confirm in the modal.
  const handleConfirmDelete = () => {
    setConfirmOpen(false);
    onDeleteAll?.();
  };

  return (
    <div ref={wrapRef} className="relative">
      <button
        type="button"
        aria-label="Settings"
        title="Settings"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={clsx(
          'w-7 h-7 grid place-items-center rounded-md transition-colors',
          open ? 'text-ivory bg-hover-strong' : 'text-ivory-faint hover:text-ivory hover:bg-hover',
        )}
      >
        <Settings size={14} />
      </button>

      {open && (
        <div
          role="menu"
          className={clsx(
            'rtie-menu-shadow absolute bottom-[calc(100%+8px)] z-30 min-w-[206px] rounded-lg border border-line-strong bg-panel py-1',
            collapsed ? 'left-0' : 'right-0',
          )}
        >
          <MenuItem icon={isDark ? <Sun size={13} /> : <Moon size={13} />} onClick={onToggleTheme}>
            {isDark ? 'Light theme' : 'Dark theme'}
          </MenuItem>
          <MenuItem icon={<Download size={13} />} onClick={handleExportAll}>
            Export all chats
          </MenuItem>
          <MenuItem icon={<Info size={13} />} onClick={() => { setOpen(false); setAboutOpen(true); }}>
            About
          </MenuItem>
          <div className="my-1 h-px bg-line" />
          <MenuItem icon={<Trash2 size={13} />} onClick={handleDeleteClick} danger>
            Delete all chats
          </MenuItem>
        </div>
      )}

      {confirmOpen && (
        <ConfirmDialog
          labelledBy="rtie-delete-all-title"
          title="Delete all chats?"
          message="This cannot be undone."
          confirmLabel="Delete"
          onCancel={() => setConfirmOpen(false)}
          onConfirm={handleConfirmDelete}
        />
      )}
      {aboutOpen && (
        <AboutModal health={health} onClose={() => setAboutOpen(false)} />
      )}
    </div>
  );
}

// Minimal About — identity, one-line purpose, build version, live connectors.
function AboutModal({ health, onClose }) {
  const version = BUILD_DATE ? `build ${GIT_HASH} · ${BUILD_DATE}` : `build ${GIT_HASH}`;
  return (
    <Modal labelledBy="rtie-about-title" onClose={onClose}>
      <div className="flex items-center gap-2.5">
        <span className="text-gold shrink-0 grid place-items-center"><BrandMark size={30} /></span>
        <div className="min-w-0">
          <h2 id="rtie-about-title" className="text-[15px] font-bold text-ivory leading-tight">
            R<span className="text-gold">-</span>TIE
          </h2>
          <p className="text-[11.5px] text-ivory-faint leading-tight">Regulatory Trace &amp; Intelligence Engine</p>
        </div>
      </div>

      <p className="mt-3 text-[12px] italic text-ivory-dim">
        “Explain every number. Trace every transformation.”
      </p>
      <p className="mt-2 text-[12px] text-ivory-dim leading-relaxed">
        Read-only AI analysis over Oracle OFSAA — tracing Basel capital
        calculations through PL/SQL with source-cited explanations.
      </p>

      <div className="mt-4 rounded-lg border border-line bg-panel-2/40 px-3 py-2.5">
        <div className="text-[10px] uppercase tracking-[0.16em] text-ivory-faint font-medium mb-1.5">
          Connections
        </div>
        <div className="flex flex-col gap-1">
          {CONNECTORS.map((c) => {
            const state = mapHealthState(health?.[c.key]);
            return (
              <div key={c.key} className="flex items-center gap-2.5 text-[12px]">
                <span className={clsx('rtie-conn-dot', `is-${state}`)} />
                <span className="text-ivory">{c.name}</span>
                <span className="ml-auto text-ivory-faint">{CONN_LABEL[state] || state}</span>
              </div>
            );
          })}
        </div>
      </div>

      <div className="mt-4 flex items-center justify-between">
        <div className="text-[11px] text-ivory-faint leading-tight">
          <div className="font-mono">{version}</div>
          <div>Built by Toheed Asghar</div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-1.5 rounded-md text-[12.5px] font-medium text-ivory bg-hover hover:bg-hover-strong transition-colors"
        >
          Close
        </button>
      </div>
    </Modal>
  );
}
