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

// Read an optional conversation deep-link id from the URL. The app has no
// router today, so nothing currently PRODUCES these — this is the forward-
// compatible reader: honor `#/chat/<id>` (hash, needs no server rewrite) or a
// `?c=<id>` / `?conversation=<id>` query param if one is ever present. Pure +
// exported for testing.
export function parseConversationId(search, hash) {
  try {
    const m = (hash || '').match(/#\/chat\/([^/?#]+)/);
    if (m) return decodeURIComponent(m[1]);
    const params = new URLSearchParams(search || '');
    return params.get('c') || params.get('conversation') || null;
  } catch {
    return null;
  }
}

// Decide what's loaded + selected on initial mount. A deep-link id (if it
// matches a stored conversation) wins and opens THAT conversation. Otherwise a
// BARE root opens a fresh empty trace — reusing an existing empty one if
// present (matching the "New trace" button), else prepending a new one.
// History is always preserved: we prepend, never drop. Pure + exported for
// testing. Returns { sessions, activeId }.
export function resolveInitialStore(savedSessions, linkId) {
  let list = Array.isArray(savedSessions) && savedSessions.length
    ? savedSessions
    : [createSession()];

  // Deep-link to a specific, existing conversation → honor it verbatim.
  if (linkId && list.some((s) => s.id === linkId)) {
    return { sessions: list, activeId: linkId };
  }

  // Bare root → a fresh empty trace, NOT the most-recent conversation.
  let empty = list.find((s) => (s.messages || []).length === 0);
  if (!empty) {
    empty = createSession();
    list = [empty, ...list];
  }
  return { sessions: list, activeId: empty.id };
}

// Decide the store + selection after deleting one conversation. Deleting the
// ACTIVE conversation routes to the fresh empty state (reuse an existing empty
// trace, else create one) — NOT another existing chat — consistent with the
// bare-root-opens-a-new-trace behavior. Deleting a background conversation
// leaves the selection untouched. Pure + exported for testing.
export function resolveAfterDelete(sessions, deletedId, activeId) {
  const prev = Array.isArray(sessions) ? sessions : [];
  let next = prev.filter((s) => s.id !== deletedId);

  if (next.length === 0) {
    const fresh = createSession();
    return { sessions: [fresh], activeId: fresh.id };
  }

  if (activeId === deletedId) {
    let empty = next.find((s) => (s.messages || []).length === 0);
    if (!empty) {
      empty = createSession();
      next = [empty, ...next];
    }
    return { sessions: next, activeId: empty.id };
  }

  return { sessions: next, activeId };
}

function loadSavedSessions() {
  try {
    const saved = localStorage.getItem('rtie_sessions');
    if (saved) return JSON.parse(saved);
  } catch {
    /* ignore */
  }
  return null;
}

export function useSessions() {
  // Compute the initial load ONCE so sessions + activeId stay consistent (a
  // fresh empty trace prepended on bare root must be the one we select). A
  // deep-link in the URL is honored; otherwise root opens a new empty trace
  // instead of the most-recent conversation.
  const [initial] = useState(() =>
    resolveInitialStore(
      loadSavedSessions(),
      parseConversationId(window.location.search, window.location.hash),
    ),
  );

  const [sessions, setSessions] = useState(initial.sessions);
  const [activeId, setActiveId] = useState(initial.activeId);

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

  // Read fresh state from the ref and compute the next store outside the
  // setSessions updater — createSession() must not run inside the updater (it
  // can double-fire under StrictMode and mint mismatched ids), same reasoning
  // as addSession above.
  const deleteSession = useCallback((id) => {
    const { sessions: next, activeId: nextActive } = resolveAfterDelete(
      sessionsRef.current, id, activeId,
    );
    setSessions(next);
    persist(next);
    setActiveId(nextActive);
  }, [activeId, persist]);

  // "Delete all chats" — wipe the entire conversation store and reset to a
  // single fresh, empty conversation (never leave zero sessions, mirroring
  // deleteSession). This is the only client store that holds chat history;
  // the caller is responsible for clearing adjacent state (e.g. starred ids).
  const clearAllSessions = useCallback(() => {
    const fresh = createSession();
    setSessions([fresh]);
    setActiveId(fresh.id);
    persist([fresh]);
  }, [persist]);

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
    clearAllSessions,
    addMessage,
    updateLastMessage,
    renameSession,
  };
}
