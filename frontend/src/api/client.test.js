// W151 Phase 5 — fetchSource request/response contract.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { fetchSource, streamQuery } from './client.js';

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

describe('streamQuery — abort handling', () => {
  // Build a fake Response whose reader models the "clean abort" race winner:
  // reader.cancel() resolves the in-flight read() with {done:true} instead of
  // rejecting, so the read loop breaks normally without throwing.
  const cleanCancelResponse = () => {
    let resolveRead;
    let onReadCalled;
    const readCalled = new Promise((r) => { onReadCalled = r; });
    const reader = {
      read: () => new Promise((resolve) => { resolveRead = resolve; onReadCalled(); }),
      cancel: () => { resolveRead?.({ done: true, value: undefined }); return Promise.resolve(); },
    };
    return { response: { ok: true, body: { getReader: () => reader } }, readCalled };
  };

  it('fires onAbort when reader.cancel() ends the stream cleanly (no terminal event)', async () => {
    const { response, readCalled } = cleanCancelResponse();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response));
    const controller = new AbortController();
    const onAbort = vi.fn();
    const onDone = vi.fn();
    const onError = vi.fn();

    const p = streamQuery('q', 's', 'e', null, null, 'ALL',
      { onAbort, onDone, onError }, { signal: controller.signal });

    // Abort only once the stream is parked in reader.read() and the abort
    // listener is registered — mirrors a user clicking Stop mid-response.
    await readCalled;
    controller.abort();
    await p;

    expect(onAbort).toHaveBeenCalledTimes(1);
    expect(onDone).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it('does not fire onAbort when the stream completed (done already delivered)', async () => {
    // Reader emits a `done` SSE frame, then ends the stream.
    const frames = [
      'event: done\ndata: {"badge":"VERIFIED"}\n\n',
    ];
    let i = 0;
    const enc = new TextEncoder();
    const reader = {
      read: async () => (i < frames.length
        ? { done: false, value: enc.encode(frames[i++]) }
        : { done: true, value: undefined }),
      cancel: () => Promise.resolve(),
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, body: { getReader: () => reader } }));

    const controller = new AbortController();
    const onAbort = vi.fn();
    const onDone = vi.fn();

    await streamQuery('q', 's', 'e', null, null, 'ALL',
      { onAbort, onDone }, { signal: controller.signal });
    // Aborting after natural completion must not clobber the delivered result.
    controller.abort();

    expect(onDone).toHaveBeenCalledTimes(1);
    expect(onAbort).not.toHaveBeenCalled();
  });
});
