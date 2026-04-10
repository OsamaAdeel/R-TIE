import { useState, useEffect } from 'react';
import { ChevronDown, Cpu, Sparkles } from 'lucide-react';
import clsx from 'clsx';
import { fetchModels } from '../api/client';

const PROVIDER_ICONS = {
  openai: <Sparkles size={12} className="text-success" />,
  anthropic: <Cpu size={12} className="text-warning" />,
};

const PROVIDER_LABELS = {
  openai: 'OpenAI',
  anthropic: 'Claude',
};

// Hardcoded fallback so the selector works even when backend is down
const FALLBACK_DATA = {
  default_provider: 'openai',
  default_model: 'gpt-4o',
  providers: {
    openai: {
      available: true,
      default_model: 'gpt-4o',
      models: ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'o1', 'o3-mini'],
    },
    anthropic: {
      available: true,
      default_model: 'claude-sonnet-4-20250514',
      models: [
        'claude-sonnet-4-20250514',
        'claude-opus-4-20250514',
        'claude-haiku-4-20250514',
      ],
    },
  },
};

export default function ModelSelector({ provider, model, onProviderChange, onModelChange }) {
  const [modelsData, setModelsData] = useState(FALLBACK_DATA);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    fetchModels()
      .then((data) => setModelsData(data))
      .catch(() => {/* keep fallback */});
  }, []);

  const providers = modelsData.providers;
  const availableProviders = Object.entries(providers)
    .filter(([, info]) => info.available)
    .map(([name]) => name);

  const currentProvider = provider || modelsData.default_provider;
  const currentModels = providers[currentProvider]?.models || [];
  const currentModel = model || providers[currentProvider]?.default_model || '';

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-bg-tertiary border border-border hover:border-accent/40 text-xs transition-colors"
      >
        {PROVIDER_ICONS[currentProvider]}
        <span className="text-text-secondary">
          {PROVIDER_LABELS[currentProvider] || currentProvider}
        </span>
        <span className="text-text-muted">/</span>
        <span className="text-text-primary font-mono">{currentModel}</span>
        <ChevronDown size={12} className="text-text-muted" />
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute bottom-full left-0 mb-2 w-72 bg-bg-secondary border border-border rounded-xl shadow-2xl z-20 overflow-hidden">
            {/* Provider tabs */}
            <div className="flex border-b border-border">
              {availableProviders.map((p) => (
                <button
                  key={p}
                  onClick={() => {
                    onProviderChange(p);
                    onModelChange(providers[p]?.default_model || '');
                  }}
                  className={clsx(
                    'flex-1 flex items-center justify-center gap-1.5 px-3 py-2.5 text-xs font-medium transition-colors',
                    currentProvider === p
                      ? 'text-accent border-b-2 border-accent bg-accent/5'
                      : 'text-text-muted hover:text-text-secondary'
                  )}
                >
                  {PROVIDER_ICONS[p]}
                  {PROVIDER_LABELS[p] || p}
                </button>
              ))}
            </div>

            {/* Model list */}
            <div className="p-1.5 max-h-48 overflow-y-auto">
              {currentModels.map((m) => (
                <button
                  key={m}
                  onClick={() => {
                    onModelChange(m);
                    setOpen(false);
                  }}
                  className={clsx(
                    'w-full text-left px-3 py-2 rounded-lg text-xs font-mono transition-colors',
                    currentModel === m
                      ? 'bg-accent/10 text-accent'
                      : 'text-text-secondary hover:bg-bg-hover hover:text-text-primary'
                  )}
                >
                  {m}
                </button>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
