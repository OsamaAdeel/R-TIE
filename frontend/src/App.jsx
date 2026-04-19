import { useState, useCallback } from 'react';
import Sidebar from './components/Sidebar';
import Chat from './pages/Chat';
import { useSessions } from './hooks/useSessions';
import { useHealth } from './hooks/useHealth';
import { streamQuery } from './api/client';

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

      // Add placeholder assistant message with streaming state
      addMessage(sid, {
        role: 'assistant',
        loading: true,
        streaming: true,
        streamedMarkdown: '',
        data: null,
        error: null,
      });
      setLoading(true);

      let meta = null;

      await streamQuery(text, sid, ENGINEER_ID, provider, model, {
        onStage: (stageData) => {
          updateLastMessage(sid, (msg) => ({
            ...msg,
            stage: stageData,
          }));
        },

        onMeta: (metaData) => {
          meta = metaData;
          updateLastMessage(sid, (msg) => ({
            ...msg,
            loading: false,
            meta: metaData,
          }));
        },

        onToken: (token) => {
          updateLastMessage(sid, (msg) => ({
            ...msg,
            loading: false,
            streamedMarkdown: (msg.streamedMarkdown || '') + token,
          }));
        },

        onDone: (finalPayload) => {
          updateLastMessage(sid, (msg) => {
            const fullMarkdown = msg.streamedMarkdown || '';
            return {
              ...msg,
              loading: false,
              streaming: false,
              streamedMarkdown: undefined,
              data: {
                ...finalPayload,
                ...(meta || {}),
                explanation: {
                  markdown: fullMarkdown,
                },
              },
            };
          });
          setLoading(false);
        },

        onClarification: (payload) => {
          updateLastMessage(sid, (msg) => ({
            ...msg,
            loading: false,
            streaming: false,
            streamedMarkdown: undefined,
            stage: undefined,
            meta: undefined,
            data: null,
            clarification: {
              message: payload?.message || 'Could you clarify your request?',
            },
          }));
          setLoading(false);
        },

        onError: (errorMsg) => {
          updateLastMessage(sid, (msg) => ({
            ...msg,
            loading: false,
            streaming: false,
            error: errorMsg,
          }));
          setLoading(false);
        },
      });
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
