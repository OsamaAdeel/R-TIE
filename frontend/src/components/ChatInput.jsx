import { useState, useRef, useEffect, useMemo } from 'react';
import { ArrowUp, Slash, Database, Quote, ChevronDown, Square } from 'lucide-react';
import clsx from 'clsx';
import { fetchModels } from '../api/client';

const SLASH_COMMANDS = [
  { cmd: '/refresh-cache', hint: 'Rebuild query cache' },
  { cmd: '/cache-list', hint: 'List cached entries' },
  { cmd: '/cache-status', hint: 'Show cache stats' },
  { cmd: '/refresh-schema', hint: 'Re-introspect DB schema' },
];

// Schema scope chip is cosmetic — backend's QueryRequest does not currently
// accept a `schema` filter (TODO(backend): add schema to /v1/query +
// /v1/stream and forward to the orchestrator). Kept in the UI to match the
// design, but no value is sent.
const SCHEMA_OPTIONS = [
  { id: 'all', label: 'All schemas', desc: 'Search across every indexed schema' },
  { id: 'OFSMDM', label: 'OFSMDM', desc: 'Master data model' },
  { id: 'OFSERM', label: 'OFSERM', desc: 'Operational risk' },
  { id: 'FSDM', label: 'FSDM', desc: 'Financial Services Data Model' },
  { id: 'FSAPPS', label: 'FSAPPS', desc: 'OFSAA application package' },
];

const PROVIDER_LABELS = { openai: 'OpenAI', anthropic: 'Claude' };

const FALLBACK_MODELS = {
  default_provider: 'openai',
  default_model: 'gpt-5-mini',
  providers: {
    openai: { available: true, default_model: 'gpt-5-mini', models: ['gpt-5.4-mini', 'gpt-5.4', 'gpt-5-mini'] },
    anthropic: { available: true, default_model: 'claude-sonnet-4-20250514', models: ['claude-sonnet-4-20250514', 'claude-opus-4-20250514', 'claude-haiku-4-20250514'] },
  },
};

const OpenAIIcon = ({ size = 12 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944z" />
  </svg>
);

const ClaudeIcon = ({ size = 12 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M4.709 15.955l4.72-2.647.08-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.158-.134-.097-.103-.097L4.4 7.7zm10.7-3.3l5.5-3 .1-.3-.2-.1-.7-.05-3-.07-3-.05-2-.04-.5-.1-.4-.4.1-.5.5-.4.6.1 1.5.1 2.3.2 1.7.1 2.5.3h.4l.1-.2-.2-.1-.2-.1-2.4-1.7-2.5-1.7-1.4-.9-.7-.5-.4-.5-.1-1 .6-.7.9 0 .2.1.9.7 1.9 1.5 2.5 1.8.4.3.1-.1 0-.1-.2-.3-1.3-2.4-1.4-2.5-.6-1-.2-.6L13 1l.4-.1 1 .1.4.4.6 1.4 1 2.2 1.5 3 .5.9.2.8.1.3h.2v-.1l.1-1.7.2-2.1.2-2.7.1-.8.4-.9.7-.5.6.3.5.7-.1.4-.3 1.9-.6 2.9-.4 1.9h.2l.2-.2 1-1.3 1.7-2.1.7-.8.9-.9.5-.4h1l.8.6-.3 1-1.1 1.3-.9 1.1-1.3 1.7-.8 1.4.1.1.2 0 2.9-.6 1.5-.3 1.8-.3.8.4.1.4-.3.8L18.3 11l-2.3.5-3.4.8z" />
  </svg>
);

const ProviderIcon = ({ name, size = 12 }) => name === 'anthropic'
  ? <ClaudeIcon size={size} />
  : <OpenAIIcon size={size} />;

export default function ChatInput({ onSend, onStop, disabled, provider, model, onProviderChange, onModelChange }) {
  const [value, setValue] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const textareaRef = useRef(null);

  // Cosmetic state — schema scope + citations toggle don't reach the backend
  // yet (see SCHEMA_OPTIONS comment above and TODO in api/client.js).
  const [schema, setSchema] = useState(SCHEMA_OPTIONS[0]);
  const [citationsOn, setCitationsOn] = useState(true);
  const [schemaOpen, setSchemaOpen] = useState(false);
  const schemaRef = useRef(null);

  // Model picker state — folded in from the old ModelSelector component.
  const [modelsData, setModelsData] = useState(FALLBACK_MODELS);
  const [modelOpen, setModelOpen] = useState(false);
  const [modelTab, setModelTab] = useState(provider || FALLBACK_MODELS.default_provider);
  const modelRef = useRef(null);

  useEffect(() => { fetchModels().then(setModelsData).catch(() => {}); }, []);

  // Toggle the model menu and, on open, sync the active provider tab to the
  // currently-selected provider. Doing this in the click handler instead of
  // an effect keeps fast-refresh + the no-set-state-in-effect rule happy.
  const toggleModelMenu = () => {
    setModelOpen((v) => {
      const next = !v;
      if (next) setModelTab(provider || modelsData.default_provider);
      return next;
    });
  };

  const canSend = value.trim().length > 0 && !disabled;

  // Slash menu shows when the input starts with `/` and the slash token has
  // not been completed with a space.
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

  // Auto-grow textarea up to a max.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  }, [value]);

  // Close popovers on outside click / Escape.
  useEffect(() => {
    if (!schemaOpen && !modelOpen) return;
    const onDoc = (e) => {
      if (schemaOpen && schemaRef.current && !schemaRef.current.contains(e.target)) setSchemaOpen(false);
      if (modelOpen && modelRef.current && !modelRef.current.contains(e.target)) setModelOpen(false);
    };
    const onKey = (e) => {
      if (e.key !== 'Escape') return;
      setSchemaOpen(false);
      setModelOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [schemaOpen, modelOpen]);

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

  const insertSlash = () => {
    setValue((v) => (v && !v.endsWith(' ') ? v + ' /' : v + '/'));
    textareaRef.current?.focus();
  };

  const handleSubmit = (e) => { e.preventDefault(); submit(); };

  const handleKeyDown = (e) => {
    if (menuOpen) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setActiveIndex((i) => (i + 1) % filtered.length); return; }
      if (e.key === 'ArrowUp')   { e.preventDefault(); setActiveIndex((i) => (i - 1 + filtered.length) % filtered.length); return; }
      if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
        e.preventDefault();
        selectCommand(filtered[activeIndex].cmd);
        return;
      }
      if (e.key === 'Escape') { e.preventDefault(); setValue(''); return; }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const currentProvider = provider || modelsData.default_provider;
  const currentModel = model || modelsData.providers?.[currentProvider]?.default_model || '';
  const tabModels = modelsData.providers?.[modelTab]?.models || [];
  const availableProviders = Object.entries(modelsData.providers || {})
    .filter(([, info]) => info?.available)
    .map(([name]) => name);

  return (
    <div className="bg-ink">
      <form onSubmit={handleSubmit} className="max-w-4xl mx-auto px-4 pt-3 pb-4">
        <div className="relative">
          {/* Slash-command autocomplete (above input) */}
          {menuOpen && (
            <div
              role="listbox"
              className="rtie-menu-shadow absolute bottom-full left-0 right-0 mb-2 rounded-xl border border-line-strong bg-panel overflow-hidden z-10"
            >
              <div className="flex items-center justify-between px-3.5 py-2 border-b border-line bg-panel-2">
                <span className="text-[11.5px] font-semibold text-ivory-dim tracking-tight">Commands</span>
                <span className="text-[11px] text-ivory-faint tabular-nums">
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
                        onMouseDown={(e) => { e.preventDefault(); selectCommand(cmd); }}
                        onMouseEnter={() => setActiveIndex(i)}
                        className={clsx(
                          'w-full flex items-center justify-between gap-4 px-3.5 py-2.5 text-left transition-colors',
                          active ? 'bg-gold-soft' : 'hover:bg-hover'
                        )}
                      >
                        <span
                          className={clsx('text-[13.5px] font-medium tracking-tight', active ? 'text-gold' : 'text-ivory')}
                          style={{ fontFamily: 'var(--font-mono)', fontFeatureSettings: "'calt' 1, 'liga' 1" }}
                        >
                          {cmd}
                        </span>
                        <span className="text-[12.5px] text-ivory-dim">{hint}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}

          {/* Composer surface — two rows in a bordered card */}
          <div className="rounded-2xl border border-line-strong bg-panel transition-colors focus-within:border-line-gold">
            {/* Row 1: textarea */}
            <textarea
              ref={textareaRef}
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask about a PL/SQL function, or type / for commands…"
              rows={1}
              className="w-full resize-none bg-transparent px-4 pt-3.5 pb-2 text-[14px] text-ivory placeholder:text-ivory-faint focus:outline-none"
              style={{ minHeight: '52px', maxHeight: '200px' }}
            />

            {/* Row 2: chip toolbar */}
            <div className="flex items-center gap-1.5 px-2.5 pb-2.5">
              {/* Commands chip */}
              <Chip onClick={insertSlash} icon={<Slash size={12} />}>Commands</Chip>

              {/* Schema scope chip — cosmetic until backend support */}
              <div className="relative" ref={schemaRef}>
                <Chip
                  onClick={() => setSchemaOpen((v) => !v)}
                  icon={<Database size={12} />}
                  active={schema.id !== 'all'}
                  caret
                  ariaExpanded={schemaOpen}
                >
                  {schema.id === 'all' ? 'Schema' : schema.label}
                </Chip>
                {schemaOpen && (
                  <div role="listbox" className="rtie-menu-shadow absolute bottom-full left-0 mb-1.5 z-10 min-w-[260px] rounded-xl border border-line-strong bg-panel py-1">
                    <div className="px-3 py-1.5 text-[11px] font-semibold uppercase tracking-widest text-ivory-faint">
                      Scope to schema
                    </div>
                    {SCHEMA_OPTIONS.map((s) => (
                      <button
                        key={s.id}
                        role="option"
                        aria-selected={s.id === schema.id}
                        onClick={() => { setSchema(s); setSchemaOpen(false); }}
                        className={clsx(
                          'w-full text-left px-3 py-2 transition-colors',
                          s.id === schema.id ? 'bg-gold-soft' : 'hover:bg-hover'
                        )}
                      >
                        <div className={clsx('text-[12.5px] font-medium', s.id === schema.id ? 'text-gold' : 'text-ivory')}>
                          {s.label}
                        </div>
                        <div className="text-[11px] text-ivory-faint mt-0.5">{s.desc}</div>
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {/* Citations toggle — cosmetic; client-side only */}
              <Chip
                onClick={() => setCitationsOn((v) => !v)}
                icon={<Quote size={12} />}
                active={citationsOn}
              >
                Citations: {citationsOn ? 'on' : 'off'}
              </Chip>

              {/* Spacer pushes model + send to the right */}
              <div className="flex-1" />

              {/* Model picker chip — opens upward */}
              <div className="relative" ref={modelRef}>
                <Chip
                  onClick={toggleModelMenu}
                  icon={<ProviderIcon name={currentProvider} size={12} />}
                  caret
                  ariaExpanded={modelOpen}
                  active={modelOpen}
                >
                  <span className="font-mono text-[12px]">{currentModel}</span>
                </Chip>
                {modelOpen && (
                  <div role="listbox" className="rtie-menu-shadow absolute bottom-full right-0 mb-1.5 z-10 min-w-[260px] rounded-xl border border-line-strong bg-panel overflow-hidden">
                    <div className="flex border-b border-line">
                      {availableProviders.map((p) => (
                        <button
                          key={p}
                          role="tab"
                          aria-selected={modelTab === p}
                          onClick={() => setModelTab(p)}
                          className={clsx(
                            'flex-1 flex items-center justify-center gap-1.5 px-3 py-2 text-[12px] font-semibold transition-colors',
                            modelTab === p
                              ? 'text-gold border-b-2 border-gold bg-gold-soft'
                              : 'text-ivory-dim hover:text-ivory hover:bg-hover'
                          )}
                        >
                          <ProviderIcon name={p} size={12} />
                          {PROVIDER_LABELS[p] || p}
                        </button>
                      ))}
                    </div>
                    <div className="py-1 max-h-56 overflow-y-auto">
                      {tabModels.map((m) => {
                        const isActive = m === currentModel && modelTab === currentProvider;
                        return (
                          <button
                            key={m}
                            role="option"
                            aria-selected={isActive}
                            onClick={() => {
                              onProviderChange?.(modelTab);
                              onModelChange?.(m);
                              setModelOpen(false);
                            }}
                            className={clsx(
                              'w-full text-left px-3.5 py-2 text-[12.5px] font-mono transition-colors',
                              isActive ? 'bg-gold-soft text-gold' : 'text-ivory-dim hover:bg-hover hover:text-ivory'
                            )}
                          >
                            {m}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>

              {/* Send / Stop button. While streaming, the submit affordance
                  flips to a Stop button that aborts the in-flight request. */}
              {disabled && typeof onStop === 'function' ? (
                <button
                  type="button"
                  onClick={onStop}
                  aria-label="Stop generating"
                  title="Stop generating"
                  className="inline-flex items-center justify-center h-7 w-7 rounded-md bg-gold text-ink hover:bg-gold-dim transition-colors shrink-0"
                >
                  <Square size={11} strokeWidth={0} fill="currentColor" />
                </button>
              ) : (
                <button
                  type="submit"
                  disabled={!canSend}
                  aria-label="Send message"
                  className={clsx(
                    'inline-flex items-center justify-center h-7 w-7 rounded-md transition-colors shrink-0',
                    canSend
                      ? 'bg-gold text-ink hover:bg-gold-dim'
                      : 'bg-hover-strong text-ivory-faint cursor-not-allowed'
                  )}
                >
                  <ArrowUp size={14} strokeWidth={2.5} />
                </button>
              )}
            </div>
          </div>

          {/* Tiny hint line under the composer */}
          <div className="mt-2 px-2 text-[11px] text-ivory-faint select-none">
            {menuOpen ? '↑↓ to navigate · Enter to select · Esc to dismiss' : 'Enter to send · Shift+Enter for newline'}
          </div>
        </div>
      </form>
    </div>
  );
}

function Chip({ icon, children, onClick, active, caret, ariaExpanded }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-expanded={typeof ariaExpanded === 'boolean' ? ariaExpanded : undefined}
      aria-haspopup={caret ? 'listbox' : undefined}
      className={clsx(
        'inline-flex items-center gap-1.5 h-7 px-2.5 rounded-md border text-[12px] font-medium transition-colors shrink-0',
        active
          // Solid accent fill when on/selected — matches the linen
          // prototype's "Citations: on" treatment.
          ? 'border-gold bg-gold text-ink'
          : 'border-line-strong bg-panel-2 text-ivory-dim hover:text-ivory hover:border-line-gold'
      )}
    >
      {icon && <span className="shrink-0">{icon}</span>}
      <span>{children}</span>
      {caret && <ChevronDown size={11} className="shrink-0 opacity-70" />}
    </button>
  );
}
