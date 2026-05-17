# Build SPA inside Docker (Pi 3/4 with enough RAM+swap, or desktop).
# For Pi B+ / 512MB: use scripts/pi-deploy.sh (host build + frontend.nginx.Dockerfile).
FROM node:20-bookworm-slim AS builder

WORKDIR /app

# Limit Node/Bun memory during build (important on Raspberry Pi)
ENV NODE_OPTIONS=--max-old-space-size=384

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://bun.sh/install | bash \
    && ln -sf /root/.bun/bin/bun /usr/local/bin/bun

COPY frontend/package.json frontend/bun.lock ./
RUN bun install --frozen-lockfile

COPY frontend/ ./

ARG VITE_API_BASE_URL=/api/v1
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

RUN bun run build

FROM nginx:1.27-alpine

COPY docker/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/build/client /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1
