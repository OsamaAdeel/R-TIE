import { useState, useRef } from 'react';
import { Copy, Check, ChevronRight, Database, FileCode, Hash } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneDark, oneLight } from 'react-syntax-highlighter/dist/esm/styles/prism';
import clsx from 'clsx';
import { useTheme } from '../hooks/useTheme';

// The agent answer block — markdown body, expandable sources rail, and a
// footer with confidence + copy. The prototype's source cards expect
// {kind, title, sub, path, lines, conf} but the backend's source_citations
// are {line, text, context, source} (RTIE/src/agents/renderer.py
// _extract_citations) and `text`/`context` are very often the placeholder
// string "inline reference" because the LLM doesn't fill them in. We
// surface the function name (from `functions_analyzed`), the line
// number, and a category derived from `c.source` ("Step 3", "Formula",
// "Reference") instead of leaning on the placeholder text. (See "backend
// gap B" in the migration plan: TODO(backend) — emit a richer per-
// citation struct including the originating function.)
export default function Answer({ data }) {
  const markdown = data?.explanation?.markdown || '';
  const citations = Array.isArray(data?.source_citations) ? data.source_citations : [];
  const functions = Array.isArray(data?.functions_analyzed) ? data.functions_analyzed : [];
  const confidence = typeof data?.confidence === 'number' ? data.confidence : null;

  const sources = buildSources(citations, functions, confidence);

  return (
    <div className="rounded-[10px] border border-line bg-panel/40 p-4">
      <MarkdownBody markdown={markdown || '*(no explanation available)*'} />
      {sources.length > 0 && <SourcesRail sources={sources} />}
      <Footer markdown={markdown} sourceCount={sources.length} confidence={confidence} />
    </div>
  );
}

function buildSources(citations, functions, confidence) {
  if (citations.length === 0) {
    return functions.map((fn) => ({
      kind: 'plsql',
      title: fn,
      category: 'Function',
      detail: 'analyzed function',
      lines: '',
      conf: confidence ?? null,
    }));
  }

  // Attribute every citation to the analyzed function. When the query
  // touched a single function, that's an exact attribution; for multi-
  // function queries we surface the first + "et al." since the backend
  // doesn't currently emit per-citation function names.
  const primaryFn = functions.length === 1
    ? functions[0]
    : functions.length > 1
      ? `${functions[0]} et al.`
      : null;

  return citations.map((c, i) => {
    const detail = pickMeaningful(c.context, c.text);
    return {
      kind: 'plsql',
      title: primaryFn || `Citation ${i + 1}`,
      category: sourceCategory(c.source),
      detail,
      lines: c.line ? `L${c.line}` : '',
      conf: confidence ?? null,
    };
  });
}

// Strip the backend's placeholder text. Returns the first meaningful
// string from the candidates, truncated for display, or '' if everything
// is empty / a placeholder.
function pickMeaningful(...candidates) {
  for (const raw of candidates) {
    if (!raw) continue;
    const t = String(raw).trim().replace(/\s+/g, ' ');
    if (!t) continue;
    if (/^inline reference$/i.test(t)) continue;
    return t.length > 80 ? t.slice(0, 79) + '…' : t;
  }
  return '';
}

// `c.source` from the renderer is one of "raw_reference", "formula", or
// "step_<N>". Translate to a human label for the card chip.
function sourceCategory(source) {
  if (!source || source === 'raw_reference') return 'Reference';
  if (source === 'formula') return 'Formula';
  const m = /^step_(.+)$/.exec(source);
  if (m) return `Step ${m[1]}`;
  return source;
}

function SourcesRail({ sources }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={clsx('mt-4 rounded-[10px] border border-line bg-panel-2/60', open ? 'open' : 'closed')}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="w-full flex items-center gap-2 px-3 py-2"
      >
        <ChevronRight size={12} className={clsx('shrink-0 transition-transform text-ivory-faint', open && 'rotate-90')} />
        <span className="text-[12px] font-semibold text-ivory">Sources</span>
        <span className="flex-1 h-px bg-line ml-1" />
        <span className="text-[11px] text-ivory-faint">{sources.length} citation{sources.length === 1 ? '' : 's'}</span>
      </button>
      {open && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 px-3 pb-3 pt-1">
          {sources.map((s, i) => <SourceCard key={i} source={s} index={i} />)}
        </div>
      )}
    </div>
  );
}

const KIND_META = {
  plsql:   { Icon: FileCode },
  table:   { Icon: Database },
  view:    { Icon: Database },
  snippet: { Icon: Hash },
};

function SourceCard({ source, index }) {
  const { Icon: KindIcon } = KIND_META[source.kind] || KIND_META.snippet;
  const conf = typeof source.conf === 'number' ? source.conf : null;
  const confPct = conf !== null ? Math.round(conf * 100) : null;
  const tone = confPct === null ? 'unknown' : confPct >= 95 ? 'high' : confPct >= 85 ? 'med' : 'low';

  return (
    <div className="rounded-md border border-line bg-panel/60 p-2.5">
      {/* Top row: index · category chip · line number on the right */}
      <div className="flex items-center gap-2 mb-1.5">
        <span className="font-mono text-[11px] text-ivory-faint">
          {String(index + 1).padStart(2, '0')}
        </span>
        {source.category && (
          <span className="inline-flex items-center gap-1 text-[10px] uppercase tracking-wider text-gold font-semibold">
            <KindIcon size={10} />{source.category}
          </span>
        )}
        {source.lines && (
          <span className="ml-auto inline-flex items-center px-1.5 py-0.5 rounded text-[10.5px] font-mono font-semibold text-gold bg-gold-soft border border-line-gold">
            {source.lines}
          </span>
        )}
      </div>
      {/* Function name — the actual subject of this citation */}
      <div className="text-[12.5px] font-mono font-semibold text-ivory truncate" title={source.title}>
        {source.title}
      </div>
      {source.detail && (
        <div className="text-[11px] text-ivory-dim leading-snug mt-1 line-clamp-2" title={source.detail}>
          {source.detail}
        </div>
      )}
      {confPct !== null && (
        <div className="flex items-center gap-1.5 mt-2">
          <div className="flex-1 h-1 rounded-full bg-hover-strong overflow-hidden">
            <div
              className={clsx(
                'h-full',
                tone === 'high' ? 'bg-emerald' : tone === 'med' ? 'bg-amber' : 'bg-burgundy'
              )}
              style={{ width: `${confPct}%` }}
            />
          </div>
          <span className="text-[10px] font-mono tabular-nums text-ivory-faint">{confPct}%</span>
        </div>
      )}
    </div>
  );
}

function Footer({ markdown, sourceCount, confidence }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = async () => {
    if (!markdown) return;
    try { await navigator.clipboard.writeText(markdown); }
    catch { /* swallow */ }
    setCopied(true);
    setTimeout(() => setCopied(false), 1800);
  };
  // No backend signal for tokens / latency / agent count yet — TODO(backend):
  // expose timing & token usage from the SSE done payload so we can show
  // the prototype's full foot-stat row.
  const confPct = typeof confidence === 'number' ? Math.round(confidence * 100) : null;
  return (
    <div className="flex items-center justify-between gap-3 mt-3 pt-3 border-t border-line text-[11px] text-ivory-faint">
      <div className="flex items-center gap-3">
        <span><strong className="text-ivory">{sourceCount}</strong> sources</span>
        {confPct !== null && <span><strong className="text-ivory">{confPct}%</strong> confidence</span>}
      </div>
      <button
        type="button"
        onClick={handleCopy}
        className={clsx(
          'inline-flex items-center gap-1.5 px-2 py-1 rounded text-[11px] transition-colors',
          copied ? 'text-gold' : 'text-ivory-faint hover:text-ivory hover:bg-hover'
        )}
        aria-label="Copy answer markdown"
      >
        {copied ? <Check size={12} /> : <Copy size={12} />}
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  );
}

// Markdown body — renders the assistant's markdown, with code blocks
// highlighted via a dark Prism theme to match the signature shell.
// Exported so the streaming branch in MessageBubble can reuse it.
export function MarkdownBody({ markdown }) {
  return (
    <div className="rtie-markdown-dark text-[14px] leading-relaxed text-ivory">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code({ inline, className, children, ...props }) {
            const match = /language-(\w+)/.exec(className || '');
            const codeStr = String(children).replace(/\n$/, '');
            const isBlock = !inline && (match || codeStr.includes('\n'));
            if (isBlock) return <DarkCodeBlock code={codeStr} language={match ? match[1] : 'sql'} />;
            return (
              <code
                {...props}
                className="px-1.5 py-0.5 rounded text-[13px] font-mono text-gold bg-gold-soft border border-line-gold"
              >
                {children}
              </code>
            );
          },
          p:  (props) => <p {...props} className="my-2.5 text-ivory-dim" />,
          h1: (props) => <h1 {...props} className="text-[17px] font-semibold text-ivory mt-6 mb-2" />,
          h2: (props) => <h2 {...props} className="text-[16px] font-semibold text-ivory mt-5 mb-2" />,
          h3: (props) => <h3 {...props} className="text-[14.5px] font-semibold text-ivory mt-4 mb-1.5" />,
          ul: (props) => <ul {...props} className="my-2 pl-5 list-disc marker:text-ivory-faint" />,
          ol: (props) => <ol {...props} className="my-2 pl-5 list-decimal marker:text-ivory-faint" />,
          li: (props) => <li {...props} className="my-1 text-ivory-dim" />,
          strong: (props) => <strong {...props} className="font-semibold text-ivory" />,
          a: (props) => <a {...props} className="text-gold hover:underline" />,
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}

function DarkCodeBlock({ code, language }) {
  const [copied, setCopied] = useState(false);
  const ref = useRef(null);
  const { theme } = useTheme();
  // Prism style switches with the app theme so PL/SQL excerpts feel
  // native on both surfaces — `oneDark` on signature, `oneLight` on linen.
  const prismStyle = theme === 'dark' ? oneDark : oneLight;
  const handleCopy = async () => {
    try { await navigator.clipboard.writeText(code); }
    catch { /* swallow */ }
    setCopied(true);
    setTimeout(() => setCopied(false), 1800);
  };
  return (
    <div ref={ref} className="my-3 rounded-md border border-line bg-ink-2 overflow-hidden">
      <div className="flex items-center justify-between px-3 py-1.5 border-b border-line bg-panel-2">
        <span className="text-[11px] font-mono text-ivory-faint">{language}</span>
        <button
          type="button"
          onClick={handleCopy}
          className="inline-flex items-center gap-1 text-[11px] text-ivory-faint hover:text-ivory transition-colors"
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <SyntaxHighlighter
        language={language}
        style={prismStyle}
        wrapLongLines
        customStyle={{
          margin: 0,
          padding: '12px 16px',
          background: 'transparent',
          fontSize: '12.5px',
          fontFamily: 'var(--font-mono)',
          lineHeight: '1.6',
        }}
        codeTagProps={{ style: { background: 'transparent', fontFamily: 'var(--font-mono)' } }}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  );
}
