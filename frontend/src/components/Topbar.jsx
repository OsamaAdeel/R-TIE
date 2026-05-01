import { useEffect, useRef, useState } from 'react';
import { ChevronDown, Star, Pencil, Trash2 } from 'lucide-react';
import clsx from 'clsx';

// Topbar — editable trace title with an actions menu (Star/Rename/Delete).
// The model picker has moved to the Composer per the redesign, so the right
// side is intentionally empty for now.
export default function Topbar({ title, msgCount, isStarred, onStar, onRename, onDelete }) {
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

  const close = () => setMenuOpen(false);
  const handleStar = () => { close(); onStar?.(); };
  const handleRename = () => {
    close();
    if (!onRename) return;
    const next = window.prompt('Rename trace', title);
    if (next == null) return;
    onRename(next);
  };
  const handleDelete = () => {
    close();
    if (!onDelete) return;
    if (!window.confirm('Delete this trace? This cannot be undone.')) return;
    onDelete();
  };

  return (
    <div className="flex items-center justify-between px-5 py-2.5 border-b border-line bg-ink">
      <div className="flex items-center gap-3 min-w-0">
        <div ref={wrapRef} className="relative">
          <button
            type="button"
            onClick={() => setMenuOpen((v) => !v)}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            className={clsx(
              'flex items-center gap-1.5 px-2 py-1 rounded-md text-ivory transition-colors',
              menuOpen ? 'bg-hover-strong' : 'hover:bg-hover'
            )}
          >
            <span className="text-[14px] font-medium truncate max-w-[480px]">{title}</span>
            <ChevronDown
              size={13}
              className={clsx('text-ivory-faint transition-transform', menuOpen && 'rotate-180')}
            />
          </button>

          {menuOpen && (
            <div
              role="menu"
              className="rtie-menu-shadow absolute left-0 top-full mt-1.5 z-20 min-w-[180px] rounded-lg border border-line-strong bg-panel py-1"
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

        {typeof msgCount === 'number' && (
          <span className="text-[11px] text-ivory-faint">
            {msgCount} {msgCount === 1 ? 'message' : 'messages'}
          </span>
        )}
      </div>

      {/* right side intentionally empty — model picker lives in Composer */}
      <div />
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
