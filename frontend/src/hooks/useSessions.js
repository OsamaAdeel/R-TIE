import { useState, useCallback } from 'react';

function generateId() {
  return crypto.randomUUID();
}

function createSession() {
  return {
    id: generateId(),
    title: 'New conversation',
    messages: [],
    createdAt: Date.now(),
  };
}

export function useSessions() {
  const [sessions, setSessions] = useState(() => {
    const saved = localStorage.getItem('rtie_sessions');
    if (saved) {
      try { return JSON.parse(saved); } catch { /* ignore */ }
    }
    const first = createSession();
    return [first];
  });

  const [activeId, setActiveId] = useState(() => {
    return sessions[0]?.id || null;
  });

  const persist = useCallback((updated) => {
    localStorage.setItem('rtie_sessions', JSON.stringify(updated));
  }, []);

  const activeSession = sessions.find((s) => s.id === activeId) || sessions[0];

  const addSession = useCallback(() => {
    let created = null;
    setSessions((prev) => {
      const empty = prev.find((s) => s.messages.length === 0);
      if (empty) {
        setActiveId(empty.id);
        return prev;
      }
      created = createSession();
      const next = [created, ...prev];
      persist(next);
      return next;
    });
    if (created) setActiveId(created.id);
    return created;
  }, [persist]);

  const deleteSession = useCallback((id) => {
    setSessions((prev) => {
      let next = prev.filter((s) => s.id !== id);
      if (next.length === 0) next = [createSession()];
      persist(next);
      if (activeId === id) setActiveId(next[0].id);
      return next;
    });
  }, [activeId, persist]);

  const addMessage = useCallback((sessionId, message) => {
    setSessions((prev) => {
      const next = prev.map((s) => {
        if (s.id !== sessionId) return s;
        const messages = [...s.messages, message];
        const title = s.messages.length === 0 && message.role === 'user'
          ? message.content.slice(0, 50)
          : s.title;
        return { ...s, messages, title };
      });
      persist(next);
      return next;
    });
  }, [persist]);

  const updateLastMessage = useCallback((sessionId, updater) => {
    setSessions((prev) => {
      const next = prev.map((s) => {
        if (s.id !== sessionId) return s;
        const messages = [...s.messages];
        if (messages.length > 0) {
          messages[messages.length - 1] = updater(messages[messages.length - 1]);
        }
        return { ...s, messages };
      });
      persist(next);
      return next;
    });
  }, [persist]);

  return {
    sessions,
    activeSession,
    activeId,
    setActiveId,
    addSession,
    deleteSession,
    addMessage,
    updateLastMessage,
  };
}
