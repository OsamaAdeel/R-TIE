# RTIE frontend image. Vite build -> static dist/ served by nginx.
# The frontend uses relative URLs (API_BASE='' in src/api/client.js), so
# nginx must reverse-proxy /v1/* and /health to the backend container.
# No VITE_API_BASE_URL build arg is wired because the source ignores it.

# ---------- Stage 1: builder ----------
FROM node:20-alpine AS builder

WORKDIR /build
# Bump npm to 11.x to match the host npm that generated package-lock.json.
# `node:20-alpine` ships npm 10.8.x by default, which reads the lockfile's
# optional-/peer-dep resolution stricter than npm 11 and rejects
# host-generated locks (e.g. "@emnapi/runtime missing from lock file").
RUN npm install -g npm@11

COPY frontend/package.json frontend/package-lock.json ./
# Strict reproducible install: `npm ci` requires package-lock.json to match
# package.json exactly. Fails the build on drift, which is what we want.
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# ---------- Stage 2: runtime ----------
FROM nginx:1.27-alpine AS runtime

COPY deploy/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /build/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD wget -qO- http://localhost/ >/dev/null 2>&1 || exit 1
