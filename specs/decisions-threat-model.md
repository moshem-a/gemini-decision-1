# Threat Model: Gemini Decisions API Conference Studio

## 1. Trust Boundaries & Untrusted Inputs
- **Browser -> FastAPI BFF (`127.0.0.1:8090` / Cloud Run HTTPS)**: Scenario overrides, custom text prompts, question instructions/criteria, FinOps parameters, BYOK API keys, and optional base64 multimodal uploads (PNG, JPEG, WEBP, GIF, MP3, WAV, PDF).
- **Google Front End (GFE) -> Cloud Run Container**: TLS termination, `X-Forwarded-Proto`, and `X-Forwarded-For` proxy headers (only rightmost GFE-appended hop is trusted when `K_SERVICE` is set).
- **FastAPI BFF -> Gemini Decisions API (`https://generativelanguage.googleapis.com/v1beta/decisions`)**: Outbound TLS 1.2+ calls with explicit 25s HTTP timeout authenticated server-side only; credentials never reach client HTML, JS, Web Storage, or logs.

## 2. Mitigations & Security Controls
| Threat / CWE | Control Implemented |
| :--- | :--- |
| **Credential Exposure (CWE-798)** | Zero hardcoded keys. Multi-tier secret resolution (`GEMINI_API_KEY` env -> gitignored `.env.local` -> Google Secret Manager) + `HKDF-SHA256` key-ring rotation (`kid`) for `AES-256-GCM` `__Host-decisions_byok` cookies bound via AAD to rotated session IDs. |
| **DOM XSS & CSP Bypass (CWE-79 / CWE-693)** | Zero `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, or inline HTML `style=` attributes. Strict per-request CSP nonce (`script-src 'self' 'nonce-...' 'report-sample'; style-src 'self'`), HTML-escaped template tokens, HSTS, COOP, and CORP. |
| **CSRF, Session Fixation & Cookie Tampering (CWE-352 / CWE-384 / CWE-565)** | `HttpOnly`, `Secure`, `SameSite=Strict` `__Host-` cookies, session & CSRF rotation on BYOK connection, RFC 6265bis `Secure` cookie deletion + `Clear-Site-Data: "cookies"`, UTF-8 byte `hmac.compare_digest`, and `Origin` + `Sec-Fetch-Site` enforcement. |
| **Denial-of-Wallet & Memory DoS (CWE-770 / CWE-400)** | Pre-buffering `13.5 MB` ASGI streaming body cap (`413`), rightmost `X-Forwarded-For` client IP resolution (no `--forwarded-allow-ips='*'`), bounded LRU sliding-window general (`120/min`) and LLM (`24/min`) rate limiters, `asyncio.Semaphore(12)`, and bounded BYOK `genai.Client` pool. |
| **Malicious File Upload / Decompression Bomb (CWE-434 / CWE-409)** | Strict MIME allowlist, `data:` URI MIME consistency check, strict MPEG/PNG/JPEG/GIF/WEBP/PDF/WAV magic-byte verification, image pixel/dimension bomb limits (`<= 16384px`, `<= 100M` px), and 8 MB raw cap. |
| **Adversarial Prompt Injection (OWASP LLM01)** | Orthogonal `noul`, `choice`, and `score` classifiers, confidence thresholding ($\tau$), and CyberStrike escalation routing (`visual_prompt_injection`, `urgent_iban_bank_change`, `quarantine_and_escalate_cyberstrike`) to `gemini-v4p1s-rev25-skimaki-cyber-permissive-raw-thoughts`. |
| **Secret & PII Leakage in UI & Logs (CWE-532 / CWE-209)** | Automatic scrubbing of `AIza...` keys, `AQ....` Vertex keys, `ya29....` OAuth tokens, `x-goog-api-key`, `?key=`, `Bearer` tokens, emails, phones, and payment cards before Cloud Logging emission and error responses. |
