import { useRef, useEffect } from 'react';
import MessageBubble from '../components/MessageBubble';
import ChatInput from '../components/ChatInput';
import ModelSelector from '../components/ModelSelector';
import { Database } from 'lucide-react';

export default function Chat({ session, onSend, loading, provider, model, onProviderChange, onModelChange }) {
  const messagesEndRef = useRef(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [session?.messages]);

  return (
    <div className="flex-1 flex flex-col h-screen">
      {/* Top bar with model selector */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-border bg-bg-secondary/50">
        <span className="text-xs text-text-muted">
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
          <EmptyState />
        ) : (
          <div className="max-w-4xl mx-auto py-6 px-4 space-y-6">
            {session.messages.map((msg, i) => (
              <MessageBubble key={i} message={msg} />
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

function EmptyState() {
  return (
    <div className="h-full flex items-center justify-center">
      <div className="text-center max-w-md">
        <div className="w-16 h-16 rounded-2xl bg-accent/10 border border-accent/25 flex items-center justify-center mx-auto mb-4">
          <Database size={28} className="text-accent" />
        </div>
        <h2 className="text-xl font-semibold text-text-primary mb-2">
          RTIE — Logic Explorer
        </h2>
        <p className="text-sm text-text-secondary mb-6 leading-relaxed">
          Ask about any PL/SQL function or procedure in Oracle OFSAA.
          Get instant, fully-cited logic explanations with formula breakdowns.
        </p>
        <div className="space-y-2 text-left">
          {[
            'Explain the logic of FN_CALC_RWA',
            'What does SP_PROCESS_GL_DATA do?',
            '/cache-list',
            '/refresh-schema',
          ].map((example) => (
            <div
              key={example}
              className="text-sm text-text-muted bg-bg-tertiary rounded-lg px-4 py-2.5 border border-border hover:border-accent/30 hover:text-accent cursor-default transition-colors"
            >
              {example}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
