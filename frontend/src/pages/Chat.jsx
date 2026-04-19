import { useRef, useEffect } from 'react';
import MessageBubble from '../components/MessageBubble';
import ChatInput from '../components/ChatInput';
import ModelSelector from '../components/ModelSelector';
import { ArrowRight } from 'lucide-react';

export default function Chat({ session, onSend, loading, provider, model, onProviderChange, onModelChange }) {
  const messagesEndRef = useRef(null);
  const messageCount = session?.messages?.length || 0;

  // Only auto-scroll when a new message is added, not during streaming updates
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messageCount]);

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
      <div className="flex-1 overflow-y-auto">
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
              className="w-full flex items-center gap-3 text-sm text-text-secondary bg-bg-secondary rounded-xl px-5 py-3.5 border-2 border-border hover:border-accent hover:text-accent hover:shadow-lg hover:shadow-accent/5 cursor-pointer transition-all duration-200 group"
            >
              <span className="text-lg">{example.icon}</span>
              <span className="flex-1 font-medium">{example.text}</span>
              <ArrowRight size={14} className="text-text-muted group-hover:text-accent group-hover:translate-x-0.5 transition-all" />
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
