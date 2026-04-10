import { useState, useCallback } from 'react';
import Sidebar from './components/Sidebar';
import Chat from './pages/Chat';
import { useSessions } from './hooks/useSessions';
import { useHealth } from './hooks/useHealth';
import { sendQuery } from './api/client';

const ENGINEER_ID = 'engineer@rtie.local';

export default function App() {
  const {
    sessions,
    activeSession,
    activeId,
    setActiveId,
    addSession,
    deleteSession,
    addMessage,
    updateLastMessage,
  } = useSessions();

  const { health } = useHealth();
  const [loading, setLoading] = useState(false);
  const [provider, setProvider] = useState(null);
  const [model, setModel] = useState(null);

  const handleSend = useCallback(
    async (text) => {
      if (!activeSession) return;
      const sid = activeSession.id;

      // Add user message
      addMessage(sid, { role: 'user', content: text });

      // Add placeholder assistant message
      addMessage(sid, { role: 'assistant', loading: true, data: null, error: null });
      setLoading(true);

      try {
        const data = await sendQuery(text, sid, ENGINEER_ID, provider, model);
        updateLastMessage(sid, (msg) => ({
          ...msg,
          loading: false,
          data,
        }));
      } catch (err) {
        updateLastMessage(sid, (msg) => ({
          ...msg,
          loading: false,
          error: err.message,
        }));
      } finally {
        setLoading(false);
      }
    },
    [activeSession, addMessage, updateLastMessage, provider, model]
  );

  return (
    <div className="flex h-screen overflow-hidden bg-bg-primary">
      <Sidebar
        sessions={sessions}
        activeId={activeId}
        onSelect={setActiveId}
        onNew={addSession}
        onDelete={deleteSession}
        health={health}
      />
      <Chat
        session={activeSession}
        onSend={handleSend}
        loading={loading}
        provider={provider}
        model={model}
        onProviderChange={setProvider}
        onModelChange={setModel}
      />
    </div>
  );
}
