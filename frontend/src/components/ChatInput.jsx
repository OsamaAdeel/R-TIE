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
    <form onSubmit={handleSubmit} className="border-t border-border p-4">
      <div className="flex items-end gap-3 max-w-4xl mx-auto">
        <div className="flex-1 relative">
          <textarea
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask about a PL/SQL function, or use /commands..."
            disabled={disabled}
            rows={1}
            className="w-full resize-none rounded-xl bg-bg-tertiary border border-border px-4 py-3 text-sm text-text-primary placeholder-text-muted focus:outline-none focus:border-accent transition-colors"
            style={{ minHeight: '48px', maxHeight: '120px' }}
            onInput={(e) => {
              e.target.style.height = 'auto';
              e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px';
            }}
          />
        </div>
        <button
          type="submit"
          disabled={disabled || !value.trim()}
          className="p-3 rounded-xl bg-accent hover:bg-accent-hover disabled:opacity-40 disabled:cursor-not-allowed text-white transition-colors"
        >
          <SendHorizonal size={18} />
        </button>
      </div>
      <div className="flex gap-3 mt-2 max-w-4xl mx-auto">
        {['/refresh-cache', '/cache-list', '/cache-status', '/refresh-schema'].map((cmd) => (
          <button
            key={cmd}
            type="button"
            onClick={() => setValue(cmd + ' ')}
            className="text-xs px-2 py-1 rounded-md bg-bg-tertiary text-text-muted hover:text-accent hover:border-accent border border-border transition-colors"
          >
            {cmd}
          </button>
        ))}
      </div>
    </form>
  );
}
