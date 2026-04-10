import { Plus, MessageSquare, Trash2, Activity } from 'lucide-react';
import clsx from 'clsx';

export default function Sidebar({ sessions, activeId, onSelect, onNew, onDelete, health }) {
  const statusColor = {
    healthy: 'bg-success',
    degraded: 'bg-warning',
    error: 'bg-error',
  };

  return (
    <aside className="w-72 h-screen flex flex-col bg-bg-secondary border-r border-border">
      {/* Header */}
      <div className="p-4 border-b border-border">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-lg bg-accent flex items-center justify-center text-white font-bold text-sm">
              R
            </div>
            <div>
              <h1 className="text-sm font-semibold text-text-primary leading-tight">RTIE</h1>
              <p className="text-xs text-text-muted">Regulatory Trace Engine</p>
            </div>
          </div>
          <button
            onClick={onNew}
            className="p-2 rounded-lg hover:bg-bg-hover text-text-secondary hover:text-text-primary transition-colors"
            title="New conversation"
          >
            <Plus size={18} />
          </button>
        </div>

        {/* Health indicator */}
        {health && (
          <div className="flex items-center gap-2 px-2 py-1.5 rounded-md bg-bg-tertiary text-xs">
            <Activity size={12} className="text-text-muted" />
            <div className="flex items-center gap-1.5">
              {['oracle', 'redis', 'postgres'].map((svc) => (
                <div key={svc} className="flex items-center gap-1">
                  <div
                    className={clsx(
                      'w-1.5 h-1.5 rounded-full',
                      health[svc] === 'ok' ? 'bg-success' : 'bg-error'
                    )}
                  />
                  <span className="text-text-muted capitalize">{svc}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Sessions list */}
      <div className="flex-1 overflow-y-auto p-2">
        <p className="text-xs text-text-muted px-2 py-1 uppercase tracking-wider">Conversations</p>
        {sessions.map((session) => (
          <div
            key={session.id}
            onClick={() => onSelect(session.id)}
            className={clsx(
              'group flex items-center gap-2 px-3 py-2.5 rounded-lg cursor-pointer mb-0.5 transition-colors',
              session.id === activeId
                ? 'bg-bg-tertiary text-text-primary'
                : 'text-text-secondary hover:bg-bg-hover hover:text-text-primary'
            )}
          >
            <MessageSquare size={14} className="shrink-0" />
            <span className="text-sm truncate flex-1">{session.title}</span>
            <button
              onClick={(e) => {
                e.stopPropagation();
                onDelete(session.id);
              }}
              className="opacity-0 group-hover:opacity-100 p-1 rounded hover:bg-bg-hover text-text-muted hover:text-error transition-all"
            >
              <Trash2 size={12} />
            </button>
          </div>
        ))}
      </div>
    </aside>
  );
}
