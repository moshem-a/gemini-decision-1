# Google Agentic SDLC Core Rules

When designing, coding, evaluating, or deploying AI applications and multi-agent systems on Google Gemini and Google Cloud, enforce these non-negotiable engineering standards:

## 1. Gemini Model Governance
- Always target the latest **Gemini v4+** model generation (or leave the SDK model parameter unset to inherit the platform's current default).
- For CyberStrike, threat modeling, red-teaming, and security auditing workloads, explicitly use **`gemini-v4p1s-rev25-skimaki-cyber-permissive-raw-thoughts`**.
- Never introduce outdated pre-v4 Gemini model identifiers in code, configs, tests, or documentation.

## 2. Unified SDK & Cognitive Architecture
- Use the unified **`google-genai`** SDK (`from google import genai`) for Gemini API and Vertex AI inference—never legacy `vertexai.generative_models` packages.
- Initialize `genai.Client()` globally per process for connection reuse, and use async `client.aio.models.generate_content` / `generate_content_stream` inside async servers (FastAPI / Cloud Run).
- Keep cognitive orchestration (Google ADK / Google Antigravity SDK prompts, tools, state machines, MCP integrations, Human-in-the-Loop gates) cleanly decoupled from cloud deployment infrastructure (Docker, Terraform, Cloud Build).

## 3. Zero-Key Cloud Security & Least Privilege
- Authenticate exclusively via **Application Default Credentials (ADC)**, **Workload Identity Federation**, and **Google Secret Manager**. Never hardcode GCP service account JSON keys, raw API keys, or static JWT secret fallbacks.
- Bind every agent tool and Cloud Run service account to minimum IAM permissions (`roles/aiplatform.user`, `roles/secretmanager.secretAccessor`, `roles/cloudtrace.agent`).
- Treat all user inputs, retrieved RAG chunks, and external tool responses as untrusted. Enforce strict Pydantic schema validation, parameterized database queries, canonical path boundary checks, and OS-level command sandboxing where supported.
- Bind local test servers strictly to `127.0.0.1` (never `0.0.0.0`).

## 4. Quantitative Trajectory Evaluation (No "Vibe-Testing")
- Every agentic workflow or tool change must pair deterministic unit tests with **Golden Trajectory Evaluations** (`Reason -> Act -> Observe`) verifying tool selection accuracy, argument invariants, safety refusals, and token/latency budgets.

## 5. Production Observability & Auditability
- Emit structured JSON logs compatible with Google Cloud Logging (`severity`, `message`, `logging.googleapis.com/trace`) and OpenTelemetry spans exported to Google Cloud Trace, without ever logging PII, credentials, or bearer tokens.
