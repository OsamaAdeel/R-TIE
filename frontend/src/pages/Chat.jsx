import { useRef, useEffect, useState } from 'react';
import MessageBubble from '../components/MessageBubble';
import ChatInput from '../components/ChatInput';
import Topbar from '../components/Topbar';
import BrandMark from '../components/BrandMark';
import { ArrowRight, ChevronDown, HelpCircle, GitBranch, Slash, RotateCw } from 'lucide-react';

const NEAR_BOTTOM_THRESHOLD = 100;

export default function Chat({
  session,
  onSend,
  loading,
  provider,
  model,
  onProviderChange,
  onModelChange,
  isStarred,
  onStarActive,
  onRenameActive,
  onDeleteActive,
}) {
  const scrollRef = useRef(null);
  const messagesEndRef = useRef(null);
  const [isAtBottom, setIsAtBottom] = useState(true);
  const messageCount = session?.messages?.length || 0;

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => {
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM_THRESHOLD;
      setIsAtBottom(atBottom);
    };
    onScroll();
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, [messageCount]);

  // Only auto-scroll when a new message is added AND the user was already near the bottom.
  // Skipping when they've scrolled up avoids yanking them away while they're reading.
  useEffect(() => {
    if (!isAtBottom) return;
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messageCount, isAtBottom]);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const showScrollButton = messageCount > 0 && !isAtBottom;

  const title = session?.messages.length === 0 ? 'New trace' : (session?.title || 'New trace');

  return (
    <div className="flex-1 flex flex-col h-screen bg-ink text-ivory relative">
      {/* Subtle gold/burgundy radial wash, lifted from the prototype's
          `.app::before` so the surface doesn't feel flat. The actual
          gradient is themed via the `--gradient-chat-wash` CSS var. */}
      <div className="rtie-chat-wash absolute inset-0 pointer-events-none" />

      <div className="relative flex-1 flex flex-col min-h-0">
        <Topbar
          title={title}
          msgCount={messageCount}
          isStarred={isStarred}
          onStar={onStarActive}
          onRename={onRenameActive}
          onDelete={onDeleteActive}
        />

        {/* Messages area */}
        <div className="relative flex-1 overflow-hidden">
          <div ref={scrollRef} className="absolute inset-0 overflow-y-auto">
            {session?.messages.length === 0 ? (
              <Hero onPick={onSend} />
            ) : (
              <div className="max-w-4xl mx-auto py-6 px-4 space-y-6">
                {session.messages.map((msg, i) => (
                  <MessageBubble
                    key={i}
                    message={msg}
                    onRetry={msg.role === 'user' ? () => onSend(msg.content) : undefined}
                    onEdit={msg.role === 'user' ? (newText) => onSend(newText) : undefined}
                  />
                ))}
                <div ref={messagesEndRef} />
              </div>
            )}
          </div>
          {showScrollButton && (
            <button
              type="button"
              onClick={scrollToBottom}
              aria-label="Scroll to latest message"
              className="absolute bottom-4 left-1/2 -translate-x-1/2 z-10 inline-flex items-center justify-center h-9 w-9 rounded-full bg-panel-2 border border-line-strong text-ivory-dim hover:text-ivory hover:bg-panel hover:border-line-gold transition-colors"
            >
              <ChevronDown size={18} strokeWidth={2.25} />
            </button>
          )}
        </div>

        <ChatInput
          onSend={onSend}
          disabled={loading}
          provider={provider}
          model={model}
          onProviderChange={onProviderChange}
          onModelChange={onModelChange}
        />
      </div>
    </div>
  );
}

// Suggestions are typed (q = question, t = trace, s = slash, c = command) so
// each row gets a kind-prefixed glyph. Clicking a suggestion submits it as
// the user's first message — same path as typing it manually.
const SUGGESTIONS = [
  { kind: 'q', label: 'Explain the logic of FN_LOAD_OPS_RISK_DATA', desc: 'Trace 14 PL/SQL functions across 3 packages' },
  { kind: 'q', label: 'What does POPULATE_PP_FROMGL do?', desc: 'Loads from GL_DATA into FCT_PP_LOSS — Basel III RWA' },
  { kind: 't', label: 'Trace lineage of column N_ANNUAL_GROSS_INCOME', desc: 'Source → staging → fact → reporting line' },
  { kind: 's', label: '/cache-list', desc: 'Show cached function indexes & their TTL' },
  { kind: 'c', label: '/refresh-schema', desc: 'Re-introspect indexed PL/SQL objects' },
];

const KIND_GLYPH = { q: HelpCircle, t: GitBranch, s: Slash, c: RotateCw };

function Hero({ onPick }) {
  return (
    <div className="h-full flex items-center justify-center p-8">
      <div className="w-full max-w-[640px] flex flex-col items-center text-center">
        {/* Brand mark — large lineage glyph */}
        <div className="text-gold mb-5">
          <BrandMark size={144} />
        </div>

        {/* R-TIE wordmark — hyphen rendered in the brand accent color */}
        <h1
          className="text-ivory leading-none mb-3"
          style={{
            fontFamily: 'var(--font-display)',
            fontWeight: 700,
            fontSize: '72px',
            letterSpacing: '-0.02em',
          }}
        >
          R<span className="text-gold">-</span>TIE
        </h1>

        <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.18em] text-ivory-faint mb-4">
          <span>Regulatory</span><span className="text-gold-dim">·</span>
          <span>Trace</span><span className="text-gold-dim">·</span>
          <span>Intelligence</span><span className="text-gold-dim">·</span>
          <span>Engine</span>
        </div>

        <p className="text-[13px] text-ivory-dim leading-relaxed max-w-[480px] mb-10">
          Read your Oracle OFSAA system the way an auditor would. Every claim
          grounded in the exact line of PL/SQL it came from.{' '}
          <em className="text-gold-dim not-italic">No hallucinations, no guessing.</em>
        </p>

        <div className="w-full space-y-2 text-left">
          {SUGGESTIONS.map((s) => {
            const Glyph = KIND_GLYPH[s.kind];
            return (
              <button
                key={s.label}
                onClick={() => onPick(s.label)}
                className="w-full flex items-center gap-3 text-left bg-panel border border-line rounded-[10px] px-4 py-3 hover:border-line-gold hover:bg-panel-2 transition-colors group"
              >
                <span className="w-6 h-6 grid place-items-center rounded-md bg-gold-soft text-gold shrink-0">
                  <Glyph size={13} strokeWidth={2.2} />
                </span>
                <span className="flex-1 min-w-0">
                  <span className="block text-[13.5px] font-medium text-ivory truncate">{s.label}</span>
                  <span className="block text-[11.5px] text-ivory-faint truncate mt-0.5">{s.desc}</span>
                </span>
                <ArrowRight size={14} className="text-ivory-faint group-hover:text-gold group-hover:translate-x-0.5 transition-all shrink-0" />
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
