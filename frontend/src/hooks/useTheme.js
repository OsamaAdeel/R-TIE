import { useCallback, useEffect, useSyncExternalStore } from 'react';

const STORAGE_KEY = 'rtie.theme';
const EVENT = 'rtie:theme-change';

// Default is `light` (linen). User can flip to `dark` (signature) via the
// sidebar footer toggle. Selection persists in localStorage. The hook uses
// useSyncExternalStore so every consumer (sidebar toggle, code blocks…)
// re-renders together when the theme changes.
function readStored() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === 'dark' ? 'dark' : 'light';
  } catch {
    return 'light';
  }
}

function writeStored(value) {
  try { localStorage.setItem(STORAGE_KEY, value); }
  catch { /* ignore */ }
  // Notify every useTheme() in the tree, even within the same tab where
  // the native `storage` event does not fire.
  window.dispatchEvent(new Event(EVENT));
}

function subscribe(cb) {
  window.addEventListener(EVENT, cb);
  window.addEventListener('storage', cb);
  return () => {
    window.removeEventListener(EVENT, cb);
    window.removeEventListener('storage', cb);
  };
}

export function useTheme() {
  const theme = useSyncExternalStore(subscribe, readStored, () => 'light');

  // Sync the chosen theme onto <html data-theme="…"> so the cascade picks
  // up the dark token overrides defined in index.css.
  useEffect(() => {
    const root = document.documentElement;
    if (theme === 'dark') root.setAttribute('data-theme', 'dark');
    else root.removeAttribute('data-theme');
  }, [theme]);

  const setTheme = useCallback((next) => writeStored(next === 'dark' ? 'dark' : 'light'), []);
  const toggleTheme = useCallback(() => writeStored(readStored() === 'dark' ? 'light' : 'dark'), []);

  return { theme, setTheme, toggleTheme };
}
