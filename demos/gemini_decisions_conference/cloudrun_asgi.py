"""Cloud Run ASGI entrypoint (`demos.gemini_decisions_conference.cloudrun_asgi:app`)."""

from __future__ import annotations

from .app import create_decisions_demo_app

app = create_decisions_demo_app()
