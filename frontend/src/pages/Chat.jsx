import { useRef, useEffect, useState } from 'react';
import MessageBubble from '../components/MessageBubble';
import ChatInput from '../components/ChatInput';
import Topbar from '../components/Topbar';
import BrandMark from '../components/BrandMark';
import { ChevronDown } from 'lucide-react';

const NEAR_BOTTOM_THRESHOLD = 100;

export default function Chat({
  session,
  onSend,
  onStop,
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
              <Hero />
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
          onStop={onStop}
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

function Hero() {
  return (
    <div className="min-h-full flex items-center justify-center px-8 py-6">
      <div className="w-full max-w-[600px] flex flex-col items-center text-center">
        <div className="text-gold mb-3">
          <BrandMark size={96} />
        </div>

        {/* R-TIE wordmark — hyphen rendered in the brand accent color */}
        <h1
          className="text-ivory leading-none mb-2"
          style={{
            fontFamily: 'var(--font-display)',
            fontWeight: 700,
            fontSize: '56px',
            letterSpacing: '-0.02em',
          }}
        >
          R<span className="text-gold">-</span>TIE
        </h1>

        <div className="flex items-center gap-1.5 text-[10.5px] uppercase tracking-[0.18em] text-ivory-faint mb-4">
          <span>Regulatory</span><span className="text-gold-dim">·</span>
          <span>Trace</span><span className="text-gold-dim">·</span>
          <span>Intelligence</span><span className="text-gold-dim">·</span>
          <span>Engine</span>
        </div>

        <p className="text-[13px] text-ivory-dim leading-relaxed max-w-[460px]">
          Read your Oracle OFSAA system the way an auditor would. Every claim
          grounded in the exact line of PL/SQL it came from.{' '}
          <em className="text-gold-dim not-italic">No hallucinations, no guessing.</em>
        </p>
      </div>
    </div>
  );
}
