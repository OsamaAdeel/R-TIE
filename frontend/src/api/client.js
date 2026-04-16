const API_BASE = '';

export async function sendQuery(query, sessionId, engineerId, provider, model) {
  const body = {
    query,
    session_id: sessionId,
    engineer_id: engineerId,
  };
  if (provider) body.provider = provider;
  if (model) body.model = model;

  const res = await fetch(`${API_BASE}/v1/query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API error ${res.status}: ${text}`);
  }

  const correlationId = res.headers.get('X-Correlation-ID');
  const data = await res.json();
  return { ...data, correlation_id: data.correlation_id || correlationId };
}

/**
 * Stream a query response via Server-Sent Events.
 *
 * @param {string} query
 * @param {string} sessionId
 * @param {string} engineerId
 * @param {string|null} provider
 * @param {string|null} model
 * @param {function} onMeta     - called once with metadata {schema, functions_analyzed, ...}
 * @param {function} onToken    - called for each markdown text chunk
 * @param {function} onDone     - called once with final payload {confidence, validated, ...}
 * @param {function} onError    - called on error
 */
export async function streamQuery(query, sessionId, engineerId, provider, model, { onMeta, onToken, onDone, onError }) {
  const body = {
    query,
    session_id: sessionId,
    engineer_id: engineerId,
  };
  if (provider) body.provider = provider;
  if (model) body.model = model;

  try {
    const res = await fetch(`${API_BASE}/v1/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!res.ok) {
      const text = await res.text();
      onError?.(`API error ${res.status}: ${text}`);
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // Parse SSE events from buffer
      const lines = buffer.split('\n');
      buffer = lines.pop() || ''; // keep incomplete line in buffer

      let currentEvent = null;
      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7).trim();
        } else if (line.startsWith('data: ') && currentEvent) {
          const data = line.slice(6);
          try {
            const parsed = JSON.parse(data);
            if (currentEvent === 'meta') {
              onMeta?.(parsed);
            } else if (currentEvent === 'token') {
              onToken?.(parsed);
            } else if (currentEvent === 'done') {
              onDone?.(parsed);
            } else if (currentEvent === 'error') {
              onError?.(parsed.error || 'Unknown streaming error');
            }
          } catch {
            // token data might be a plain string
            if (currentEvent === 'token') {
              onToken?.(data);
            }
          }
          currentEvent = null;
        } else if (line === '') {
          currentEvent = null;
        }
      }
    }
  } catch (err) {
    onError?.(err.message);
  }
}

export async function fetchModels() {
  const res = await fetch(`${API_BASE}/v1/models`);
  if (!res.ok) throw new Error('Failed to fetch models');
  return res.json();
}

export async function checkHealth() {
  const res = await fetch(`${API_BASE}/health`);
  if (!res.ok) throw new Error('Health check failed');
  return res.json();
}
