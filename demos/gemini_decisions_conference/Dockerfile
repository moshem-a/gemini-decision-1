# Production Non-Root Multi-Stage uv Dockerfile for Gemini Decisions API Studio on Cloud Run
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /build
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN uv venv /opt/venv && \
    uv pip install --python /opt/venv/bin/python --no-cache \
      "cryptography>=44.0.1,<52.0.0" \
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

RUN groupadd -r appgroup && useradd -r -g appgroup -d /nonexistent -s /usr/sbin/nologin appuser

WORKDIR /app

# Keep /opt/venv and /app owned by root:root so unprivileged appuser cannot mutate code/packages at runtime
COPY --from=builder /opt/venv /opt/venv
COPY demos/__init__.py ./demos/__init__.py
COPY demos/gemini_decisions_conference ./demos/gemini_decisions_conference

USER appuser

EXPOSE 8080

# Use `exec` so uvicorn becomes PID 1 (receives Cloud Run SIGTERM cleanly) and omit `--forwarded-allow-ips='*'`
# so untrusted leftmost X-Forwarded-For headers cannot spoof request.client.host.
CMD ["sh", "-c", "exec uvicorn demos.gemini_decisions_conference.cloudrun_asgi:app --host 0.0.0.0 --port ${PORT:-8080}"]
