# Threat Model: Gemini Decisions API Conference Studio

## 1. Trust Boundaries & Untrusted Inputs
- **Browser -> FastAPI BFF (`127.0.0.1:8090`)**: Scenario overrides, custom text prompts, question instructions/criteria, FinOps parameters, and optional base64 multimodal uploads (PNG, JPEG, WEBP, MP3, WAV, PDF).
- **FastAPI BFF -> Gemini Decisions API (`https://generativelanguage.googleapis.com/v1beta/decisions`)**: Outbound TLS 1.2+ calls authenticated server-side only; credentials never reach client HTML, JS, or logs.

## 2. Mitigations & Security Controls
| Threat / CWE | Control Implemented |
| :--- | :--- |
| **Credential Exposure (CWE-798)** | Zero hardcoded keys. Multi-tier secret resolution (`GEMINI_API_KEY` env -> gitignored `.env.local` -> Google Secret Manager) isolated inside BFF memory. |
| **DOM Cross-Site Scripting (CWE-79)** | Zero `innerHTML`, `outerHTML`, `insertAdjacentHTML`, or `document.write`. All UI and SVG visualizations use `createElement`, `createElementNS`, `textContent`, and `replaceChildren`. Strict per-request CSP nonce. |
| **CSRF & Session Fixation (CWE-352)** | `HttpOnly`, `SameSite=Strict` session cookies with bounded TTL, synchronizer `X-CSRF-Token` verification on all `POST` endpoints, and explicit logout invalidation. |
| **Malicious File Upload / Polyglot (CWE-434)** | Strict MIME allow-list + server-side magic-byte header inspection (PNG, JPEG, WEBP, GIF, PDF, MP3/ID3, WAV/RIFF), 8 MB payload cap, in-memory base64 forwarding (never written to web root). |
| **Adversarial Prompt Injection (OWASP LLM01)** | Explicit positive `noul` and `choice` criteria, untrusted input framing, confidence thresholding ($\tau$), and CyberStrike escalation routing to `gemini-v4p1s-rev25-skimaki-cyber-permissive-raw-thoughts`. |
| **PII Leakage in UI & Logs (CWE-532)** | Automatic email, phone, card, and bearer-token redaction before structured Cloud Logging emission and UI display. |
