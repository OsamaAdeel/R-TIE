import { useState, useEffect } from 'react';
import { ChevronDown, Cpu, Sparkles } from 'lucide-react';
import clsx from 'clsx';
import { fetchModels } from '../api/client';

const PROVIDER_ICONS = {
  openai: <Sparkles size={12} className="text-emerald-500" />,
  anthropic: <Cpu size={12} className="text-amber-500" />,
};

const PROVIDER_LABELS = {
  openai: 'OpenAI',
  anthropic: 'Claude',
};

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
      .catch(() => {});
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
        className="flex items-center gap-2 px-3 py-1.5 rounded-xl bg-bg-tertiary border-2 border-border hover:border-accent/30 text-xs transition-all duration-200 hover:shadow-sm"
      >
        {PROVIDER_ICONS[currentProvider]}
        <span className="text-text-secondary font-medium">
          {PROVIDER_LABELS[currentProvider] || currentProvider}
        </span>
        <span className="text-text-muted">/</span>
        <span className="text-text-primary font-mono font-semibold">{currentModel}</span>
        <ChevronDown size={12} className="text-text-muted" />
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute top-full right-0 mt-2 w-72 bg-bg-secondary border-2 border-border rounded-2xl shadow-xl z-20 overflow-hidden">
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
                    'flex-1 flex items-center justify-center gap-1.5 px-3 py-3 text-xs font-semibold transition-all duration-200',
                    currentProvider === p
                      ? 'text-accent border-b-2 border-accent bg-accent-soft'
                      : 'text-text-muted hover:text-text-secondary hover:bg-bg-tertiary'
                  )}
                >
                  {PROVIDER_ICONS[p]}
                  {PROVIDER_LABELS[p] || p}
                </button>
              ))}
            </div>

            {/* Model list */}
            <div className="p-2 max-h-48 overflow-y-auto">
              {currentModels.map((m) => (
                <button
                  key={m}
                  onClick={() => {
                    onModelChange(m);
                    setOpen(false);
                  }}
                  className={clsx(
                    'w-full text-left px-3.5 py-2.5 rounded-xl text-xs font-mono transition-all duration-200',
                    currentModel === m
                      ? 'bg-accent-light text-accent font-bold'
                      : 'text-text-secondary hover:bg-bg-tertiary hover:text-text-primary'
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
