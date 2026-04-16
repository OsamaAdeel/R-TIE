import { useState } from 'react';
import { User, Bot, Copy, Check } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneLight } from 'react-syntax-highlighter/dist/esm/styles/prism';
import ResponseCard from './ResponseCard';
import CommandResult from './CommandResult';

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
        <div className="w-9 h-9 rounded-full bg-gradient-to-br from-slate-100 to-slate-200 border-2 border-white shadow-sm flex items-center justify-center shrink-0">
          <User size={15} className="text-slate-500" />
        </div>
      </div>
    );
  }

  // Assistant message
  const data = message.data;

  return (
    <div className="flex gap-3">
      <div className="w-9 h-9 rounded-full bg-gradient-to-br from-blue-500 to-indigo-500 border-2 border-white shadow-sm flex items-center justify-center shrink-0">
        <Bot size={15} className="text-white" />
      </div>
      <div className="max-w-4xl flex-1 min-w-0">
        {message.error ? (
          <ErrorCard error={message.error} />
        ) : message.streaming ? (
          message.streamedMarkdown
            ? <StreamingMarkdown markdown={message.streamedMarkdown} meta={message.meta} />
            : <LoadingIndicator />
        ) : message.loading ? (
          <LoadingIndicator />
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

function MarkdownResponse({ data }) {
  const { explanation, confidence, validated, correlation_id, schema, object_name, source_citations } = data || {};
  const markdown = explanation?.markdown || '';

  return (
    <div className="bg-bg-secondary border-2 border-border rounded-2xl rounded-bl-sm overflow-hidden shadow-sm">
      {/* Header */}
      {data?.search_results?.length > 0 && (
        <div className="px-5 py-3 border-b border-border bg-gradient-to-r from-accent-soft to-bg-secondary text-xs text-text-muted">
          Investigated {object_name?.split(' ')[0]} across {data.functions_analyzed?.length || 0} functions systematically
        </div>
      )}

      {/* Markdown body */}
      <div className="px-6 py-5 prose prose-sm max-w-none
        prose-headings:text-text-primary prose-headings:font-bold
        prose-h2:text-lg prose-h2:mt-6 prose-h2:mb-3 prose-h2:pb-2 prose-h2:border-b prose-h2:border-border
        prose-h3:text-base prose-h3:mt-5 prose-h3:mb-2
        prose-p:text-text-secondary prose-p:leading-relaxed prose-p:my-2
        prose-strong:text-text-primary
        prose-code:text-red-600 prose-code:bg-red-50 prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded-md prose-code:text-xs prose-code:font-mono prose-code:before:content-none prose-code:after:content-none
        prose-ul:my-2 prose-li:text-text-secondary prose-li:my-1
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
                const lang = match ? match[1] : 'sql';
                return <CodeBlockWithCopy code={codeStr} language={lang} />;
              }
              return <code className={className} {...props}>{children}</code>;
            },
          }}
        >
          {markdown}
        </ReactMarkdown>
      </div>

      {/* Footer with badges */}
      <div className="px-5 py-3 border-t border-border bg-bg-tertiary flex items-center justify-between text-xs text-text-muted">
        <span className="font-mono">Correlation: {correlation_id}</span>
        <div className="flex items-center gap-2">
          {confidence != null && (
            <span className={`font-mono font-bold px-2.5 py-1 rounded-full ${
              Math.round(confidence * 100) >= 85 ? 'bg-green-50 text-green-600' :
              Math.round(confidence * 100) >= 70 ? 'bg-yellow-50 text-yellow-600' :
              'bg-red-50 text-red-600'
            }`}>
              {Math.round(confidence * 100)}%
            </span>
          )}
          {validated ? (
            <span className="flex items-center gap-1 font-semibold px-2.5 py-1 rounded-full bg-green-50 text-green-600">
              Verified
            </span>
          ) : (
            <span className="flex items-center gap-1 font-semibold px-2.5 py-1 rounded-full bg-yellow-50 text-yellow-600">
              Unverified
            </span>
          )}
          <span className="font-medium">{source_citations?.length || 0} source citations</span>
        </div>
      </div>
    </div>
  );
}

function StreamingMarkdown({ markdown, meta }) {
  return (
    <div className="bg-bg-secondary border-2 border-border rounded-2xl rounded-bl-sm overflow-hidden shadow-sm">
      {/* Header with streaming indicator */}
      {meta?.functions_analyzed?.length > 0 && (
        <div className="px-5 py-3 border-b border-border bg-gradient-to-r from-accent-soft to-bg-secondary text-xs text-text-muted flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full bg-green-400 animate-pulse" />
          Analyzing {meta.functions_analyzed.length} functions...
        </div>
      )}

      {/* Streaming markdown body */}
      <div className="px-6 py-5 prose prose-sm max-w-none
        prose-headings:text-text-primary prose-headings:font-bold
        prose-h2:text-lg prose-h2:mt-6 prose-h2:mb-3 prose-h2:pb-2 prose-h2:border-b prose-h2:border-border
        prose-h3:text-base prose-h3:mt-5 prose-h3:mb-2
        prose-p:text-text-secondary prose-p:leading-relaxed prose-p:my-2
        prose-strong:text-text-primary
        prose-code:text-red-600 prose-code:bg-red-50 prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded-md prose-code:text-xs prose-code:font-mono prose-code:before:content-none prose-code:after:content-none
        prose-ul:my-2 prose-li:text-text-secondary prose-li:my-1
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
        {/* Blinking cursor */}
        <span className="inline-block w-2 h-5 bg-accent/60 animate-pulse rounded-sm ml-0.5" />
      </div>
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
    <div className="relative group my-3">
      {/* Language label + copy button */}
      <div className="flex items-center justify-between bg-white border border-b-0 border-blue-200 rounded-t-xl px-4 py-1.5">
        <span className="text-[10px] font-bold text-blue-400 uppercase tracking-wider">{language}</span>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1 text-[10px] font-medium text-slate-400 hover:text-blue-500 transition-colors"
        >
          {copied ? <Check size={12} className="text-green-500" /> : <Copy size={12} />}
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <SyntaxHighlighter
        style={oneLight}
        language={language}
        showLineNumbers
        wrapLongLines
        customStyle={{
          margin: 0,
          borderRadius: '0 0 12px 12px',
          fontSize: '12.5px',
          border: '1px solid #bfdbfe',
          borderTop: 'none',
          background: '#ffffff',
        }}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  );
}

function LoadingIndicator() {
  return (
    <div className="bg-bg-secondary border-2 border-border rounded-2xl rounded-bl-sm px-5 py-4 shadow-sm">
      <div className="flex items-center gap-3">
        <div className="flex gap-1.5">
          <div className="w-2.5 h-2.5 rounded-full bg-accent animate-bounce" style={{ animationDelay: '0ms' }} />
          <div className="w-2.5 h-2.5 rounded-full bg-blue-400 animate-bounce" style={{ animationDelay: '150ms' }} />
          <div className="w-2.5 h-2.5 rounded-full bg-indigo-400 animate-bounce" style={{ animationDelay: '300ms' }} />
        </div>
        <span className="text-sm text-text-muted font-medium">Analyzing PL/SQL logic...</span>
      </div>
    </div>
  );
}

function ErrorCard({ error }) {
  return (
    <div className="bg-error-light border-2 border-error/20 rounded-2xl rounded-bl-sm px-5 py-4 shadow-sm">
      <p className="text-sm text-error font-semibold">Error</p>
      <p className="text-sm text-text-secondary mt-1">{error}</p>
    </div>
  );
}
