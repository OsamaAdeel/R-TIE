import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Plus, MessageSquare, Star, Pencil, Trash2, MoreHorizontal, ChevronRight, ChevronLeft } from 'lucide-react';
import clsx from 'clsx';
import BrandMark from './BrandMark';
import MenuItem from './MenuItem';
import SettingsMenu from './SettingsMenu';
import Modal from './Modal';
import ConfirmDialog from './ConfirmDialog';
import { computeMenuPlacement } from '../lib/menuPlacement';

// Backend /health returns each connector as 'ok' | 'error'. The design uses
// four states (ok / degraded / down / unknown). 'error' maps to 'down'; we
// have no degraded signal yet (TODO(backend): richer health detail).
function mapHealthState(raw) {
  if (raw === 'ok') return 'ok';
  if (raw === 'error') return 'down';
  return 'unknown';
}

const CONN_LABEL = {
  ok: 'operational',
  degraded: 'degraded',
  down: 'down',
  unknown: 'checking…',
};

const CONNECTORS = [
  { key: 'oracle', name: 'Oracle DB' },
  { key: 'postgres', name: 'Postgres' },
  { key: 'redis', name: 'Redis cache' },
];

export default function Sidebar({
  sessions,
  activeId,
  starredIds,
  onSelect,
  onNew,
  onDelete,
  onRename,
  onStar,
  onDeleteAll,
  health,
  theme,
  onToggleTheme,
}) {
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem('rtie.sidebar.collapsed') === '1'; }
    catch { return false; }
  });

  const toggleCollapsed = () => {
    setCollapsed((c) => {
      const next = !c;
      try { localStorage.setItem('rtie.sidebar.collapsed', next ? '1' : '0'); } catch { /* ignore */ }
      return next;
    });
  };

  const starred = starredIds || new Set();
  const starredList = sessions.filter((s) => starred.has(s.id));
  const recentList = sessions.filter((s) => !starred.has(s.id));

  return (
    <aside
      className={clsx(
        'rtie-sidebar-bg h-screen flex flex-col border-r border-line relative shrink-0 transition-[width] duration-200',
        collapsed ? 'w-16' : 'w-[260px]'
      )}
    >
      {/* Header: brand + collapse toggle.
          When collapsed (64px) we stack the brand mark and the expand
          chevron vertically — they don't fit side-by-side. The brand
          mark is also clickable in that mode so the whole header acts
          as an expand target. */}
      {collapsed ? (
        <div className="flex flex-col items-center gap-2 py-3 border-b border-line">
          <button
            type="button"
            onClick={toggleCollapsed}
            aria-label="Expand sidebar"
            title="Expand sidebar"
            className="text-gold grid place-items-center w-9 h-9 rounded-md hover:bg-hover transition-colors"
          >
            <BrandMark size={32} />
          </button>
          <button
            type="button"
            onClick={toggleCollapsed}
            aria-label="Expand sidebar"
            title="Expand sidebar"
            className="w-7 h-6 grid place-items-center border border-line-strong rounded-md text-ivory-dim hover:text-ivory hover:border-line-gold hover:bg-hover transition-colors"
          >
            <ChevronRight size={14} />
          </button>
        </div>
      ) : (
        <div className="flex items-center gap-2 px-4 pt-[18px] pb-[14px] border-b border-line">
          <div className="flex items-center gap-[7px] flex-1 min-w-0">
            <span className="text-gold shrink-0 grid place-items-center w-9 h-9">
              <BrandMark size={36} />
            </span>
            <span
              className="text-ivory font-bold leading-none truncate"
              style={{
                fontFamily: 'var(--font-display)',
                fontSize: '25px',
                letterSpacing: '-0.02em',
              }}
            >
              R<span className="text-gold">-</span>TIE
            </span>
          </div>
          <button
            type="button"
            onClick={toggleCollapsed}
            aria-label="Collapse sidebar"
            title="Collapse sidebar"
            className="w-6 h-6 grid place-items-center border border-line rounded-md text-ivory-faint hover:text-ivory hover:border-line-strong hover:bg-hover transition-colors shrink-0"
          >
            <ChevronLeft size={14} />
          </button>
        </div>
      )}

      {/* New trace */}
      <button
        type="button"
        onClick={onNew}
        title={collapsed ? 'New trace (⌘K)' : ''}
        className={clsx(
          'mx-[14px] mt-[14px] mb-2 group flex items-center gap-2 rounded-[10px] border border-dashed border-line-strong px-3 py-2.5 text-[13px] font-medium text-ivory transition-colors',
          'hover:border-line-gold hover:bg-gold-soft hover:text-gold',
          collapsed && 'justify-center px-0'
        )}
      >
        <Plus size={16} className="transition-transform duration-200 [transition-timing-function:cubic-bezier(0.34,1.56,0.64,1)] group-hover:scale-125 shrink-0" />
        {!collapsed && <span>New trace</span>}
      </button>

      {/* Connector rail */}
      {!collapsed ? (
        <div className="px-[14px] pb-[14px] pt-1">
          <div className="flex items-center gap-2 px-2.5 mb-1">
            <span className="text-[10.5px] uppercase tracking-[0.16em] text-ivory-faint font-medium">Connections</span>
            <span className="flex-1 h-px bg-line" />
          </div>
          <div className="flex flex-col gap-px">
            {CONNECTORS.map((c) => {
              const state = mapHealthState(health?.[c.key]);
              return (
                <div
                  key={c.key}
                  className="flex items-center gap-2.5 px-2.5 py-1 rounded-md text-[12.5px] text-ivory-dim hover:bg-hover"
                  title={`${c.name} · ${CONN_LABEL[state] || state}`}
                >
                  <span className={clsx('rtie-conn-dot', `is-${state}`)} />
                  <span className="font-medium text-ivory">{c.name}</span>
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        <div className="flex flex-col items-center gap-2 py-2">
          {CONNECTORS.map((c) => {
            const state = mapHealthState(health?.[c.key]);
            return (
              <span
                key={c.key}
                className={clsx('rtie-conn-dot', `is-${state}`)}
                title={`${c.name} · ${CONN_LABEL[state] || state}`}
              />
            );
          })}
        </div>
      )}

      {/* Conversations: starred section + recents.
          Hidden entirely when collapsed — bare icons can't tell traces
          apart, so the rail just shows the brand, New-trace button,
          connector dots, and user chip until the user expands. */}
      {!collapsed && (
        <div className="flex-1 overflow-y-auto px-2 pb-2">
          {starredList.length > 0 && (
            <>
              <SectionLabel icon={<Star size={11} className="fill-gold text-gold" />}>Starred</SectionLabel>
              {starredList.map((s) => (
                <ConvRow
                  key={s.id}
                  session={s}
                  isActive={s.id === activeId}
                  isStarred
                  collapsed={false}
                  onSelect={onSelect}
                  onStar={onStar}
                  onRename={onRename}
                  onDelete={onDelete}
                />
              ))}
            </>
          )}
          <SectionLabel>Recents</SectionLabel>
          {recentList.map((s) => (
            <ConvRow
              key={s.id}
              session={s}
              isActive={s.id === activeId}
              isStarred={starred.has(s.id)}
              collapsed={false}
              onSelect={onSelect}
              onStar={onStar}
              onRename={onRename}
              onDelete={onDelete}
            />
          ))}
        </div>
      )}
      {/* When collapsed, soak up the vertical space so the user chip
          stays anchored to the bottom of the rail. */}
      {collapsed && <div className="flex-1" />}

      {/* User chip footer */}
      <div className={clsx(
        'border-t border-line px-3 py-3 flex gap-2',
        collapsed ? 'flex-col items-center' : 'items-center'
      )}>
        <div
          className="w-8 h-8 rounded-full grid place-items-center text-[11.5px] font-bold text-ink bg-gold shrink-0"
          title="Toheed Asghar · Risk Engineering"
        >
          TA
        </div>
        {!collapsed && (
          <div className="flex-1 min-w-0 leading-tight">
            <div className="text-ivory text-[13px] font-medium truncate">Toheed Asghar</div>
            <div className="text-ivory-faint text-[11px] truncate">Risk Engineering</div>
          </div>
        )}
        {/* Settings menu — consolidates the theme toggle (moved here from the
            standalone footer icon), bulk export, About, and the destructive
            "Delete all chats". Shown in both modes so the menu (and theme
            toggle) stays reachable when the sidebar is collapsed. */}
        <SettingsMenu
          theme={theme}
          onToggleTheme={onToggleTheme}
          sessions={sessions}
          onDeleteAll={onDeleteAll}
          health={health}
          collapsed={collapsed}
        />
      </div>
    </aside>
  );
}

function SectionLabel({ icon, children }) {
  return (
    <div className="flex items-center gap-1.5 px-3 pt-3 pb-1.5">
      {icon}
      <span className="text-[10px] uppercase tracking-widest font-semibold text-ivory-faint">
        {children}
      </span>
    </div>
  );
}

function ConvRow({ session, isActive, isStarred, collapsed, onSelect, onStar, onRename, onDelete }) {
  const [menuOpen, setMenuOpen] = useState(false);
  // Fixed viewport coords for the portaled menu (escapes the sidebar's
  // overflow-y-auto clip); recomputed each open from the trigger's rect.
  const [menuStyle, setMenuStyle] = useState(null);
  const [renaming, setRenaming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const btnRef = useRef(null);
  const menuRef = useRef(null);

  const openMenu = () => {
    const r = btnRef.current?.getBoundingClientRect();
    if (!r) return;
    setMenuStyle(computeMenuPlacement({
      buttonRect: r,
      viewportH: window.innerHeight,
      viewportW: window.innerWidth,
    }));
    setMenuOpen(true);
  };

  useEffect(() => {
    if (!menuOpen) return;
    // The menu is portaled to <body>, so "outside" means outside BOTH the
    // trigger and the menu itself.
    const onDoc = (e) => {
      if (btnRef.current?.contains(e.target)) return;
      if (menuRef.current?.contains(e.target)) return;
      setMenuOpen(false);
    };
    const onKey = (e) => { if (e.key === 'Escape') setMenuOpen(false); };
    // The fixed menu would detach from its trigger if the list scrolls or the
    // window resizes — just close it.
    const onReflow = () => setMenuOpen(false);
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    window.addEventListener('resize', onReflow);
    window.addEventListener('scroll', onReflow, true);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('resize', onReflow);
      window.removeEventListener('scroll', onReflow, true);
    };
  }, [menuOpen]);

  const handleStar = (e) => { e.stopPropagation(); setMenuOpen(false); onStar?.(session.id); };
  const handleRename = (e) => {
    e.stopPropagation();
    setMenuOpen(false);
    setRenaming(true);
  };
  const handleDelete = (e) => {
    e.stopPropagation();
    setMenuOpen(false);
    setDeleting(true);
  };

  if (collapsed) {
    return (
      <div
        onClick={() => onSelect(session.id)}
        title={session.title}
        className={clsx(
          'flex items-center justify-center my-0.5 mx-1 h-9 rounded-md cursor-pointer transition-colors',
          isActive ? 'bg-gold-soft text-ivory' : 'text-ivory-dim hover:bg-hover hover:text-ivory'
        )}
      >
        {isStarred ? <Star size={14} className="fill-gold text-gold" /> : <MessageSquare size={14} />}
      </div>
    );
  }

  return (
    <div
      onClick={() => onSelect(session.id)}
      className={clsx(
        'group relative flex items-center gap-2 px-3 py-2 mx-1 my-0.5 rounded-md cursor-pointer transition-colors',
        isActive ? 'bg-gold-soft text-ivory' : 'text-ivory-dim hover:bg-hover hover:text-ivory',
        menuOpen && 'bg-hover-strong text-ivory'
      )}
    >
      {isStarred && (
        <span className="shrink-0">
          <Star size={13} className="fill-gold text-gold" />
        </span>
      )}
      <span className="flex-1 truncate text-[13px]">{session.title}</span>
      <button
        ref={btnRef}
        type="button"
        aria-label="Conversation options"
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        onClick={(e) => { e.stopPropagation(); menuOpen ? setMenuOpen(false) : openMenu(); }}
        className={clsx(
          'shrink-0 p-1 rounded text-ivory-faint hover:text-ivory hover:bg-hover-strong transition-opacity',
          menuOpen ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
        )}
      >
        <MoreHorizontal size={14} />
      </button>

      {/* Portaled to <body> with fixed coords so the sidebar's overflow-y-auto
          can't clip it; computeMenuPlacement flips it upward near the bottom. */}
      {menuOpen && menuStyle && createPortal(
        <div
          ref={menuRef}
          role="menu"
          className="rtie-menu-shadow fixed z-50 rounded-lg border border-line-strong bg-panel py-1"
          style={{ top: menuStyle.top, left: menuStyle.left, width: menuStyle.width }}
          onClick={(e) => e.stopPropagation()}
        >
          <MenuItem icon={<Star size={13} className={isStarred ? 'fill-gold text-gold' : ''} />} onClick={handleStar}>
            {isStarred ? 'Unstar' : 'Star'}
          </MenuItem>
          <MenuItem icon={<Pencil size={13} />} onClick={handleRename}>Rename</MenuItem>
          <div className="my-1 h-px bg-line" />
          <MenuItem icon={<Trash2 size={13} />} onClick={handleDelete} danger>Delete</MenuItem>
        </div>,
        document.body,
      )}

      {renaming && (
        <RenameModal
          initial={session.title}
          onCancel={() => setRenaming(false)}
          onCommit={(next) => { setRenaming(false); onRename?.(session.id, next); }}
        />
      )}

      {deleting && (
        <ConfirmDialog
          labelledBy="rtie-delete-trace-title"
          title="Delete this trace?"
          message="This cannot be undone."
          confirmLabel="Delete"
          onCancel={() => setDeleting(false)}
          onConfirm={() => { setDeleting(false); onDelete?.(session.id); }}
        />
      )}
    </div>
  );
}

// In-app conversation rename — replaces the native window.prompt(). Reuses the
// shared Modal idiom (same as the delete-all-chats confirmation). Pre-fills the
// current name, commits on OK / Enter, cancels on Escape / backdrop / Cancel.
function RenameModal({ initial, onCancel, onCommit }) {
  const [value, setValue] = useState(initial || '');

  const submit = () => {
    const next = value.trim();
    if (!next || next === initial) { onCancel(); return; }
    onCommit(next);
  };

  return (
    <Modal labelledBy="rtie-rename-title" onClose={onCancel}>
      <h2 id="rtie-rename-title" className="text-[15px] font-semibold text-ivory">
        Rename conversation
      </h2>
      <input
        autoFocus
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onFocus={(e) => e.target.select()}
        onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } }}
        placeholder="Conversation name"
        className="mt-3 w-full bg-panel-2 border border-line-strong rounded-md px-3 py-2 text-[13px] text-ivory placeholder:text-ivory-faint focus:outline-none focus:border-line-gold"
      />
      <div className="mt-5 flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="px-3 py-1.5 rounded-md text-[12.5px] font-medium text-ivory-faint hover:text-ivory hover:bg-hover transition-colors"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={submit}
          className="px-3 py-1.5 rounded-md text-[12.5px] font-semibold text-ink bg-gold hover:bg-gold-dim transition-colors"
        >
          OK
        </button>
      </div>
    </Modal>
  );
}
