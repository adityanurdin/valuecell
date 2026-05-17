# Runtime-only frontend for Raspberry Pi (and other low-RAM hosts).
# Build static assets on the Pi host first — see scripts/pi-deploy.sh
FROM nginx:1.27-alpine

COPY docker/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY frontend/build/client /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1
