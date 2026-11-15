"""Hardened FastAPI BFF for the Gemini Decisions API Conference Keynote Studio.

Security controls implemented:
- Zero client-side API key exposure (BFF architecture; server-side `google-genai` v3.2 client)
- HKDF-SHA256 key derivation with key-ring rotation (`kid`) for HMAC sessions & AES-256-GCM BYOK cookies
- Session rotation on BYOK elevation + RFC 6265bis-compliant `Secure` `__Host-` cookie deletion
- Timing-safe bytes `X-CSRF-Token` + `Origin`/`Sec-Fetch-Site` verification on all state-changing `POST` endpoints
- Streaming 13.5 MB ASGI request body cap (`413`), Cloud Run rightmost `X-Forwarded-For` IP extraction,
  bounded LRU general + LLM rate limiters, and `asyncio.Semaphore` upstream concurrency protection
- Per-request cryptographic CSP nonce, HSTS (over HTTPS), `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Permissions-Policy`, and `Cache-Control: no-store` on all responses
- Multimodal upload magic-byte + decompression-bomb verification, 8 MB cap, and SSRF-safe URI allowlist
"""

from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
import functools
import hashlib
import hmac
import html
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import time
from typing import Any, AsyncIterator

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from google.genai.errors import APIError
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send
import uvicorn

from . import CYBERSTRIKE_MODEL_ID, DECISION_MODEL_ID
from .case_studies import list_case_studies
from .engine import (
    DecisionsControlPlaneEngine,
    MissingApiKeyError,
    decode_validated_base64_upload,
    mask_pii,
)
from .finops import PRICING_TIERS, calculate_finops_roi
from .scenarios import get_scenario_by_id, list_scenarios

logger = logging.getLogger("decisions_demo.bff")

SESSION_TTL_SECONDS = 3600 * 4
MAX_HTTP_BODY_BYTES = 13_500_000  # 13.5 MB pre-buffering HTTP body stream cap (supports 8 MB raw base64 + JSON)
RATE_LIMIT_MAX_REQ = 120
LLM_RATE_LIMIT_MAX_REQ = 24
RATE_LIMIT_WINDOW_SEC = 60.0
MAX_TRACKED_IPS = 4_096
MAX_REVOKED_SESSIONS = 4_096
LLM_MAX_CONCURRENCY = 12
MIN_SESSION_SECRET_LEN = 32

QUESTION_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TOKEN_SAFE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_KID_RE = re.compile(r"^[0-9a-f]{8}$")
_HEX_SIG_RE = re.compile(r"^[0-9a-f]{64}$")
TRUSTED_MEDIA_PREFIXES = ("https://storage.googleapis.com/generativeai-downloads/",)
LLM_GATED_PATHS = frozenset(
    {
        "/api/session/api-key",
        "/api/decisions/evaluate",
        "/api/decisions/benchmark",
        "/api/cases/evaluate",
    }
)

# TODO(security): Integrate Cloud Identity-Aware Proxy (IAP) / OAuth 2.0 OIDC + MFA when exposing
# this studio beyond conference demo mode to multi-tenant enterprise users.
# TODO(security): Back session revocation with Memorystore/Firestore if strict cross-replica instant
# logout revocation is required in multi-instance deployments.
# TODO(security): Integrate Cloud DLP / sandboxed malware & CDR scanning if untrusted external user
# PDF/media uploads are persisted in multi-tenant production (currently verified in-memory and discarded).


class _PayloadTooLargeError(Exception):
    """Raised when an incoming HTTP body stream exceeds `MAX_HTTP_BODY_BYTES`."""


@dataclass(frozen=True)
class _DerivedKeyEntry:
    kid: str
    hmac_key: bytes
    aes_key: bytes


@dataclass
class DemoSession:
    session_id: str
    csrf_token: str
    expires_at: float


def _derive_keyring(raw_secrets: list[str]) -> tuple[_DerivedKeyEntry, dict[str, _DerivedKeyEntry]]:
    """Derive domain-separated HMAC-SHA256 and AES-256-GCM subkeys via HKDF-SHA256."""
    entries: list[_DerivedKeyEntry] = []
    salt = b"gemini-decisions-studio-hkdf-v1"
    for raw in raw_secrets:
        secret_bytes = raw.encode("utf-8")
        kid = hashlib.sha256(b"decisions-kid:" + secret_bytes).hexdigest()[:8]
        hmac_subkey = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=b"session-cookie-hmac-sha256",
        ).derive(secret_bytes)
        aes_subkey = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=b"byok-cookie-aes-256-gcm",
        ).derive(secret_bytes)
        entries.append(_DerivedKeyEntry(kid=kid, hmac_key=hmac_subkey, aes_key=aes_subkey))
    return entries[0], {e.kid: e for e in entries}


def _safe_compare_str(presented: str, expected: str) -> bool:
    """Timing-safe string comparison immune to Python's non-ASCII `hmac.compare_digest` `TypeError`."""
    if not presented or not expected:
        return False
    try:
        a = presented.encode("utf-8", errors="surrogateescape")
        b = expected.encode("utf-8", errors="strict")
        return hmac.compare_digest(a, b)
    except (TypeError, UnicodeError, ValueError):
        return False


@functools.lru_cache(maxsize=4)
def _read_cached_template(template_path: str) -> str:
    return Path(template_path).read_text(encoding="utf-8")


class _StreamingBodyLimitMiddleware:
    """Pure ASGI wrapper enforcing `MAX_HTTP_BODY_BYTES` on streaming `http.request` chunks."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_HTTP_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        for hdr_name, hdr_val in scope.get("headers", []):
            if hdr_name == b"content-length":
                try:
                    if int(hdr_val.decode("latin-1").strip()) > self.max_bytes:
                        await self._send_413(scope, send)
                        return
                except ValueError:
                    await self._send_400(scope, send)
                    return

        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                received_bytes += len(chunk)
                if received_bytes > self.max_bytes:
                    raise _PayloadTooLargeError()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _PayloadTooLargeError:
            await self._send_413(scope, send)

    @staticmethod
    def _security_headers_raw(scope: Scope) -> list[tuple[bytes, bytes]]:
        nonce = secrets.token_urlsafe(16)
        csp = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' 'report-sample'; "
            "style-src 'self'; "
            "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"content-security-policy", csp.encode("ascii")),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
            (b"permissions-policy", b"camera=(), geolocation=(), microphone=(), focus-without-user-activation=()"),
            (b"cross-origin-opener-policy", b"same-origin-allow-popups"),
            (b"cross-origin-resource-policy", b"same-origin"),
            (b"cache-control", b"no-store"),
        ]
        is_tls = scope.get("scheme") == "https"
        if not is_tls:
            for h_name, h_val in scope.get("headers", []):
                if h_name == b"x-forwarded-proto":
                    if h_val.decode("latin-1", errors="ignore").split(",")[-1].strip().lower() == "https":
                        is_tls = True
                    break
        if is_tls:
            headers.append((b"strict-transport-security", b"max-age=63072000; includeSubDomains"))
        return headers

    async def _send_413(self, scope: Scope, send: Send) -> None:
        body = json.dumps(
            {"detail": f"Request payload exceeds {self.max_bytes // 1_000_000} MB maximum safety limit."}
        ).encode("utf-8")
        headers = self._security_headers_raw(scope) + [(b"content-length", str(len(body)).encode("ascii"))]
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _send_400(self, scope: Scope, send: Send) -> None:
        body = json.dumps({"detail": "Malformed Content-Length header."}).encode("utf-8")
        headers = self._security_headers_raw(scope) + [(b"content-length", str(len(body)).encode("ascii"))]
        await send({"type": "http.response.start", "status": 400, "headers": headers})
        await send({"type": "http.response.body", "body": body})


class UploadedMediaPayload(BaseModel):
    mime_type: str = Field(..., min_length=3, max_length=64)
    data_b64: str = Field(..., min_length=8, max_length=12_000_000)
    resolution: str | None = Field(default=None, pattern=r"^(low|medium|high|ultra_high)$")


class QuestionSpecPayload(BaseModel):
    type: str = Field(..., pattern=r"^(noul|choice|score)$")
    instructions: str = Field(..., min_length=3, max_length=2000)
    criteria: dict[str, str] | list[str] | None = None

    @field_validator("instructions")
    @classmethod
    def validate_instructions_non_blank(cls, val: str) -> str:
        cleaned = val.strip()
        if len(cleaned) < 3:
            raise ValueError("Question instructions must contain at least 3 non-whitespace characters.")
        return cleaned

    @field_validator("criteria")
    @classmethod
    def validate_criteria(cls, val: dict[str, str] | list[str] | None) -> dict[str, str] | list[str] | None:
        if val is None:
            return None
        if isinstance(val, dict):
            if len(val) < 2 or len(val) > 255:
                raise ValueError("Choice criteria map must contain between 2 and 255 entries.")
            cleaned_map: dict[str, str] = {}
            for k, desc in val.items():
                clean_k = k.strip()
                clean_desc = str(desc).strip()
                if not clean_k or clean_k != k or len(k) > 128:
                    raise ValueError(f"Invalid option key '{k}'.")
                if not clean_desc or len(clean_desc) > 600:
                    raise ValueError(f"Option '{k}' description must be non-empty and <= 600 chars.")
                cleaned_map[clean_k] = clean_desc
            return cleaned_map
        if isinstance(val, list):
            if len(val) < 2 or len(val) > 255:
                raise ValueError("Score criteria rubric must contain between 2 and 255 ordered levels.")
            cleaned_list: list[str] = []
            for item in val:
                clean_item = str(item).strip()
                if not clean_item or len(clean_item) > 600:
                    raise ValueError("Score rubric level must be non-empty and <= 600 chars.")
                cleaned_list.append(clean_item)
            return cleaned_list
        raise ValueError("Unsupported criteria structure.")

    @model_validator(mode="after")
    def validate_type_criteria_pairing(self) -> QuestionSpecPayload:
        if self.type == "noul" and self.criteria is not None:
            raise ValueError("Question of type 'noul' must not include 'criteria'.")
        if self.type == "choice" and not isinstance(self.criteria, dict):
            raise ValueError("Question of type 'choice' requires a criteria object map.")
        if self.type == "score" and not isinstance(self.criteria, list):
            raise ValueError("Question of type 'score' requires an ordered criteria array.")
        return self


class EvaluateDecisionRequest(BaseModel):
    scenario_id: str | None = Field(default=None, max_length=64)
    custom_text: str | None = Field(default=None, max_length=32_000)
    image_resolution: str = Field(default="low", pattern=r"^(low|medium|high|ultra_high)$")
    include_scenario_media: bool = True
    uploaded_media: UploadedMediaPayload | None = None
    questions: dict[str, QuestionSpecPayload] = Field(..., min_length=1, max_length=60)
    confidence_threshold: float = Field(default=0.75, ge=0.05, le=0.99)

    @field_validator("questions")
    @classmethod
    def validate_question_keys(cls, qmap: dict[str, QuestionSpecPayload]) -> dict[str, QuestionSpecPayload]:
        for key in qmap:
            if not QUESTION_KEY_RE.match(key):
                raise ValueError(f"Invalid question key '{key}'. Must match [A-Za-z0-9_-]{{1,64}}.")
        return qmap


class ScalingBenchmarkRequest(BaseModel):
    counts: list[int] = Field(default_factory=lambda: [1, 5, 12, 25], min_length=2, max_length=5)

    @field_validator("counts")
    @classmethod
    def validate_counts(cls, counts: list[int]) -> list[int]:
        for c in counts:
            if c < 1 or c > 40:
                raise ValueError("Benchmark question counts must be between 1 and 40.")
        return counts


class CaseStudyEvalRequest(BaseModel):
    case_id: str | None = Field(default=None, max_length=64)
    resolution: str | None = Field(default=None, pattern=r"^(low|medium|high|ultra_high)$")
    confidence_threshold: float = Field(default=0.75, ge=0.05, le=0.99)


class ConnectApiKeyRequest(BaseModel):
    api_key: str = Field(..., min_length=12, max_length=256)
    verify: bool = True

    @field_validator("api_key")
    @classmethod
    def validate_key_format(cls, val: str) -> str:
        cleaned = val.strip()
        if len(cleaned) < 12 or any(ch.isspace() or ord(ch) < 33 or ord(ch) > 126 for ch in cleaned):
            raise ValueError("Invalid Gemini API key format. Paste a valid Google AI Studio key without spaces.")
        return cleaned


class FinOpsRequest(BaseModel):
    daily_requests: int = Field(default=250_000, ge=1_000, le=200_000_000)
    shared_input_tokens: int = Field(default=1_400, ge=50, le=200_000)
    questions_per_request: int = Field(default=6, ge=1, le=500)
    tokens_per_question_spec: int = Field(default=48, ge=10, le=500)
    generative_output_tokens_per_question: int = Field(default=95, ge=20, le=4_000)
    baseline_tier_id: str = Field(default="gemini_4_argon_promo", max_length=64)
    escalation_rate_pct: float = Field(default=8.0, ge=0.0, le=100.0)
    separate_calls_anti_pattern: bool = False

    @field_validator("baseline_tier_id")
    @classmethod
    def validate_tier_id(cls, val: str) -> str:
        cleaned = val.strip()
        if cleaned not in PRICING_TIERS:
            raise ValueError(f"Unsupported baseline_tier_id '{val}'.")
        return cleaned


def create_decisions_demo_app(
    workspace_root: Path | None = None,
    engine_override: DecisionsControlPlaneEngine | None = None,
    session_secret: str | None = None,
) -> FastAPI:
    """Construct the hardened FastAPI application for the Gemini Decisions API Conference Studio."""
    static_dir = Path(__file__).resolve().parent / "static"
    engine = engine_override or DecisionsControlPlaneEngine(workspace_root=workspace_root)
    is_cloud_run = bool(os.environ.get("K_SERVICE"))
    trust_proxy = is_cloud_run or os.environ.get("DECISIONS_TRUST_PROXY", "").strip() == "1"

    configured_secret = (session_secret or os.environ.get("DECISIONS_DEMO_SESSION_SECRET", "")).strip()
    if configured_secret:
        secret_list = [s.strip() for s in configured_secret.split(",") if s.strip()]
        if not secret_list or any(len(s) < MIN_SESSION_SECRET_LEN for s in secret_list):
            raise ValueError(
                f"DECISIONS_DEMO_SESSION_SECRET entries must each be at least {MIN_SESSION_SECRET_LEN} characters."
            )
    else:
        secret_list = [secrets.token_urlsafe(48)]
        if is_cloud_run:
            sys.stdout.write(
                json.dumps(
                    {
                        "severity": "WARNING",
                        "message": (
                            "DECISIONS_DEMO_SESSION_SECRET not set; generated ephemeral per-instance secret. "
                            "Mount DECISIONS_DEMO_SESSION_SECRET from Secret Manager for multi-replica session continuity."
                        ),
                        "component": "gemini-decisions-studio",
                    }
                )
                + "\n"
            )

    primary_key, key_ring = _derive_keyring(secret_list)
    gcp_project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()

    revoked_sessions: OrderedDict[str, float] = OrderedDict()
    last_prune_ts = 0.0
    rate_buckets: OrderedDict[str, deque[float]] = OrderedDict()
    llm_rate_buckets: OrderedDict[str, deque[float]] = OrderedDict()
    llm_semaphore = asyncio.Semaphore(LLM_MAX_CONCURRENCY)

    allowed_origins = [] if is_cloud_run else ["http://127.0.0.1:8090", "http://localhost:8090"]
    extra_origin = os.environ.get("DECISIONS_ALLOWED_ORIGIN", "").strip().rstrip("/")
    if extra_origin.startswith("https://"):
        allowed_origins.append(extra_origin)
    allowed_origins_set = frozenset(allowed_origins)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await engine.aclose()

    app = FastAPI(
        title="Gemini Decisions API — Conference Keynote Studio",
        version="1.2.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def _is_https(request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        # Inspect rightmost proxy-appended protocol to defeat leftmost X-Forwarded-Proto downgrade spoofing
        fwd_proto = request.headers.get("x-forwarded-proto", "").split(",")[-1].strip().lower()
        return fwd_proto == "https"

    def _resolve_client_ip(request: Request) -> str:
        """Extract non-spoofable client IP (rightmost GFE-appended XFF hop on Cloud Run; direct peer locally)."""
        if trust_proxy:
            xff = request.headers.get("x-forwarded-for", "")
            if xff:
                candidate = xff.split(",")[-1].strip()
                if candidate and len(candidate) <= 64:
                    return candidate
        return request.client.host if request.client else "127.0.0.1"

    def _extract_trace_resource(request: Request) -> str | None:
        trace_hdr = request.headers.get("x-cloud-trace-context", "")
        if trace_hdr and gcp_project:
            trace_id = trace_hdr.split("/")[0].strip()
            if re.fullmatch(r"[0-9a-fA-F]{16,32}", trace_id):
                return f"projects/{gcp_project}/traces/{trace_id}"
        return None

    def _emit_audit_log(
        request: Request,
        *,
        status_code: int,
        elapsed_ms: float,
        event: str | None = None,
        severity_override: str | None = None,
    ) -> None:
        severity = severity_override or ("WARNING" if status_code >= 400 else "INFO")
        log_entry: dict[str, Any] = {
            "severity": severity,
            "message": event or f"{request.method} {request.url.path} -> {status_code} ({elapsed_ms}ms)",
            "component": "gemini-decisions-studio",
            "httpRequest": {
                "requestMethod": request.method,
                "requestUrl": request.url.path,
                "status": status_code,
                "latency": f"{elapsed_ms / 1000.0:.4f}s",
            },
        }
        trace_res = _extract_trace_resource(request)
        if trace_res:
            log_entry["logging.googleapis.com/trace"] = trace_res
        sys.stdout.write(json.dumps(log_entry) + "\n")

    def _cookie_name(request: Request) -> str:
        return "__Host-decisions_sid" if _is_https(request) else "decisions_sid"

    def _byok_cookie_name(request: Request) -> str:
        return "__Host-decisions_byok" if _is_https(request) else "decisions_byok"

    def _set_hardened_cookie(resp: Response, request: Request, *, key: str, value: str) -> None:
        resp.set_cookie(
            key=key,
            value=value,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=_is_https(request),
            samesite="strict",
            path="/",
        )

    def _clear_hardened_cookie(resp: Response, request: Request, *, key: str) -> None:
        """Delete cookie with matching `Secure`/`HttpOnly`/`SameSite` attributes per RFC 6265bis `__Host-` rules."""
        resp.delete_cookie(
            key=key,
            path="/",
            secure=_is_https(request),
            httponly=True,
            samesite="strict",
        )

    def _mask_api_key(raw_key: str) -> str:
        tail = raw_key[-4:] if len(raw_key) >= 4 else "****"
        return f"••••{tail}"

    def _encrypt_byok_cookie(raw_key: str, sess: DemoSession) -> str:
        nonce = secrets.token_bytes(12)
        body_bytes = json.dumps({"k": raw_key, "exp": int(sess.expires_at)}).encode("utf-8")
        ct = AESGCM(primary_key.aes_key).encrypt(nonce, body_bytes, sess.session_id.encode("utf-8"))
        blob_b64 = base64.urlsafe_b64encode(nonce + ct).decode("ascii")
        return f"v1.{primary_key.kid}.{blob_b64}"

    def _decrypt_byok_cookie(request: Request, sess: DemoSession | None) -> str | None:
        if sess is None:
            return None
        token = request.cookies.get(_byok_cookie_name(request), "")
        if not token:
            return None
        try:
            parts = token.split(".")
            if len(parts) != 3 or parts[0] != "v1" or not _KID_RE.fullmatch(parts[1]):
                return None
            key_entry = key_ring.get(parts[1])
            if key_entry is None:
                return None
            blob = base64.urlsafe_b64decode(parts[2].encode("ascii"))
            if len(blob) < 29:
                return None
            nonce, ct = blob[:12], blob[12:]
            pt = AESGCM(key_entry.aes_key).decrypt(nonce, ct, sess.session_id.encode("utf-8"))
            parsed = json.loads(pt.decode("utf-8"))
            if float(parsed.get("exp", 0)) <= time.time():
                return None
            k = str(parsed.get("k", "")).strip()
            return k if len(k) >= 12 else None
        except Exception:
            return None

    def _record_revoked_session(session_id: str, expires_at: float) -> None:
        revoked_sessions[session_id] = expires_at
        revoked_sessions.move_to_end(session_id)
        while len(revoked_sessions) > MAX_REVOKED_SESSIONS:
            revoked_sessions.popitem(last=False)

    def _prune_revoked(now: float) -> None:
        nonlocal last_prune_ts
        if (now - last_prune_ts) < 30.0:
            return
        last_prune_ts = now
        expired = [sid for sid, exp in revoked_sessions.items() if exp <= now]
        for sid in expired:
            revoked_sessions.pop(sid, None)

    def _encode_session_cookie(sess: DemoSession) -> str:
        payload = f"v1.{primary_key.kid}.{sess.session_id}.{sess.csrf_token}.{int(sess.expires_at)}"
        sig = hmac.new(primary_key.hmac_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def _decode_session_cookie(raw_cookie: str, now: float) -> DemoSession | None:
        parts = raw_cookie.split(".")
        if len(parts) != 6:
            return None
        ver, kid, sid, csrf_tok, exp_str, presented_sig = parts
        if (
            ver != "v1"
            or not _KID_RE.fullmatch(kid)
            or not _TOKEN_SAFE_RE.fullmatch(sid)
            or not _TOKEN_SAFE_RE.fullmatch(csrf_tok)
            or not exp_str.isdigit()
            or len(exp_str) > 12
            or not _HEX_SIG_RE.fullmatch(presented_sig)
        ):
            return None
        key_entry = key_ring.get(kid)
        if key_entry is None:
            return None
        payload = f"v1.{kid}.{sid}.{csrf_tok}.{exp_str}"
        expected_sig = hmac.new(key_entry.hmac_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not _safe_compare_str(presented_sig, expected_sig):
            return None
        expires_at = float(int(exp_str))
        if expires_at <= now or sid in revoked_sessions:
            return None
        return DemoSession(session_id=sid, csrf_token=csrf_tok, expires_at=expires_at)

    def _mint_new_session(now: float | None = None) -> DemoSession:
        ts = now if now is not None else time.time()
        return DemoSession(
            session_id=secrets.token_urlsafe(24),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=ts + SESSION_TTL_SECONDS,
        )

    def _get_or_create_session(request: Request) -> tuple[DemoSession, bool]:
        now = time.time()
        _prune_revoked(now)
        cname = _cookie_name(request)
        raw_val = request.cookies.get(cname, "")
        existing = _decode_session_cookie(raw_val, now) if raw_val else None
        if existing is not None:
            return existing, False
        return _mint_new_session(now), True

    def _verify_csrf(request: Request) -> DemoSession:
        # Defense-in-depth Fetch Metadata & Origin check on state-changing POST endpoints
        sec_fetch = request.headers.get("sec-fetch-site", "").strip().lower()
        if sec_fetch and sec_fetch not in {"same-origin", "none"}:
            raise HTTPException(status_code=403, detail="Cross-site request blocked by Fetch Metadata policy.")

        origin = request.headers.get("origin", "").strip().rstrip("/")
        if origin:
            scheme = "https" if _is_https(request) else request.url.scheme
            host_hdr = request.headers.get("host", "") or request.url.netloc
            self_origin = f"{scheme}://{host_hdr}".rstrip("/")
            if origin not in allowed_origins_set and origin != self_origin:
                raise HTTPException(status_code=403, detail="Untrusted Origin header.")

        now = time.time()
        _prune_revoked(now)
        cname = _cookie_name(request)
        raw_val = request.cookies.get(cname, "")
        sess = _decode_session_cookie(raw_val, now) if raw_val else None
        if sess is None:
            raise HTTPException(status_code=403, detail="Session expired or missing. Refresh the studio.")
        presented = request.headers.get("X-CSRF-Token", "")
        if not _safe_compare_str(presented, sess.csrf_token):
            raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")
        return sess

    def _check_sliding_rate_limit(
        buckets: OrderedDict[str, deque[float]],
        client_ip: str,
        now_mono: float,
        max_req: int,
    ) -> bool:
        bucket = buckets.get(client_ip)
        if bucket is not None:
            while bucket and (now_mono - bucket[0]) > RATE_LIMIT_WINDOW_SEC:
                bucket.popleft()
            if not bucket:
                buckets.pop(client_ip, None)
                bucket = None

        if bucket is None:
            while len(buckets) >= MAX_TRACKED_IPS:
                buckets.popitem(last=False)
            bucket = deque()
            buckets[client_ip] = bucket
        else:
            buckets.move_to_end(client_ip)

        if len(bucket) >= max_req:
            return False
        bucket.append(now_mono)
        return True

    def _apply_security_headers(request: Request, response: Response, nonce: str) -> Response:
        csp = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' 'report-sample'; "
            "style-src 'self'; "
            "img-src 'self' https://gemini-decisions-studio-1085727639300.us-central1.run.app https://storage.googleapis.com data:; "
            "media-src 'self' https://storage.googleapis.com data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), geolocation=(), microphone=(), focus-without-user-activation=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        if _is_https(request):
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"

        if request.url.path.startswith("/static/cases/"):
            response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
            response.headers["Cache-Control"] = "public, max-age=86400"
        elif request.url.path in {"/favicon.ico", "/robots.txt"}:
            response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
            response.headers["Cache-Control"] = "public, max-age=86400"
        else:
            response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
            if (
                request.url.path.startswith("/api/")
                or request.url.path in {"/", "/docs", "/docs/"}
                or request.url.path.endswith((".js", ".css"))
            ):
                response.headers["Cache-Control"] = "no-store"

        if request.url.path.startswith("/api/"):
            existing_vary = [v.strip() for v in response.headers.get("Vary", "").split(",") if v.strip()]
            seen_lower = {v.lower() for v in existing_vary}
            for vary_hdr in ("Origin", "Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site"):
                if vary_hdr.lower() not in seen_lower:
                    existing_vary.append(vary_hdr)
                    seen_lower.add(vary_hdr.lower())
            response.headers["Vary"] = ", ".join(existing_vary)
        return response

    # Register CORSMiddleware BEFORE `@app.middleware("http")` so `security_envelope_middleware` wraps
    # `CORSMiddleware` — ensuring OPTIONS preflights also receive rate limiting, audit logging, HSTS/CSP,
    # and deduplicated `Vary` headers — while `_StreamingBodyLimitMiddleware` wraps outermost for 13.5 MB body caps.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST", "HEAD", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
        allow_credentials=True,
    )

    @app.middleware("http")
    async def security_envelope_middleware(request: Request, call_next: Any) -> Response:
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        started = time.perf_counter()

        if request.method not in {"GET", "POST", "HEAD", "OPTIONS"}:
            resp = JSONResponse(
                status_code=405,
                content={"detail": "Method not allowed."},
                headers={"Allow": "GET, POST, HEAD, OPTIONS"},
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            _emit_audit_log(request, status_code=405, elapsed_ms=elapsed_ms)
            return _apply_security_headers(request, resp, nonce)

        client_ip = _resolve_client_ip(request)
        now_mono = time.monotonic()
        if not _check_sliding_rate_limit(rate_buckets, client_ip, now_mono, RATE_LIMIT_MAX_REQ):
            resp = JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Retry shortly."},
                headers={"Retry-After": "60"},
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            _emit_audit_log(request, status_code=429, elapsed_ms=elapsed_ms, event="rate_limit_exceeded_general")
            return _apply_security_headers(request, resp, nonce)

        if request.method == "POST" and request.url.path in LLM_GATED_PATHS:
            if not _check_sliding_rate_limit(llm_rate_buckets, client_ip, now_mono, LLM_RATE_LIMIT_MAX_REQ):
                resp = JSONResponse(
                    status_code=429,
                    content={"detail": "Live model evaluation rate limit exceeded. Wait up to 60s or connect your own key."},
                    headers={"Retry-After": "60"},
                )
                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
                _emit_audit_log(request, status_code=429, elapsed_ms=elapsed_ms, event="rate_limit_exceeded_llm")
                return _apply_security_headers(request, resp, nonce)

        response: Response = await call_next(request)

        if request.url.path.startswith("/api/"):
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            _emit_audit_log(request, status_code=response.status_code, elapsed_ms=elapsed_ms)

        return _apply_security_headers(request, response, nonce)

    app.add_middleware(_StreamingBodyLimitMiddleware, max_bytes=MAX_HTTP_BODY_BYTES)

    @app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def serve_index(request: Request) -> Response:
        sess, is_new = _get_or_create_session(request)
        html_template = _read_cached_template(str(static_dir / "index.html"))
        safe_nonce = html.escape(request.state.csp_nonce, quote=True)
        safe_csrf = html.escape(sess.csrf_token, quote=True)
        rendered = html_template.replace("__CSP_NONCE__", safe_nonce).replace("__CSRF_TOKEN__", safe_csrf)
        resp = HTMLResponse(content=rendered)
        if is_new or _cookie_name(request) not in request.cookies:
            _set_hardened_cookie(resp, request, key=_cookie_name(request), value=_encode_session_cookie(sess))
        return resp

    @app.api_route("/docs", methods=["GET", "HEAD"], response_class=HTMLResponse)
    @app.api_route("/docs/", methods=["GET", "HEAD"], response_class=HTMLResponse)
    async def serve_medium_article_docs(request: Request) -> Response:
        docs_template = _read_cached_template(str(static_dir / "docs.html"))
        safe_nonce = html.escape(request.state.csp_nonce, quote=True)
        rendered = docs_template.replace("__CSP_NONCE__", safe_nonce)
        return HTMLResponse(content=rendered)

    @app.api_route("/favicon.ico", methods=["GET", "HEAD"])
    async def serve_favicon() -> Response:
        svg_icon = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            '<rect width="64" height="64" rx="16" fill="#1e3a8a"/>'
            '<path d="M32 10l6.2 15.8L54 32l-15.8 6.2L32 54l-6.2-15.8L10 32l15.8-6.2z" fill="#60a5fa"/>'
            "</svg>"
        )
        return Response(content=svg_icon, media_type="image/svg+xml")

    @app.api_route("/robots.txt", methods=["GET", "HEAD"])
    async def serve_robots() -> Response:
        robots_txt = "User-agent: *\nAllow: /\nAllow: /docs\nDisallow: /api/\n"
        return Response(content=robots_txt, media_type="text/plain; charset=utf-8")

    @app.get("/api/bootstrap")
    async def get_bootstrap(request: Request) -> Response:
        sess, is_new = _get_or_create_session(request)
        user_key = _decrypt_byok_cookie(request, sess)
        default_finops = calculate_finops_roi()
        payload = {
            "status": "ok",
            "version": "1.2.0",
            "live_api_ready": bool(engine.is_live_configured or user_key),
            "byok": {
                "connected": bool(user_key),
                "masked_key": _mask_api_key(user_key) if user_key else None,
                "server_default_available": engine.is_live_configured,
            },
            "security_posture": {
                "version": "1.2.0",
                "status": "HARDENED",
                "open_findings": 0,
                "controls_verified_count": 18,
                "cyberstrike_specialist_model": CYBERSTRIKE_MODEL_ID,
                "session_kdf": "HKDF-SHA256 + Key-Ring Rotation (kid)",
                "byok_cipher": "AES-256-GCM (__Host-decisions_byok + Session AAD + Rotation)",
                "body_stream_cap_mb": round(MAX_HTTP_BODY_BYTES / 1_000_000, 1),
                "rate_limits": (
                    f"{RATE_LIMIT_MAX_REQ}/min General · {LLM_RATE_LIMIT_MAX_REQ}/min LLM "
                    f"(Rightmost GFE XFF) · Semaphore({LLM_MAX_CONCURRENCY})"
                ),
                "cloud_run_mode": is_cloud_run,
                "controls": [
                    {
                        "id": "SEC-BYOK",
                        "title": "HKDF-SHA256 & AES-256-GCM Session-Bound BYOK",
                        "status": "ACTIVE",
                        "detail": (
                            "User Gemini keys are encrypted in __Host-decisions_byok (HttpOnly; Secure; SameSite=Strict) "
                            "bound via AEAD AAD to a rotated session ID—never stored in localStorage."
                        ),
                    },
                    {
                        "id": "SEC-RATE",
                        "title": "Denial-of-Wallet & X-Forwarded-For Spoof Guard",
                        "status": "ACTIVE",
                        "detail": (
                            "Extracts non-spoofable rightmost Cloud Run GFE IP, enforcing 24 req/min LLM sliding windows, "
                            "asyncio.Semaphore(12), and --max-instances=10 quota protection."
                        ),
                    },
                    {
                        "id": "SEC-STREAM",
                        "title": "13.5 MB Streaming ASGI Cap & Decompression-Bomb Guard",
                        "status": "ACTIVE",
                        "detail": (
                            "Aborts oversized streams with 413 before RAM buffering; verifies PNG/JPEG/WEBP/PDF/WAV/MP3 "
                            "magic bytes, MPEG sync frames, and <= 100 MP image pixel bounds."
                        ),
                    },
                    {
                        "id": "SEC-CSP",
                        "title": "Per-Request CSP Nonce, HSTS & CSRF Fetch-Metadata",
                        "status": "ACTIVE",
                        "detail": (
                            "Zero inline styles, timing-safe bytes X-CSRF-Token + Origin/Sec-Fetch-Site enforcement, "
                            "and full HSTS/CSP/nosniff/DENY headers across GET, POST, HEAD, and OPTIONS."
                        ),
                    },
                    {
                        "id": "SEC-CYBERSTRIKE",
                        "title": "Two-Pass Adversarial & Visual Prompt-Injection Gate",
                        "status": "ACTIVE",
                        "detail": (
                            f"Order-invariant gating flags BEC wire fraud & visual AI_OVERRIDE injections in gemini-decision-1 "
                            f"and escalates to {CYBERSTRIKE_MODEL_ID}."
                        ),
                    },
                    {
                        "id": "SEC-IAM",
                        "title": "Non-Root Immutable Container & Least-Privilege Cloud Run SA",
                        "status": "ACTIVE",
                        "detail": (
                            "Runs as unprivileged appuser over root-owned read-only /app & /opt/venv with dedicated "
                            "zero-editor Cloud Run service account and strict PII/API-key log scrubbing."
                        ),
                    },
                ],
            },
            "decision_model": DECISION_MODEL_ID,
            "cyberstrike_model": CYBERSTRIKE_MODEL_ID,
            "csrf_token": sess.csrf_token,
            "pricing": {
                "decision_input_per_1m_usd": 0.10,
                "decision_output_per_1m_usd": 0.00,
                "tiers": [
                    {
                        "id": t.tier_id,
                        "label": t.label,
                        "input_usd_per_1m": t.input_usd_per_1m,
                        "output_usd_per_1m": t.output_usd_per_1m,
                    }
                    for t in PRICING_TIERS.values()
                ],
            },
            "scenarios": list_scenarios(),
            "case_studies": list_case_studies(),
            "default_finops": default_finops,
        }
        resp = JSONResponse(content=payload)
        if is_new:
            _set_hardened_cookie(resp, request, key=_cookie_name(request), value=_encode_session_cookie(sess))
        return resp

    @app.post("/api/session/api-key")
    async def connect_user_api_key(request: Request, body: ConnectApiKeyRequest) -> Response:
        old_sess = _verify_csrf(request)
        latency_ms: float | None = None
        if body.verify:
            try:
                async with llm_semaphore:
                    check = await engine.verify_api_key(body.api_key)
                latency_ms = check.get("latency_ms")
            except APIError as exc:
                safe_msg = mask_pii(str(exc.message or "Invalid or unauthorized Gemini API key."))
                raise HTTPException(status_code=400, detail=f"Key verification failed: {safe_msg}") from exc
            except Exception as exc:
                status_c = getattr(exc, "status_code", None)
                if status_c in {400, 401, 403, 429}:
                    raise HTTPException(
                        status_code=400,
                        detail="Gemini API rejected the key. Verify your Google AI Studio key.",
                    ) from exc
                raise HTTPException(status_code=400, detail="Unable to verify Gemini API key.") from exc

        # Rotate session ID & CSRF token on credential elevation (prevents session fixation & aligns 4h TTL)
        _record_revoked_session(old_sess.session_id, old_sess.expires_at)
        rotated_sess = _mint_new_session()
        encrypted_cookie = _encrypt_byok_cookie(body.api_key, rotated_sess)

        resp = JSONResponse(
            content={
                "connected": True,
                "masked_key": _mask_api_key(body.api_key),
                "verified": body.verify,
                "latency_ms": latency_ms,
                "live_api_ready": True,
                "csrf_token": rotated_sess.csrf_token,
            }
        )
        _set_hardened_cookie(resp, request, key=_cookie_name(request), value=_encode_session_cookie(rotated_sess))
        _set_hardened_cookie(resp, request, key=_byok_cookie_name(request), value=encrypted_cookie)
        return resp

    @app.post("/api/session/api-key/clear")
    async def clear_user_api_key(request: Request) -> Response:
        _verify_csrf(request)
        resp = JSONResponse(
            content={
                "connected": False,
                "masked_key": None,
                "live_api_ready": engine.is_live_configured,
            }
        )
        _clear_hardened_cookie(resp, request, key=_byok_cookie_name(request))
        return resp

    @app.post("/api/session/logout")
    async def logout_session(request: Request) -> Response:
        sess = _verify_csrf(request)
        _record_revoked_session(sess.session_id, sess.expires_at)
        resp = JSONResponse(content={"status": "logged_out"})
        _clear_hardened_cookie(resp, request, key=_cookie_name(request))
        _clear_hardened_cookie(resp, request, key=_byok_cookie_name(request))
        if _is_https(request):
            resp.headers["Clear-Site-Data"] = '"cookies"'
        return resp

    @app.post("/api/decisions/evaluate")
    async def evaluate_decisions(request: Request, body: EvaluateDecisionRequest) -> Response:
        sess = _verify_csrf(request)
        user_key = _decrypt_byok_cookie(request, sess)
        scenario = get_scenario_by_id(body.scenario_id or "") if body.scenario_id else None

        input_blocks: list[dict[str, Any]] = []

        # 1. Optional custom uploaded multimodal file (verified via magic bytes & dimension bounds)
        if body.uploaded_media is not None:
            try:
                media_block = decode_validated_base64_upload(
                    data_b64=body.uploaded_media.data_b64,
                    mime_type=body.uploaded_media.mime_type,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if media_block["type"] == "image":
                media_block["resolution"] = body.uploaded_media.resolution or body.image_resolution
            input_blocks.append(media_block)
        elif body.include_scenario_media and scenario is not None:
            for blk in scenario.get("input_blocks", []):
                if blk.get("type") in {"image", "audio", "video", "document"}:
                    uri = str(blk.get("uri", ""))
                    if not uri.startswith(TRUSTED_MEDIA_PREFIXES):
                        raise HTTPException(status_code=400, detail="Untrusted media URI blocked.")
                    copied_blk = dict(blk)
                    if copied_blk.get("type") == "image":
                        copied_blk["resolution"] = body.image_resolution
                    input_blocks.append(copied_blk)

        # 2. Text context (explicit `custom_text` override vs `None` scenario default)
        if body.custom_text is not None:
            if body.custom_text.strip():
                prefix_nl = "\n" if input_blocks else ""
                input_blocks.append({"type": "text", "text": prefix_nl + body.custom_text})
        elif scenario is not None:
            for blk in scenario.get("input_blocks", []):
                if blk.get("type") == "text":
                    input_blocks.append(dict(blk))

        if not input_blocks:
            raise HTTPException(status_code=400, detail="At least one text or media input block is required.")

        serialized_questions: dict[str, dict[str, Any]] = {}
        for k, qspec in body.questions.items():
            qdict: dict[str, Any] = {
                "type": qspec.type,
                "instructions": qspec.instructions,
            }
            if qspec.criteria is not None:
                qdict["criteria"] = qspec.criteria
            serialized_questions[k] = qdict

        try:
            async with llm_semaphore:
                evaluation = await engine.evaluate(
                    input_blocks=input_blocks,
                    questions=serialized_questions,
                    confidence_threshold=body.confidence_threshold,
                    generative_baseline=scenario.get("generative_baseline") if scenario else None,
                    api_key_override=user_key,
                    trace_resource=_extract_trace_resource(request),
                )
        except APIError as exc:
            safe_msg = mask_pii(str(exc.message or "Gemini Decisions API returned an error."))
            raise HTTPException(status_code=400, detail=f"Gemini Decisions API error: {safe_msg}") from exc
        except MissingApiKeyError as exc:
            raise HTTPException(status_code=503, detail=mask_pii(str(exc))) from exc
        except Exception as exc:
            if getattr(exc, "status_code", None) in {400, 422, 429}:
                safe_msg = mask_pii(str(exc))
                raise HTTPException(status_code=int(getattr(exc, "status_code", 400)), detail=safe_msg) from exc
            logger.error("Unexpected Decisions evaluation error: %s", type(exc).__name__)
            raise HTTPException(status_code=500, detail="Internal evaluation error.") from exc

        return JSONResponse(content=evaluation)

    @app.post("/api/decisions/benchmark")
    async def run_benchmark(request: Request, body: ScalingBenchmarkRequest) -> Response:
        sess = _verify_csrf(request)
        user_key = _decrypt_byok_cookie(request, sess)
        try:
            async with llm_semaphore:
                result = await engine.run_scaling_benchmark(counts=body.counts, api_key_override=user_key)
        except APIError as exc:
            safe_msg = mask_pii(str(exc.message or "Benchmark request failed."))
            raise HTTPException(status_code=400, detail=f"Gemini Decisions API error: {safe_msg}") from exc
        except MissingApiKeyError as exc:
            raise HTTPException(status_code=503, detail=mask_pii(str(exc))) from exc
        except Exception as exc:
            logger.error("Unexpected benchmark error: %s", type(exc).__name__)
            raise HTTPException(status_code=500, detail="Internal benchmark error.") from exc
        return JSONResponse(content=result)

    @app.post("/api/finops/calculate")
    async def compute_finops(request: Request, body: FinOpsRequest) -> Response:
        _verify_csrf(request)
        report = calculate_finops_roi(
            daily_requests=body.daily_requests,
            shared_input_tokens=body.shared_input_tokens,
            questions_per_request=body.questions_per_request,
            tokens_per_question_spec=body.tokens_per_question_spec,
            generative_output_tokens_per_question=body.generative_output_tokens_per_question,
            baseline_tier_id=body.baseline_tier_id,
            escalation_rate_pct=body.escalation_rate_pct,
            separate_calls_anti_pattern=body.separate_calls_anti_pattern,
        )
        return JSONResponse(content=report)

    @app.post("/api/cases/evaluate")
    async def evaluate_case_endpoint(request: Request, body: CaseStudyEvalRequest) -> Response:
        sess = _verify_csrf(request)
        user_key = _decrypt_byok_cookie(request, sess)
        trace_res = _extract_trace_resource(request)
        try:
            async with llm_semaphore:
                if body.case_id:
                    res = await engine.evaluate_case_study(
                        case_id=body.case_id,
                        resolution_override=body.resolution,
                        confidence_threshold=body.confidence_threshold,
                        api_key_override=user_key,
                        trace_resource=trace_res,
                    )
                else:
                    res = await engine.evaluate_all_case_studies(
                        resolution_override=body.resolution,
                        confidence_threshold=body.confidence_threshold,
                        api_key_override=user_key,
                        trace_resource=trace_res,
                    )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=mask_pii(str(exc))) from exc
        except APIError as exc:
            safe_msg = mask_pii(str(exc.message or "Gemini Decisions API returned an error."))
            raise HTTPException(status_code=400, detail=f"Gemini Decisions API error: {safe_msg}") from exc
        except MissingApiKeyError as exc:
            raise HTTPException(status_code=503, detail=mask_pii(str(exc))) from exc
        except Exception as exc:
            if getattr(exc, "status_code", None) in {400, 422, 429}:
                raise HTTPException(
                    status_code=int(getattr(exc, "status_code", 400)),
                    detail=mask_pii(str(exc)),
                ) from exc
            logger.error("Unexpected case evaluation error: %s", type(exc).__name__)
            raise HTTPException(status_code=500, detail="Internal case evaluation error.") from exc
        return JSONResponse(content=res)

    return app


def run_demo_server() -> None:
    """Start the local Conference Keynote Studio strictly bound to `127.0.0.1`."""
    is_cloud_run = bool(os.environ.get("K_SERVICE"))
    host = "0.0.0.0" if is_cloud_run else os.environ.get("DECISIONS_DEMO_HOST", "127.0.0.1")
    if not is_cloud_run and host == "0.0.0.0":
        host = "127.0.0.1"
    port = int(os.environ.get("PORT") or os.environ.get("DECISIONS_DEMO_PORT", "8090"))
    app = create_decisions_demo_app()
    uvicorn.run(app, host=host, port=port, log_level="info")
