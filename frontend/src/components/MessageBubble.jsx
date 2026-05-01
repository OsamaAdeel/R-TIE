import { useState } from 'react';
import { Copy, Check, RotateCcw, Pencil, HelpCircle, AlertCircle } from 'lucide-react';
import clsx from 'clsx';
import TrustBanner from './TrustBanner';
import AgentActivity from './AgentActivity';
import CommandResult from './CommandResult';
import Answer, { MarkdownBody } from './Answer';
import { buildPipelineSteps } from '../lib/pipelineSteps';

export default function MessageBubble({ message, onRetry, onEdit }) {
  const isUser = message.role === 'user';

  if (isUser) {
    return <UserMessage content={message.content} onRetry={onRetry} onEdit={onEdit} />;
  }

  const data = message.data;
  const isCommand = data?.type === 'command';
  const inProgress = !!(message.streaming || message.loading);

  // Pipeline disclosure is only meaningful for logic-pipeline responses;
  // skip it for slash commands, errors, and clarifications.
  const pipelineSteps = (!isCommand && !message.error && !message.clarification)
    ? buildPipelineSteps({
        stage: message.stage,
        data,
        streaming: message.streaming,
        loading: message.loading,
      })
    : null;

  const sourceCount = data?.source_citations?.length || 0;

  return (
    <div className="max-w-4xl">
      <div className="mb-2.5 flex items-baseline gap-2">
        <span
          className="text-gold font-bold text-[16px] tracking-tight leading-none"
          style={{ fontFamily: 'var(--font-display)', letterSpacing: '-0.01em' }}
        >
          R-TIE
        </span>
        {sourceCount > 0 && (
          <span className="text-[11px] text-ivory-faint">
            · {sourceCount} source{sourceCount === 1 ? '' : 's'} cited
          </span>
        )}
      </div>

      {pipelineSteps && (
        <AgentActivity
          steps={pipelineSteps}
          defaultOpen={inProgress}
          sourceCount={sourceCount}
        />
      )}

      {message.error ? (
        <ErrorCard error={message.error} />
      ) : message.clarification ? (
        <ClarificationCard message={message.clarification.message} />
      ) : message.streaming ? (
        message.streamedMarkdown
          ? <StreamingMarkdown markdown={message.streamedMarkdown} meta={message.meta} />
          : null
      ) : message.loading ? null : isCommand ? (
        <CommandResult result={data.result} correlationId={data.correlation_id} />
      ) : (
        <>
          <TrustBanner data={data} />
          <Answer data={data} />
        </>
      )}
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
    if (trimmed && trimmed !== content) onEdit?.(trimmed);
    setEditing(false);
  };

  const handleCancel = () => {
    setEditText(content);
    setEditing(false);
  };

  if (editing) {
    return (
      <div className="flex justify-end">
        <div className="max-w-2xl w-full">
          <textarea
            value={editText}
            onChange={(e) => setEditText(e.target.value)}
            className="w-full bg-panel border border-line-strong rounded-[10px] px-4 py-3 text-[13.5px] text-ivory resize-none focus:outline-none focus:border-line-gold"
            rows={Math.max(2, editText.split('\n').length)}
            autoFocus
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSave(); }
              if (e.key === 'Escape') handleCancel();
            }}
          />
          <div className="flex items-center justify-end gap-2 mt-2">
            <button
              onClick={handleCancel}
              className="px-3 py-1 text-[11px] font-medium text-ivory-faint hover:text-ivory transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              className="px-3 py-1 text-[11px] font-medium text-ink bg-gold rounded-md hover:bg-gold-dim transition-colors"
            >
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
        <div className="bg-panel border border-line rounded-[10px] px-4 py-2.5">
          <p className="text-[13.5px] text-ivory whitespace-pre-wrap">{content}</p>
        </div>
        {/* Hover-revealed action row */}
        <div className="flex items-center justify-end gap-1 mt-1 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity duration-150">
          <button
            onClick={onRetry}
            className="p-1 rounded hover:bg-hover text-ivory-faint hover:text-ivory transition-colors"
            title="Retry"
          >
            <RotateCcw size={13} />
          </button>
          <button
            onClick={() => setEditing(true)}
            className="p-1 rounded hover:bg-hover text-ivory-faint hover:text-ivory transition-colors"
            title="Edit"
          >
            <Pencil size={13} />
          </button>
          <button
            onClick={handleCopy}
            className="p-1 rounded hover:bg-hover text-ivory-faint hover:text-ivory transition-colors"
            title="Copy"
          >
            {copied ? <Check size={13} className="text-gold" /> : <Copy size={13} />}
          </button>
        </div>
      </div>
    </div>
  );
}

function StreamingMarkdown({ markdown, meta }) {
  return (
    <div className="rounded-[10px] border border-line bg-panel/40 p-4">
      <div className="flex items-center gap-2 text-[11px] text-ivory-faint mb-3">
        <span className="relative flex h-2 w-2">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-gold opacity-75" />
          <span className="relative inline-flex rounded-full h-2 w-2 bg-gold" />
        </span>
        {meta?.functions_analyzed?.length > 0
          ? `Writing explanation across ${meta.functions_analyzed.length} function${meta.functions_analyzed.length === 1 ? '' : 's'}…`
          : 'Generating response…'}
      </div>
      <MarkdownBody markdown={markdown} />
      <span className="inline-block w-1.5 h-4 bg-gold animate-pulse rounded-sm ml-0.5 align-text-bottom" />
    </div>
  );
}

function ErrorCard({ error }) {
  return (
    <div className={clsx(
      'flex items-start gap-3 rounded-[10px] border px-4 py-3',
      'border-burgundy/50 bg-burgundy/10'
    )}>
      <AlertCircle size={14} className="text-burgundy mt-0.5 shrink-0" />
      <div className="flex-1 min-w-0">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-burgundy">Error</p>
        <p className="text-[12.5px] text-ivory mt-1 break-words">{error}</p>
      </div>
    </div>
  );
}

function ClarificationCard({ message }) {
  return (
    <div className="flex items-start gap-3 rounded-[10px] border border-amber/40 bg-amber/5 px-4 py-3">
      <HelpCircle size={14} className="text-amber mt-0.5 shrink-0" />
      <div className="flex-1 min-w-0">
        <p className="text-[10px] font-semibold uppercase tracking-wider text-amber">
          More information needed
        </p>
        <p className="text-[13px] text-ivory mt-1 leading-relaxed whitespace-pre-wrap">{message}</p>
      </div>
    </div>
  );
}
