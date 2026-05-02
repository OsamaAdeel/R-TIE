import { useState, useCallback, useRef, useEffect } from 'react';

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

  // Mirror sessions into a ref so addSession can read fresh state without
  // relying on the setState updater for side effects.
  const sessionsRef = useRef(sessions);
  useEffect(() => { sessionsRef.current = sessions; }, [sessions]);

  // "New trace" — switch to an existing empty conversation if one exists,
  // otherwise create a new one and switch. The activeId update lives
  // outside the setSessions updater because React updater functions must
  // be pure (under StrictMode they may run twice, dropping side effects).
  const addSession = useCallback(() => {
    const existingEmpty = sessionsRef.current.find((s) => s.messages.length === 0);
    if (existingEmpty) {
      setActiveId(existingEmpty.id);
      return existingEmpty;
    }
    const created = createSession();
    setSessions((prev) => {
      const next = [created, ...prev];
      persist(next);
      return next;
    });
    setActiveId(created.id);
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

  const renameSession = useCallback((id, title) => {
    const trimmed = (title || '').trim();
    if (!trimmed) return;
    setSessions((prev) => {
      const next = prev.map((s) => (s.id === id ? { ...s, title: trimmed } : s));
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
    renameSession,
  };
}
