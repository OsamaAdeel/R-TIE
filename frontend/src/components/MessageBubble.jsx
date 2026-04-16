import { useState } from 'react';
import { User, Copy, Check, Search, Database, Brain, FileCode, ChevronRight, Sparkles } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneLight } from 'react-syntax-highlighter/dist/esm/styles/prism';
import ResponseCard from './ResponseCard';

// Force white background on code blocks — oneLight defaults to grey
const codeTheme = {
  ...oneLight,
  'pre[class*="language-"]': {
    ...oneLight['pre[class*="language-"]'],
    background: '#ffffff',
    margin: 0,
  },
  'code[class*="language-"]': {
    ...oneLight['code[class*="language-"]'],
    background: '#ffffff',
  },
};
import CommandResult from './CommandResult';

const STAGE_CONFIG = {
  classify: { icon: Brain, label: 'Understanding query', color: 'text-violet-500' },
  search:   { icon: Search, label: 'Searching functions', color: 'text-blue-500' },
  fetch:    { icon: FileCode, label: 'Reading source code', color: 'text-emerald-500' },
  explain:  { icon: Sparkles, label: 'Generating explanation', color: 'text-amber-500' },
};

export default function MessageBubble({ message }) {
  const isUser = message.role === 'user';

  if (isUser) {
    return (
      <div className="flex gap-3 justify-end">
        <div className="max-w-2xl">
          <div className="bg-gradient-to-br from-accent to-blue-500 rounded-2xl rounded-br-sm px-5 py-3 shadow-lg shadow-accent/15">
            <p className="text-sm text-white whitespace-pre-wrap">{message.content}</p>
          </div>
        </div>
        <div className="w-8 h-8 rounded-full bg-gradient-to-br from-slate-100 to-slate-200 border border-slate-200 flex items-center justify-center shrink-0">
          <User size={14} className="text-slate-500" />
        </div>
      </div>
    );
  }

  // Assistant message
  const data = message.data;

  return (
    <div className="flex gap-3">
      {/* Agent avatar */}
      <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-500 via-purple-500 to-pink-500 flex items-center justify-center shrink-0 shadow-sm shadow-purple-500/20">
        <Database size={14} className="text-white" />
      </div>
      <div className="max-w-4xl flex-1 min-w-0">
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
    <div className="prose prose-sm max-w-none
      prose-headings:text-text-primary prose-headings:font-bold
      prose-h2:text-[15px] prose-h2:mt-5 prose-h2:mb-2 prose-h2:pb-1.5 prose-h2:border-b prose-h2:border-border
      prose-h3:text-sm prose-h3:mt-4 prose-h3:mb-1.5
      prose-p:text-text-secondary prose-p:leading-relaxed prose-p:my-1.5 prose-p:text-[13px]
      prose-strong:text-text-primary
      prose-code:text-red-600 prose-code:bg-red-50 prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded-md prose-code:text-xs prose-code:font-mono prose-code:before:content-none prose-code:after:content-none
      prose-ul:my-1.5 prose-li:text-text-secondary prose-li:my-0.5 prose-li:text-[13px]
      prose-ol:my-1.5
      prose-a:text-accent
    ">
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
            return <code className={className} {...props}>{children}</code>;
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
    <div className="relative group my-2.5 not-prose">
      <div style={{ background: '#ffffff', border: '1px solid #e2e8f0', borderBottom: 'none', borderRadius: '8px 8px 0 0', padding: '4px 14px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <span style={{ fontSize: '10px', fontWeight: 700, color: '#93c5fd', textTransform: 'uppercase', letterSpacing: '0.05em' }}>{language}</span>
        <button
          onClick={handleCopy}
          style={{ fontSize: '10px', fontWeight: 500, color: '#94a3b8', background: 'none', border: 'none', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '4px' }}
        >
          {copied ? <Check size={11} style={{ color: '#22c55e' }} /> : <Copy size={11} />}
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <div style={{ background: '#ffffff', border: '1px solid #e2e8f0', borderTop: 'none', borderRadius: '0 0 8px 8px', overflow: 'hidden' }}>
        <SyntaxHighlighter
          style={codeTheme}
          language={language}
          showLineNumbers
          wrapLongLines
          customStyle={{
            margin: 0,
            borderRadius: 0,
            fontSize: '12px',
            border: 'none',
            background: '#ffffff',
            padding: '12px',
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
