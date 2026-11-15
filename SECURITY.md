# Security Policy & Architecture

## Supported Versions

| Version | Supported |
| :--- | :--- |
| `main` (`1.1.x`) | Yes |
| `< 1.1.0` | No (upgrade to `main`) |

## Reporting a Vulnerability

Please **do not** open public GitHub issues for suspected security vulnerabilities.

Instead, report vulnerabilities privately via GitHub's **Report a vulnerability** button under the repository's **Security -> Advisories** tab (`https://github.com/moshem-a/gemini-decision-1/security/advisories/new`). Include:
1. Affected endpoint, file, or configuration surface.
2. Minimal proof-of-concept steps or HTTP request/response trace.
3. Impact assessment (confidentiality, integrity, availability, or FinOps quota impact).

## Zero-Trust BFF Security Controls

The Gemini Decisions Studio enforces defense-in-depth across its FastAPI Backend-for-Frontend (BFF), browser client, and Google Cloud Run runtime:

1. **Zero Client-Side Secret Exposure & Encrypted BYOK Cookies**
   - Server-side `GEMINI_API_KEY` never leaves the backend process.
   - Optional user-supplied Bring-Your-Own-Key (BYOK) keys are never stored in `localStorage` or `sessionStorage`. They are encrypted with **AES-256-GCM** using **HKDF-SHA256** domain-separated subkeys (`kid`-prefixed key ring supporting zero-downtime secret rotation), bound via AEAD Additional Authenticated Data (`AAD`) to the rotated `session_id`, and stored in `__Host-decisions_byok` (`HttpOnly; Secure; SameSite=Strict; Path=/`).
2. **CSRF, Origin & Fetch Metadata Enforcement**
   - State-changing `POST` routes enforce timing-safe bytes comparison on `X-CSRF-Token`, `Sec-Fetch-Site` (`same-origin` / `none`), and strict `Origin` validation (`DECISIONS_ALLOWED_ORIGIN` + same-origin check; `localhost` origins are excluded in Cloud Run production).
3. **Browser Hardening & Strict Content Security Policy**
   - Every response emits a per-request cryptographic CSP nonce (`script-src 'self' 'nonce-...'`, `style-src 'self'`, `object-src 'none'`, `frame-ancestors 'none'`, `base-uri 'self'`, `form-action 'self'`), `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Permissions-Policy`, `Cross-Origin-Opener-Policy`, and `Cross-Origin-Resource-Policy`.
4. **DoS, Upload & SSRF Protections**
   - Pure ASGI `_StreamingBodyLimitMiddleware` aborts bodies exceeding `13.5 MB` (`413`) before memory buffering.
   - Multimodal uploads enforce an `8 MB` decoded cap, binary magic-byte header verification (`PNG`, `JPEG`, `WEBP`, `PDF`, `WAV`, `MP3`), and image pixel decompression-bomb caps (`<= 25 MP`, `<= 8192 px/side`).
   - Sliding-window IP rate limiters (`120 req/min` general, `24 req/min` LLM endpoints using Cloud Run's non-spoofable rightmost `X-Forwarded-For` hop) and `asyncio.Semaphore(12)` protect upstream model quotas.
5. **Adversarial & Visual Prompt-Injection Escalation**
   - Untrusted text/media inputs are evaluated by `gemini-decision-1` (`total_output_tokens == 0`, zero tool execution). Flagged adversarial inputs or low-confidence security gates escalate to `gemini-v4p1s-rev25-skimaki-cyber-permissive-raw-thoughts`.
6. **Cloud Run Container & IAM Hardening**
   - Multi-stage `uv` Docker image runs as non-root `appuser` (`uid >= 999`, `/usr/sbin/nologin`) over root-owned immutable `/app` and `/opt/venv` directories, deployed with a dedicated least-privilege service account and `--max-instances` quota bounds.

See [specs/decisions-threat-model.md](specs/decisions-threat-model.md) for the full STRIDE & OWASP LLM Top 10 threat model.
