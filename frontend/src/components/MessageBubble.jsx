import { User, Bot } from 'lucide-react';
import ResponseCard from './ResponseCard';
import CommandResult from './CommandResult';

export default function MessageBubble({ message }) {
  const isUser = message.role === 'user';

  if (isUser) {
    return (
      <div className="flex gap-3 justify-end">
        <div className="max-w-2xl">
          <div className="bg-accent/15 border border-accent/25 rounded-2xl rounded-br-sm px-4 py-3">
            <p className="text-sm text-text-primary whitespace-pre-wrap">{message.content}</p>
          </div>
        </div>
        <div className="w-8 h-8 rounded-full bg-bg-tertiary border border-border flex items-center justify-center shrink-0">
          <User size={14} className="text-text-secondary" />
        </div>
      </div>
    );
  }

  // Assistant message
  const data = message.data;

  return (
    <div className="flex gap-3">
      <div className="w-8 h-8 rounded-full bg-accent/15 border border-accent/25 flex items-center justify-center shrink-0">
        <Bot size={14} className="text-accent" />
      </div>
      <div className="max-w-4xl flex-1 min-w-0">
        {message.loading ? (
          <LoadingIndicator />
        ) : message.error ? (
          <ErrorCard error={message.error} />
        ) : data?.type === 'command' ? (
          <CommandResult result={data.result} correlationId={data.correlation_id} />
        ) : (
          <ResponseCard data={data} />
        )}
      </div>
    </div>
  );
}

function LoadingIndicator() {
  return (
    <div className="bg-bg-secondary border border-border rounded-2xl rounded-bl-sm px-4 py-4">
      <div className="flex items-center gap-3">
        <div className="flex gap-1">
          <div className="w-2 h-2 rounded-full bg-accent animate-bounce" style={{ animationDelay: '0ms' }} />
          <div className="w-2 h-2 rounded-full bg-accent animate-bounce" style={{ animationDelay: '150ms' }} />
          <div className="w-2 h-2 rounded-full bg-accent animate-bounce" style={{ animationDelay: '300ms' }} />
        </div>
        <span className="text-sm text-text-muted">Analyzing PL/SQL logic...</span>
      </div>
    </div>
  );
}

function ErrorCard({ error }) {
  return (
    <div className="bg-error/10 border border-error/25 rounded-2xl rounded-bl-sm px-4 py-3">
      <p className="text-sm text-error font-medium">Error</p>
      <p className="text-sm text-text-secondary mt-1">{error}</p>
    </div>
  );
}
