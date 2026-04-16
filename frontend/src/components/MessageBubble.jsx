import { User, Bot } from 'lucide-react';
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
