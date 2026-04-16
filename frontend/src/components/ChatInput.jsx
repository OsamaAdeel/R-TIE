import { useState } from 'react';
import { SendHorizonal } from 'lucide-react';

export default function ChatInput({ onSend, disabled }) {
  const [value, setValue] = useState('');

  const handleSubmit = (e) => {
    e.preventDefault();
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue('');
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="border-t border-border bg-bg-secondary p-4 shadow-[0_-4px_20px_rgba(0,0,0,0.04)]">
      <div className="flex items-end gap-3 max-w-4xl mx-auto">
        <div className="flex-1 relative">
          <textarea
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask about a PL/SQL function, or use /commands..."
            disabled={disabled}
            rows={1}
            className="w-full resize-none rounded-2xl bg-bg-tertiary border-2 border-border px-5 py-3.5 text-sm text-text-primary placeholder-text-muted focus:outline-none focus:border-accent focus:ring-4 focus:ring-accent/10 transition-all duration-200"
            style={{ minHeight: '52px', maxHeight: '120px' }}
            onInput={(e) => {
              e.target.style.height = 'auto';
              e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px';
            }}
          />
        </div>
        <button
          type="submit"
          disabled={disabled || !value.trim()}
          className="p-3.5 rounded-2xl bg-gradient-to-r from-accent to-blue-500 hover:from-accent-hover hover:to-blue-600 disabled:opacity-40 disabled:cursor-not-allowed text-white shadow-lg shadow-accent/25 hover:shadow-accent/40 transition-all duration-200 hover:scale-105 active:scale-95"
        >
          <SendHorizonal size={18} />
        </button>
      </div>
      <div className="flex gap-2 mt-3 max-w-4xl mx-auto">
        {['/refresh-cache', '/cache-list', '/cache-status', '/refresh-schema'].map((cmd) => (
          <button
            key={cmd}
            type="button"
            onClick={() => setValue(cmd + ' ')}
            className="text-xs font-medium px-3 py-1.5 rounded-full bg-bg-tertiary text-text-muted hover:text-accent hover:bg-accent-light border border-border hover:border-accent/30 transition-all duration-200"
          >
            {cmd}
          </button>
        ))}
      </div>
    </form>
  );
}
