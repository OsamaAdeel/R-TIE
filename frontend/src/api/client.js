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
