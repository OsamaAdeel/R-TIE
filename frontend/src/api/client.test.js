// W151 Phase 5 — fetchSource request/response contract.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { fetchSource } from './client.js';

afterEach(() => { vi.restoreAllMocks(); });

describe('fetchSource', () => {
  it('POSTs {function, schema, line_start, line_end} and returns parsed JSON', async () => {
    const payload = {
      function: 'FN_A', schema: 'OFSERM', line_start: 40, line_end: 53,
      lines: [{ line: 40, text: 'INSERT INTO T' }], clamped: false, truncated_to: null,
    };
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => payload });
    vi.stubGlobal('fetch', fetchMock);

    const res = await fetchSource('FN_A', 'OFSERM', 40, 50);
    expect(res).toEqual(payload);

    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe('/v1/source');
    expect(opts.method).toBe('POST');
    expect(JSON.parse(opts.body)).toEqual({
      function: 'FN_A', schema: 'OFSERM', line_start: 40, line_end: 50,
    });
  });

  it('throws on a non-ok response (so the panel can fall back to the excerpt)', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false, status: 404, text: async () => 'no indexed source',
    }));
    await expect(fetchSource('FN_X', 'OFSERM', 1, 2)).rejects.toThrow(/source fetch 404/);
  });
});
