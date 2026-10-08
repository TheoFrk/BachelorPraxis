# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca; fi; \
    npm ci --strict-ssl=true
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SE_DB=/app/var/securities-eye.sqlite SE_STATIC_DIR=/app/frontend/dist
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY backend/ backend/
RUN --mount=type=secret,id=system_ca \
    if [ -f /run/secrets/system_ca ]; then export PIP_CERT=/run/secrets/system_ca; fi; \
    pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps .
COPY --from=ui /ui/dist frontend/dist/
RUN groupadd --gid 10001 securities && useradd --uid 10001 --gid 10001 --no-create-home securities \
    && mkdir -p /app/var && chown 10001:10001 /app/var
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3)"
CMD ["uvicorn", "securities_eye.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
