# Production Non-Root Multi-Stage uv Dockerfile for Gemini Decisions API Studio on Cloud Run
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /build
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python --no-cache \
      "cryptography>=42.0.0" \
      "fastapi>=0.115.0,<1.0.0" \
      "uvicorn[standard]>=0.30.0,<1.0.0" \
      "pydantic>=2.8.0,<3.0.0" \
      "httpx>=0.27.0,<1.0.0" \
      "tenacity>=8.2.0,<10.0.0" \
      "google-genai>=3.2.0,<4.0.0"

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PORT=8080

RUN groupadd -r appgroup && useradd -r -g appgroup -d /app -s /sbin/nologin appuser

WORKDIR /app

COPY --from=builder --chown=appuser:appgroup /opt/venv /opt/venv
COPY --chown=appuser:appgroup demos/__init__.py ./demos/__init__.py
COPY --chown=appuser:appgroup demos/gemini_decisions_conference ./demos/gemini_decisions_conference

USER appuser

EXPOSE 8080

CMD ["sh", "-c", "uvicorn demos.gemini_decisions_conference.cloudrun_asgi:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
