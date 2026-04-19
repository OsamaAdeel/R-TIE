import { useState, useRef, useEffect, useMemo } from 'react';
import { ArrowUp, Command } from 'lucide-react';

const SLASH_COMMANDS = [
  { cmd: '/refresh-cache', hint: 'Rebuild query cache' },
  { cmd: '/cache-list', hint: 'List cached entries' },
  { cmd: '/cache-status', hint: 'Show cache stats' },
  { cmd: '/refresh-schema', hint: 'Re-introspect DB schema' },
];

export default function ChatInput({ onSend, disabled }) {
  const [value, setValue] = useState('');
  const [focused, setFocused] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const textareaRef = useRef(null);

  const canSend = value.trim().length > 0 && !disabled;

  // Show menu when the input starts with `/` and the slash token hasn't been completed with a space.
  const slashToken = useMemo(() => {
    if (!value.startsWith('/')) return null;
    const firstSpace = value.indexOf(' ');
    if (firstSpace !== -1) return null;
    return value;
  }, [value]);

  const filtered = useMemo(() => {
    if (slashToken === null) return [];
    const q = slashToken.toLowerCase();
    return SLASH_COMMANDS.filter(({ cmd }) => cmd.toLowerCase().startsWith(q));
  }, [slashToken]);

  const menuOpen = filtered.length > 0;

  useEffect(() => {
    if (activeIndex >= filtered.length) setActiveIndex(0);
  }, [filtered.length, activeIndex]);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  }, [value]);

  const submit = () => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue('');
    setActiveIndex(0);
  };

  const selectCommand = (cmd) => {
    setValue(cmd + ' ');
    setActiveIndex(0);
    textareaRef.current?.focus();
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    submit();
  };

  const handleKeyDown = (e) => {
    if (menuOpen) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActiveIndex((i) => (i + 1) % filtered.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActiveIndex((i) => (i - 1 + filtered.length) % filtered.length);
        return;
      }
      if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
        e.preventDefault();
        selectCommand(filtered[activeIndex].cmd);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setValue('');
        return;
      }
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="bg-bg-primary">
      <form onSubmit={handleSubmit} className="max-w-4xl mx-auto px-4 pt-3 pb-4">
        <div className="relative">
          {menuOpen && (
            <div
              role="listbox"
              className="absolute bottom-full left-0 right-0 mb-2 rounded-xl border border-border bg-bg-secondary shadow-lg overflow-hidden z-10"
            >
              <div
                className="flex items-center justify-between px-3.5 py-2 border-b border-border bg-bg-tertiary/60"
                style={{ fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif" }}
              >
                <span className="text-[11.5px] font-semibold text-text-secondary tracking-tight">
                  Commands
                </span>
                <span className="text-[11px] text-text-muted tabular-nums">
                  {filtered.length} of {SLASH_COMMANDS.length}
                </span>
              </div>
              <ul className="py-1 max-h-64 overflow-y-auto">
                {filtered.map(({ cmd, hint }, i) => {
                  const active = i === activeIndex;
                  return (
                    <li key={cmd} role="option" aria-selected={active}>
                      <button
                        type="button"
                        onMouseDown={(e) => {
                          e.preventDefault();
                          selectCommand(cmd);
                        }}
                        onMouseEnter={() => setActiveIndex(i)}
                        className={[
                          'w-full flex items-center justify-between gap-4 px-3.5 py-2.5 text-left transition-colors',
                          active ? 'bg-accent-soft' : 'hover:bg-bg-tertiary',
                        ].join(' ')}
                      >
                        <span
                          className={[
                            'text-[13.5px] font-medium tracking-tight',
                            active ? 'text-accent-hover' : 'text-accent',
                          ].join(' ')}
                          style={{ fontFamily: "'JetBrains Mono', 'Fira Code', 'Söhne Mono', Consolas, monospace", fontFeatureSettings: "'calt' 1, 'liga' 1" }}
                        >
                          {cmd}
                        </span>
                        <span
                          className="text-[12.5px] text-text-secondary"
                          style={{ fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif", letterSpacing: '0.01em' }}
                        >
                          {hint}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}

          <div
            className={[
              'relative rounded-2xl bg-bg-secondary border transition-colors',
              focused ? 'border-accent shadow-[0_0_0_3px_var(--color-accent-soft)]' : 'border-border hover:border-border-strong',
            ].join(' ')}
          >
            <textarea
              ref={textareaRef}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={handleKeyDown}
              onFocus={() => setFocused(true)}
              onBlur={() => setFocused(false)}
              placeholder="Ask about a PL/SQL function, or type / for commands…"
              disabled={disabled}
              rows={1}
              className="w-full resize-none bg-transparent px-4 pt-3.5 pb-12 text-sm text-text-primary placeholder-text-muted focus:outline-none disabled:opacity-60"
              style={{ minHeight: '52px', maxHeight: '200px' }}
            />

            <div className="absolute inset-x-0 bottom-0 flex items-center justify-between px-3 pb-2.5">
              <div className="flex items-center gap-1 text-[11px] text-text-muted select-none">
                <Command size={11} />
                <span>
                  {menuOpen ? '↑↓ to navigate · Enter to select · Esc to dismiss' : 'Enter to send · Shift+Enter for newline'}
                </span>
              </div>
              <button
                type="submit"
                disabled={!canSend}
                aria-label="Send message"
                className={[
                  'inline-flex items-center justify-center h-7 w-7 rounded-lg transition-colors',
                  canSend
                    ? 'bg-accent text-white hover:bg-accent-hover'
                    : 'bg-bg-tertiary text-text-muted cursor-not-allowed',
                ].join(' ')}
              >
                <ArrowUp size={15} strokeWidth={2.5} />
              </button>
            </div>
          </div>
        </div>
      </form>
    </div>
  );
}
