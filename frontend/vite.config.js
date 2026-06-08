import { execSync } from 'node:child_process'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// Build identity for the About panel — no real semver yet (package.json is
// 0.0.0), so surface the git short-hash + build date. Resolved once at config
// load; falls back to 'dev' outside a git checkout.
function gitShortHash() {
  try { return execSync('git rev-parse --short HEAD').toString().trim(); }
  catch { return 'dev'; }
}

export default defineConfig({
  define: {
    __GIT_HASH__: JSON.stringify(gitShortHash()),
    __BUILD_DATE__: JSON.stringify(new Date().toISOString().slice(0, 10)),
  },
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/v1': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
})
