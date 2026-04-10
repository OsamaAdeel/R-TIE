import { useState, useEffect, useCallback } from 'react';
import { checkHealth } from '../api/client';

export function useHealth(intervalMs = 30000) {
  const [health, setHealth] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const data = await checkHealth();
      setHealth(data);
    } catch {
      setHealth({ oracle: 'error', redis: 'error', postgres: 'error', status: 'degraded' });
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, intervalMs);
    return () => clearInterval(id);
  }, [refresh, intervalMs]);

  return { health, refresh };
}
