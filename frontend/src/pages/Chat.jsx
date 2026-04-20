import { useRef, useEffect, useState } from 'react';
import MessageBubble from '../components/MessageBubble';
import ChatInput from '../components/ChatInput';
import ModelSelector from '../components/ModelSelector';
import { ArrowRight, ChevronDown } from 'lucide-react';

const NEAR_BOTTOM_THRESHOLD = 100;

export default function Chat({ session, onSend, loading, provider, model, onProviderChange, onModelChange }) {
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

  return (
    <div className="flex-1 flex flex-col h-screen bg-bg-primary">
      {/* Top bar */}
      <div className="flex items-center justify-between px-5 py-2.5 border-b border-border bg-bg-secondary shadow-sm">
        <span className="text-xs text-text-muted font-medium">
          {session?.messages.length || 0} messages
        </span>
        <ModelSelector
          provider={provider}
          model={model}
          onProviderChange={onProviderChange}
          onModelChange={onModelChange}
        />
      </div>

      {/* Messages area */}
      <div className="relative flex-1 overflow-hidden">
        <div ref={scrollRef} className="absolute inset-0 overflow-y-auto">
          {session?.messages.length === 0 ? (
            <EmptyState onSend={onSend} />
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
            className="absolute bottom-4 left-1/2 -translate-x-1/2 z-10 inline-flex items-center justify-center h-9 w-9 rounded-full bg-bg-secondary border border-border shadow-md text-text-secondary hover:text-text-primary hover:bg-bg-tertiary hover:border-border-strong transition-colors"
          >
            <ChevronDown size={18} strokeWidth={2.25} />
          </button>
        )}
      </div>

      {/* Input */}
      <ChatInput onSend={onSend} disabled={loading} />
    </div>
  );
}

function EmptyState({ onSend }) {
  const examples = [
    { text: 'Explain the logic of FN_LOAD_OPS_RISK_DATA', icon: '🔍' },
    { text: 'What does POPULATE_PP_FROMGL do?', icon: '📊' },
    { text: '/cache-list', icon: '💾' },
    { text: '/refresh-schema', icon: '🔄' },
  ];

  return (
    <div className="h-full flex items-center justify-center p-8">
      <div className="text-center max-w-lg">
        {/* Wordmark — serif display, Claude-style brand moment */}
        <h1
          className="mb-3 text-text-primary"
          style={{
            fontFamily: "'Instrument Serif', 'Times New Roman', serif",
            fontStyle: 'italic',
            fontWeight: 400,
            fontSize: '76px',
            lineHeight: 1,
            letterSpacing: '-0.02em',
          }}
        >
          R-TIE
        </h1>
        <p className="text-sm text-text-secondary mb-10 leading-relaxed max-w-md mx-auto">
          Trace, explain, and reason about your Oracle OFSAA system — with
          cited answers grounded in the source.
        </p>

        {/* Example cards */}
        <div className="space-y-2.5 text-left">
          {examples.map((example) => (
            <button
              key={example.text}
              onClick={() => onSend(example.text)}
              className="w-full flex items-center gap-3 text-sm text-text-secondary bg-bg-secondary rounded-lg px-4 py-2.5 hover:bg-bg-tertiary hover:text-text-primary cursor-pointer transition-colors duration-150 group"
            >
              <span className="text-base">{example.icon}</span>
              <span className="flex-1 font-medium">{example.text}</span>
              <ArrowRight size={13} className="text-text-muted group-hover:translate-x-0.5 transition-transform" />
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
