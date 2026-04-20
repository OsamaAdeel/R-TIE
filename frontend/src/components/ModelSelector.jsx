import { useState, useEffect } from 'react';
import { ChevronDown } from 'lucide-react';
import clsx from 'clsx';
import { fetchModels } from '../api/client';

const OpenAIIcon = ({ size = 12, className = '' }) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="currentColor"
    className={className}
    aria-hidden="true"
  >
    <path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z" />
  </svg>
);

const ClaudeIcon = ({ size = 12, className = '' }) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 24 24"
    fill="currentColor"
    className={className}
    aria-hidden="true"
  >
    <path d="M4.709 15.955l4.72-2.647.08-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.158-.134-.097-.103-.097-2.358-1.596-2.552-1.688-1.336-.972-.724-.491-.364-.462-.158-1.008.656-.722.881.06.225.061.893.686 1.908 1.476 2.491 1.833.365.304.145-.103.019-.073-.164-.274-1.355-2.446-1.446-2.49-.644-1.032-.17-.619a2.97 2.97 0 01-.104-.729L6.283.134 6.696 0l.996.134.42.364.62 1.414 1.002 2.229 1.555 3.03.456.898.243.832.091.255h.158V9.01l.128-1.706.237-2.095.23-2.695.08-.76.376-.91.747-.492.584.28.48.685-.067.444-.286 1.851-.559 2.903-.364 1.942h.212l.243-.242.985-1.306 1.652-2.064.73-.82.85-.904.547-.431h1.033l.76.564-.34 1.008-1.064 1.348-.881 1.142-1.264 1.7-.79 1.36.073.11.188-.02 2.856-.606 1.543-.28 1.841-.315.833.388.091.395-.328.807-1.969.486-2.309.462-3.439.813-.042.03.049.061 1.549.146.662.036h1.622l3.02.225.79.522.474.638-.079.485-1.215.62-1.64-.389-3.829-.91-1.312-.329h-.182v.11l1.093 1.068 2.006 1.81 2.509 2.33.127.578-.322.455-.34-.049-2.205-1.657-.851-.747-1.926-1.62h-.128v.17l.444.649 2.345 3.521.122 1.08-.17.353-.608.213-.668-.122-1.374-1.925-1.415-2.167-1.143-1.943-.14.08-.674 7.254-.316.37-.729.28-.607-.461-.322-.747.322-1.476.389-1.924.315-1.53.286-1.9.17-.632-.012-.042-.14.018-1.434 1.967-2.18 2.945-1.726 1.845-.414.164-.717-.37.067-.662.401-.589 2.388-3.036 1.44-1.882.93-1.086-.006-.158h-.055L4.132 18.56l-1.13.146-.487-.456.061-.746.231-.243 1.908-1.312-.006.006z" />
  </svg>
);

const PROVIDER_ICONS = {
  openai: <OpenAIIcon size={12} className="text-text-secondary" />,
  anthropic: <ClaudeIcon size={12} className="text-[#D97757]" />,
};

const PROVIDER_LABELS = {
  openai: 'OpenAI',
  anthropic: 'Claude',
};

const FALLBACK_DATA = {
  default_provider: 'openai',
  default_model: 'gpt-5-mini',
  providers: {
    openai: {
      available: true,
      default_model: 'gpt-5-mini',
      models: [
        'gpt-5.4-mini',
        'gpt-5.4',
        'gpt-5.2',
        'gpt-5-mini',
        'gpt-5-nano',
        'gpt-4o',
        'gpt-4o-mini',
        'gpt-4-turbo',
        'o1',
        'o3-mini',
      ],
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
