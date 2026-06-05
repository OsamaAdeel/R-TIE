// Settings menu — bulk "Export all chats" serialization.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { exportAllSessionsToMarkdown, exportSessionToMarkdown } from './exportMarkdown.js';

// Freeze time so the `Exported:` timestamps (new Date().toISOString()) are
// identical across calls — otherwise the verbatim-substring check below races
// the millisecond boundary between the bulk export and a standalone export.
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-06-05T12:00:00Z')); });
afterEach(() => { vi.useRealTimers(); });

const SESSIONS = [
  {
    id: 's1', title: 'Trace N_EOP_BAL', createdAt: 0,
    messages: [
      { role: 'user', content: 'what writes N_EOP_BAL?' },
      { role: 'assistant', data: { explanation: { markdown: 'FN_LOAD_OPS_RISK_DATA writes it.' } } },
    ],
  },
  {
    id: 's2', title: 'CAP943 derivation', createdAt: 0,
    messages: [{ role: 'user', content: 'how is CAP943 derived?' }],
  },
  { id: 's3', title: 'Empty conversation', createdAt: 0, messages: [] },
];

describe('exportAllSessionsToMarkdown', () => {
  it('emits a header with conversation + total-message counts', () => {
    const md = exportAllSessionsToMarkdown(SESSIONS);
    expect(md).toContain('# R-TIE — all conversations');
    expect(md).toContain('**Conversations:** 3');
    expect(md).toContain('**Messages (total):** 3'); // 2 + 1 + 0
  });

  it('includes every session (by its own title heading)', () => {
    const md = exportAllSessionsToMarkdown(SESSIONS);
    expect(md).toContain('# Trace N_EOP_BAL');
    expect(md).toContain('# CAP943 derivation');
    expect(md).toContain('# Empty conversation');
  });

  it('reuses the per-session serialization verbatim', () => {
    const md = exportAllSessionsToMarkdown(SESSIONS);
    // The single-session export of s1 must appear as a substring of the bulk doc.
    expect(md).toContain(exportSessionToMarkdown(SESSIONS[0]));
  });

  it('handles an empty / missing session list without throwing', () => {
    expect(exportAllSessionsToMarkdown([])).toContain('**Conversations:** 0');
    expect(() => exportAllSessionsToMarkdown(undefined)).not.toThrow();
    expect(exportAllSessionsToMarkdown(undefined)).toContain('**Conversations:** 0');
  });
});
