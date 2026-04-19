import { Plus, MessageSquare, Trash2, Activity } from 'lucide-react';
import clsx from 'clsx';

export default function Sidebar({ sessions, activeId, onSelect, onNew, onDelete, health }) {
  return (
    <aside className="w-72 h-screen flex flex-col bg-sidebar text-sidebar-text">
      {/* Header */}
      <div className="p-4 border-b border-white/10">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2.5">
            <div
              className="w-9 h-9 rounded-xl flex items-center justify-center"
              style={{
                background: '#ffffff',
                boxShadow:
                  '0 4px 10px -2px rgba(0, 0, 0, 0.35), inset 0 -1px 0 rgba(15, 23, 42, 0.08)',
              }}
              aria-label="R-TIE"
            >
              <svg width="22" height="22" viewBox="0 0 40 40" fill="none" aria-hidden="true">
                <path
                  d="M11 9 V31 M11 9 H22 C26.5 9 29 12 29 15.75 C29 19.5 26.5 22.5 22 22.5 H11 M20.5 22.5 L30 31"
                  stroke="var(--color-sidebar)"
                  strokeWidth="4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </div>
            <div>
              <h1 className="text-sm font-bold text-white leading-tight tracking-tight">R-TIE</h1>
              <p className="text-[11px] text-sidebar-muted font-medium">Regulatory Trace Engine</p>
            </div>
          </div>
          <button
            onClick={onNew}
            className="p-2 rounded-lg bg-white/5 hover:bg-white/15 text-sidebar-muted hover:text-white transition-all duration-200"
            title="New conversation"
          >
            <Plus size={18} />
          </button>
        </div>

        {/* Health indicator */}
        {health && (
          <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-white/5 text-xs">
            <Activity size={12} className="text-sidebar-muted" />
            <div className="flex items-center gap-2">
              {['oracle', 'redis', 'postgres'].map((svc) => (
                <div key={svc} className="flex items-center gap-1">
                  <div
                    className={clsx(
                      'w-2 h-2 rounded-full',
                      health[svc] === 'ok'
                        ? 'bg-emerald-400 shadow-sm shadow-emerald-400/50'
                        : 'bg-red-400 shadow-sm shadow-red-400/50'
                    )}
                  />
                  <span className="text-sidebar-muted capitalize">{svc}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Sessions list */}
      <div className="flex-1 overflow-y-auto p-2">
        <p className="text-[10px] text-sidebar-muted px-3 py-2 uppercase tracking-widest font-semibold">Conversations</p>
        {sessions.map((session) => (
          <div
            key={session.id}
            onClick={() => onSelect(session.id)}
            className={clsx(
              'group flex items-center gap-2 px-3 py-2.5 rounded-lg cursor-pointer mb-0.5 transition-all duration-200',
              session.id === activeId
                ? 'bg-white/15 text-white'
                : 'text-sidebar-muted hover:bg-white/8 hover:text-sidebar-text'
            )}
          >
            <MessageSquare size={14} className="shrink-0" />
            <span className="text-sm truncate flex-1">{session.title}</span>
            <button
              onClick={(e) => {
                e.stopPropagation();
                onDelete(session.id);
              }}
              className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-white/10 text-sidebar-muted hover:text-red-400 transition-all"
            >
              <Trash2 size={12} />
            </button>
          </div>
        ))}
      </div>
    </aside>
  );
}
