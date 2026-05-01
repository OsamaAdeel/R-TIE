import { useState, useCallback, useEffect } from 'react';
import Sidebar from './components/Sidebar';
import Chat from './pages/Chat';
import { useSessions } from './hooks/useSessions';
import { useHealth } from './hooks/useHealth';
import { useTheme } from './hooks/useTheme';
import { streamQuery } from './api/client';

const ENGINEER_ID = 'engineer@rtie.local';

// Starred conversations are local-only — there is no backend trace-list /
// star endpoint (TODO(backend): persist server-side if/when conversations
// move off localStorage).
const STARRED_KEY = 'rtie.starred';

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
    renameSession,
  } = useSessions();

  const { health } = useHealth();
  const { theme, toggleTheme } = useTheme();
  const [loading, setLoading] = useState(false);
  const [provider, setProvider] = useState(null);
  const [model, setModel] = useState(null);

  const [starredIds, setStarredIds] = useState(() => {
    try {
      const raw = localStorage.getItem(STARRED_KEY);
      return new Set(raw ? JSON.parse(raw) : []);
    } catch {
      return new Set();
    }
  });

  useEffect(() => {
    try { localStorage.setItem(STARRED_KEY, JSON.stringify([...starredIds])); }
    catch { /* ignore */ }
  }, [starredIds]);

  const toggleStar = useCallback((id) => {
    setStarredIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleDelete = useCallback((id) => {
    deleteSession(id);
    setStarredIds((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }, [deleteSession]);

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
    <div className="flex h-screen overflow-hidden bg-ink text-ivory">
      <Sidebar
        sessions={sessions}
        activeId={activeId}
        starredIds={starredIds}
        onSelect={setActiveId}
        onNew={addSession}
        onDelete={handleDelete}
        onRename={renameSession}
        onStar={toggleStar}
        health={health}
        theme={theme}
        onToggleTheme={toggleTheme}
      />
      <Chat
        session={activeSession}
        onSend={handleSend}
        loading={loading}
        provider={provider}
        model={model}
        onProviderChange={setProvider}
        onModelChange={setModel}
        isStarred={activeId ? starredIds.has(activeId) : false}
        onStarActive={activeId ? () => toggleStar(activeId) : undefined}
        onRenameActive={activeId ? (next) => renameSession(activeId, next) : undefined}
        onDeleteActive={activeId ? () => handleDelete(activeId) : undefined}
      />
    </div>
  );
}
