// Root-load selection — bare root opens a fresh trace, deep-links are honored.
import { describe, it, expect } from 'vitest';
import { parseConversationId, resolveInitialStore, resolveAfterDelete } from './useSessions.js';

const withMsgs = (id) => ({ id, title: id, messages: [{ role: 'user', content: 'hi' }], createdAt: 0 });
const empty = (id) => ({ id, title: id, messages: [], createdAt: 0 });

describe('parseConversationId', () => {
  it('reads a #/chat/<id> hash deep-link', () => {
    expect(parseConversationId('', '#/chat/abc-123')).toBe('abc-123');
  });
  it('reads a ?c= / ?conversation= query deep-link', () => {
    expect(parseConversationId('?c=xyz', '')).toBe('xyz');
    expect(parseConversationId('?conversation=q9', '')).toBe('q9');
  });
  it('returns null for a bare root (no id anywhere)', () => {
    expect(parseConversationId('', '')).toBeNull();
    expect(parseConversationId('?foo=bar', '#section')).toBeNull();
  });
});

describe('resolveInitialStore — bare root', () => {
  it('opens a fresh empty trace instead of the most-recent conversation', () => {
    const saved = [withMsgs('a'), withMsgs('b')];
    const { sessions, activeId } = resolveInitialStore(saved, null);
    // A new empty trace is prepended and selected — NOT saved[0].
    expect(sessions[0].messages).toHaveLength(0);
    expect(activeId).toBe(sessions[0].id);
    expect(activeId).not.toBe('a');
  });

  it('preserves all existing conversations (prepend, never drop)', () => {
    const saved = [withMsgs('a'), withMsgs('b')];
    const { sessions } = resolveInitialStore(saved, null);
    expect(sessions.map((s) => s.id)).toEqual(expect.arrayContaining(['a', 'b']));
    expect(sessions).toHaveLength(3); // 2 preserved + 1 fresh
  });

  it('reuses an existing empty trace rather than piling up new ones', () => {
    const saved = [empty('e0'), withMsgs('a')];
    const { sessions, activeId } = resolveInitialStore(saved, null);
    expect(sessions).toHaveLength(2);      // no extra empty added
    expect(activeId).toBe('e0');
  });

  it('falls back to a single fresh session when nothing is stored', () => {
    const { sessions, activeId } = resolveInitialStore(null, null);
    expect(sessions).toHaveLength(1);
    expect(sessions[0].messages).toHaveLength(0);
    expect(activeId).toBe(sessions[0].id);
  });
});

describe('resolveInitialStore — deep-link', () => {
  it('opens the targeted conversation and does NOT prepend a fresh trace', () => {
    const saved = [withMsgs('a'), withMsgs('b')];
    const { sessions, activeId } = resolveInitialStore(saved, 'b');
    expect(activeId).toBe('b');
    expect(sessions).toHaveLength(2);      // unchanged — no fresh trace
    expect(sessions).toBe(saved);
  });

  it('ignores a deep-link id that matches no stored conversation (→ fresh root)', () => {
    const saved = [withMsgs('a')];
    const { sessions, activeId } = resolveInitialStore(saved, 'ghost');
    expect(sessions[0].messages).toHaveLength(0);
    expect(activeId).toBe(sessions[0].id);
    expect(activeId).not.toBe('ghost');
  });
});

describe('resolveAfterDelete', () => {
  it('routes to a FRESH empty trace when the active conversation is deleted', () => {
    const saved = [withMsgs('a'), withMsgs('b')];
    const { sessions, activeId } = resolveAfterDelete(saved, 'a', 'a'); // delete the active one
    expect(sessions.some((s) => s.id === 'a')).toBe(false); // gone
    expect(sessions.some((s) => s.id === 'b')).toBe(true);  // history kept
    // Selection is a new empty trace, NOT the remaining 'b'.
    const sel = sessions.find((s) => s.id === activeId);
    expect(sel.messages).toHaveLength(0);
    expect(activeId).not.toBe('b');
  });

  it('reuses an existing empty trace rather than minting another', () => {
    const saved = [empty('e0'), withMsgs('a')];
    const { sessions, activeId } = resolveAfterDelete(saved, 'a', 'a');
    expect(sessions).toHaveLength(1);
    expect(activeId).toBe('e0');
  });

  it('leaves the selection untouched when deleting a background conversation', () => {
    const saved = [withMsgs('a'), withMsgs('b')];
    const { sessions, activeId } = resolveAfterDelete(saved, 'b', 'a'); // active is 'a'
    expect(activeId).toBe('a');
    expect(sessions.map((s) => s.id)).toEqual(['a']);
  });

  it('falls back to a single fresh session when the last one is deleted', () => {
    const saved = [withMsgs('a')];
    const { sessions, activeId } = resolveAfterDelete(saved, 'a', 'a');
    expect(sessions).toHaveLength(1);
    expect(sessions[0].messages).toHaveLength(0);
    expect(activeId).toBe(sessions[0].id);
  });
});
