import { useState } from 'react';
import { Copy, Check, Search, Brain, FileCode, ChevronRight, Sparkles, RotateCcw, Pencil } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneLight } from 'react-syntax-highlighter/dist/esm/styles/prism';
import ResponseCard from './ResponseCard';

// Code theme — transparent bg, wrapper handles the styling
const codeTheme = {
  ...oneLight,
  'pre[class*="language-"]': {
    ...oneLight['pre[class*="language-"]'],
    background: 'transparent',
    margin: 0,
  },
  'code[class*="language-"]': {
    ...oneLight['code[class*="language-"]'],
    background: 'transparent',
  },
};
import CommandResult from './CommandResult';

const STAGE_CONFIG = {
  classify: { icon: Brain, label: 'Understanding query', color: 'text-violet-500' },
  search:   { icon: Search, label: 'Searching functions', color: 'text-blue-500' },
  fetch:    { icon: FileCode, label: 'Reading source code', color: 'text-emerald-500' },
  explain:  { icon: Sparkles, label: 'Generating explanation', color: 'text-amber-500' },
};

export default function MessageBubble({ message, onRetry, onEdit }) {
  const isUser = message.role === 'user';

  if (isUser) {
    return <UserMessage content={message.content} onRetry={onRetry} onEdit={onEdit} />;
  }

  // Assistant message
  const data = message.data;

  return (
    <div>
      <div className="max-w-4xl">
        {message.error ? (
          <ErrorCard error={message.error} />
        ) : message.streaming ? (
          message.streamedMarkdown
            ? <StreamingMarkdown markdown={message.streamedMarkdown} meta={message.meta} stage={message.stage} />
            : <AgentThinking stage={message.stage} />
        ) : message.loading ? (
          <AgentThinking stage={message.stage} />
        ) : data?.type === 'command' ? (
          <CommandResult result={data.result} correlationId={data.correlation_id} />
        ) : data?.explanation?.markdown ? (
          <MarkdownResponse data={data} />
        ) : (
          <ResponseCard data={data} />
        )}
      </div>
    </div>
  );
}

function UserMessage({ content, onRetry, onEdit }) {
  const [copied, setCopied] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState(content);

  const handleCopy = () => {
    navigator.clipboard.writeText(content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleSave = () => {
    const trimmed = editText.trim();
    if (trimmed && trimmed !== content) {
      onEdit?.(trimmed);
    }
    setEditing(false);
  };

  const handleCancel = () => {
    setEditText(content);
    setEditing(false);
  };

  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

  if (editing) {
    return (
      <div className="flex justify-end">
        <div className="max-w-2xl w-full">
          <textarea
            value={editText}
            onChange={(e) => setEditText(e.target.value)}
            className="w-full bg-white border border-[#d0d0d0] rounded-xl px-4 py-3 text-sm text-text-primary resize-none focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent/30"
            rows={Math.max(2, editText.split('\n').length)}
            autoFocus
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSave(); }
              if (e.key === 'Escape') handleCancel();
            }}
          />
          <div className="flex items-center justify-end gap-2 mt-2">
            <button onClick={handleCancel} className="px-3 py-1 text-xs font-medium text-[#888] hover:text-[#555] transition-colors">
              Cancel
            </button>
            <button onClick={handleSave} className="px-3 py-1 text-xs font-medium text-white bg-accent rounded-lg hover:bg-accent-hover transition-colors">
              Send
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex justify-end group">
      <div className="max-w-2xl">
        <div className="bg-[#f0f0f0] rounded-2xl rounded-br-sm px-5 py-3">
          <p className="text-sm text-text-primary whitespace-pre-wrap">{content}</p>
        </div>
        {/* Action bar — visible on hover */}
        <div className="flex items-center justify-end gap-1 mt-1 opacity-0 group-hover:opacity-100 transition-opacity duration-150">
          <span className="text-[11px] text-[#aaa] mr-1">{time}</span>
          <button onClick={onRetry} className="p-1 rounded hover:bg-[#f0f0f0] text-[#aaa] hover:text-[#555] transition-colors" title="Retry">
            <RotateCcw size={13} />
          </button>
          <button onClick={() => setEditing(true)} className="p-1 rounded hover:bg-[#f0f0f0] text-[#aaa] hover:text-[#555] transition-colors" title="Edit">
            <Pencil size={13} />
          </button>
          <button onClick={handleCopy} className="p-1 rounded hover:bg-[#f0f0f0] text-[#aaa] hover:text-[#555] transition-colors" title="Copy">
            {copied ? <Check size={13} className="text-green-500" /> : <Copy size={13} />}
          </button>
        </div>
      </div>
    </div>
  );
}

function AgentThinking({ stage }) {
  const currentStage = stage?.stage || 'classify';
  const stages = ['classify', 'search', 'fetch', 'explain'];
  const currentIdx = stages.indexOf(currentStage);

  return (
    <div className="space-y-0">
      {/* Pipeline stages */}
      <div className="bg-bg-secondary border border-border rounded-xl px-4 py-3 space-y-2">
        {stages.map((s, i) => {
          const config = STAGE_CONFIG[s];
          const Icon = config.icon;
          const isActive = i === currentIdx;
          const isDone = i < currentIdx;
          const isPending = i > currentIdx;

          return (
            <div key={s} className={`flex items-center gap-2.5 py-1 transition-all duration-300 ${isPending ? 'opacity-30' : ''}`}>
              {/* Status indicator */}
              {isDone ? (
                <div className="w-5 h-5 rounded-full bg-green-100 flex items-center justify-center">
                  <Check size={11} className="text-green-600" />
                </div>
              ) : isActive ? (
                <div className="w-5 h-5 rounded-full bg-white border-2 border-accent flex items-center justify-center">
                  <div className="w-2 h-2 rounded-full bg-accent animate-pulse" />
                </div>
              ) : (
                <div className="w-5 h-5 rounded-full bg-slate-100 border border-slate-200" />
              )}

              {/* Icon + label */}
              <Icon size={13} className={isActive ? config.color : isDone ? 'text-green-500' : 'text-slate-300'} />
              <span className={`text-xs font-medium ${isActive ? 'text-text-primary' : isDone ? 'text-text-muted' : 'text-slate-300'}`}>
                {isActive ? (stage?.message || config.label) : config.label}
              </span>

              {/* Spinner for active */}
              {isActive && (
                <div className="ml-auto">
                  <div className="w-3.5 h-3.5 border-2 border-accent/30 border-t-accent rounded-full animate-spin" />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function MarkdownResponse({ data }) {
  const { explanation, functions_analyzed } = data || {};
  const markdown = explanation?.markdown || '';

  return (
    <div>
      {/* Subtle context line */}
      {functions_analyzed?.length > 0 && (
        <div className="flex items-center gap-1.5 text-[11px] text-text-muted mb-3">
          <ChevronRight size={10} />
          Investigated {functions_analyzed.join(', ')} systematically
        </div>
      )}

      {/* Markdown body — no card, no border, just clean text */}
      <MarkdownBody markdown={markdown} />
    </div>
  );
}

function StreamingMarkdown({ markdown, meta, stage }) {
  return (
    <div>
      {/* Live indicator */}
      <div className="flex items-center gap-2 text-[11px] text-text-muted mb-3">
        <span className="relative flex h-2 w-2">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75" />
          <span className="relative inline-flex rounded-full h-2 w-2 bg-green-500" />
        </span>
        {meta?.functions_analyzed?.length > 0
          ? `Writing explanation across ${meta.functions_analyzed.length} functions...`
          : 'Generating response...'}
      </div>

      {/* Streaming body — clean, no card */}
      <MarkdownBody markdown={markdown} />
      <span className="inline-block w-1.5 h-4 bg-accent animate-pulse rounded-sm ml-0.5 align-text-bottom" />
    </div>
  );
}

function MarkdownBody({ markdown }) {
  return (
    <div className="rtie-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code({ node, inline, className, children, ...props }) {
            const match = /language-(\w+)/.exec(className || '');
            const codeStr = String(children).replace(/\n$/, '');
            const isBlock = !inline && (match || codeStr.includes('\n'));
            if (isBlock) {
              return <CodeBlockWithCopy code={codeStr} language={match ? match[1] : 'sql'} />;
            }
            return <code {...props}>{children}</code>;
          },
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}

function CodeBlockWithCopy({ code, language }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="rtie-codeblock group">
      {/* Header: language label left, copy icon right */}
      <div className="rtie-codeblock-header">
        <span className="rtie-codeblock-lang">{language}</span>
        <button onClick={handleCopy} className="rtie-codeblock-copy">
          {copied ? <Check size={14} /> : <Copy size={14} />}
        </button>
      </div>
      {/* Code body */}
      <div className="rtie-codeblock-body">
        <SyntaxHighlighter
          style={codeTheme}
          language={language}
          wrapLongLines
          customStyle={{
            margin: 0,
            borderRadius: 0,
            fontSize: '13px',
            fontFamily: "'Söhne Mono', 'Fira Code', 'JetBrains Mono', Consolas, monospace",
            border: 'none',
            background: 'transparent',
            padding: '16px',
            lineHeight: '1.55',
          }}
        >
          {code}
        </SyntaxHighlighter>
      </div>
    </div>
  );
}

function LoadingIndicator() {
  return <AgentThinking stage={null} />;
}

function ErrorCard({ error }) {
  return (
    <div className="bg-red-50 border border-red-200 rounded-xl px-4 py-3">
      <p className="text-xs text-red-600 font-semibold">Error</p>
      <p className="text-xs text-red-500 mt-1">{error}</p>
    </div>
  );
}
