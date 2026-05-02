import { useEffect, useRef, useState } from 'react';
import { Plus, MessageSquare, Star, Pencil, Trash2, Settings, MoreHorizontal, ChevronRight, ChevronLeft, Sun, Moon } from 'lucide-react';
import clsx from 'clsx';
import BrandMark from './BrandMark';

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
        <ThemeToggle theme={theme} onToggle={onToggleTheme} />
        {!collapsed && (
          <button
            type="button"
            title="Settings"
            className="w-7 h-7 grid place-items-center rounded-md text-ivory-faint hover:text-ivory hover:bg-hover transition-colors"
          >
            <Settings size={14} />
          </button>
        )}
      </div>
    </aside>
  );
}

function ThemeToggle({ theme, onToggle }) {
  if (typeof onToggle !== 'function') return null;
  const isDark = theme === 'dark';
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-label={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      title={isDark ? 'Switch to light theme' : 'Switch to dark theme'}
      className="w-7 h-7 grid place-items-center rounded-md text-ivory-faint hover:text-ivory hover:bg-hover transition-colors"
    >
      {isDark ? <Sun size={14} /> : <Moon size={14} />}
    </button>
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
  const wrapRef = useRef(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onDoc = (e) => { if (wrapRef.current && !wrapRef.current.contains(e.target)) setMenuOpen(false); };
    const onKey = (e) => { if (e.key === 'Escape') setMenuOpen(false); };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [menuOpen]);

  const handleStar = (e) => { e.stopPropagation(); setMenuOpen(false); onStar?.(session.id); };
  const handleRename = (e) => {
    e.stopPropagation();
    setMenuOpen(false);
    const next = window.prompt('Rename trace', session.title);
    if (next == null) return;
    onRename?.(session.id, next);
  };
  const handleDelete = (e) => {
    e.stopPropagation();
    setMenuOpen(false);
    if (!window.confirm('Delete this trace? This cannot be undone.')) return;
    onDelete?.(session.id);
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
      ref={wrapRef}
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
        type="button"
        aria-label="Conversation options"
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        onClick={(e) => { e.stopPropagation(); setMenuOpen((v) => !v); }}
        className={clsx(
          'shrink-0 p-1 rounded text-ivory-faint hover:text-ivory hover:bg-hover-strong transition-opacity',
          menuOpen ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
        )}
      >
        <MoreHorizontal size={14} />
      </button>

      {menuOpen && (
        <div
          role="menu"
          className="rtie-menu-shadow absolute right-2 top-9 z-20 min-w-[170px] rounded-lg border border-line-strong bg-panel py-1"
          onClick={(e) => e.stopPropagation()}
        >
          <MenuItem icon={<Star size={13} className={isStarred ? 'fill-gold text-gold' : ''} />} onClick={handleStar}>
            {isStarred ? 'Unstar' : 'Star'}
          </MenuItem>
          <MenuItem icon={<Pencil size={13} />} onClick={handleRename}>Rename</MenuItem>
          <div className="my-1 h-px bg-line" />
          <MenuItem icon={<Trash2 size={13} />} onClick={handleDelete} danger>Delete</MenuItem>
        </div>
      )}
    </div>
  );
}

function MenuItem({ icon, onClick, danger, children }) {
  return (
    <button
      role="menuitem"
      onClick={onClick}
      className={clsx(
        'w-full flex items-center gap-2.5 px-3 py-2 text-[13px] font-medium tracking-tight text-left transition-colors',
        danger
          ? 'text-burgundy hover:bg-burgundy/10'
          : 'text-ivory hover:bg-hover'
      )}
      style={{ fontFamily: 'var(--font-sans)', fontFeatureSettings: "'cv11', 'ss01'" }}
    >
      <span className={clsx('shrink-0', danger ? 'text-burgundy/80' : 'text-ivory-faint')}>{icon}</span>
      <span>{children}</span>
    </button>
  );
}
