"""Hardened FastAPI BFF for the Gemini Decisions API Conference Keynote Studio.

Security controls implemented:
- Zero client-side API key exposure (BFF architecture; server-side `google-genai` v3.2 client)
- HttpOnly, SameSite=Strict session cookies with bounded 4h TTL & logout invalidation
- Timing-safe synchronizer `X-CSRF-Token` validation on all state-changing `POST` endpoints
- Per-request cryptographic CSP nonce, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Permissions-Policy: camera=(), microphone=(), geolocation=()`, `Cache-Control: no-store`
- Strict HTTP method allowlist (`GET`, `POST`, `HEAD` -> `405` otherwise), rate limiting, and strict CORS
- Multimodal upload magic-byte verification, 8 MB size cap, and SSRF-safe URI allowlist
"""

from __future__ import annotations

import base64
from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import time
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from google.genai.errors import APIError
from pydantic import BaseModel, Field, field_validator
import uvicorn

from . import CYBERSTRIKE_MODEL_ID, DECISION_MODEL_ID
from .case_studies import list_case_studies
from .engine import (
    DecisionsControlPlaneEngine,
    decode_validated_base64_upload,
    mask_pii,
)
from .finops import PRICING_TIERS, calculate_finops_roi
from .scenarios import get_scenario_by_id, list_scenarios

logger = logging.getLogger("decisions_demo.bff")

SESSION_TTL_SECONDS = 3600 * 4
RATE_LIMIT_MAX_REQ = 120
RATE_LIMIT_WINDOW_SEC = 60.0
QUESTION_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TRUSTED_MEDIA_PREFIXES = ("https://storage.googleapis.com/generativeai-downloads/",)

# TODO(security): Integrate Cloud Identity-Aware Proxy (IAP) / OAuth 2.0 OIDC + MFA when exposing
# this studio beyond localhost conference presentation mode to multi-tenant enterprise users.
# TODO(security): Integrate Cloud DLP / sandboxed malware & CDR scanning if untrusted external user
# PDF/media uploads are persisted in multi-tenant production (currently verified in-memory via magic bytes and discarded).


@dataclass
class DemoSession:
    session_id: str
    csrf_token: str
    expires_at: float


class UploadedMediaPayload(BaseModel):
    mime_type: str = Field(..., min_length=3, max_length=64)
    data_b64: str = Field(..., min_length=8, max_length=12_000_000)
    resolution: str | None = Field(default=None, pattern=r"^(low|medium|high|ultra_high)$")


class QuestionSpecPayload(BaseModel):
    type: str = Field(..., pattern=r"^(noul|choice|score)$")
    instructions: str = Field(..., min_length=3, max_length=2000)
    criteria: dict[str, str] | list[str] | None = None

    @field_validator("criteria")
    @classmethod
    def validate_criteria(cls, val: dict[str, str] | list[str] | None) -> dict[str, str] | list[str] | None:
        if val is None:
            return None
        if isinstance(val, dict):
            if len(val) < 2 or len(val) > 255:
                raise ValueError("Choice/noul criteria map must contain between 2 and 255 entries.")
            for k, desc in val.items():
                clean_k = k.strip()
                if not clean_k or clean_k != k or len(k) > 128:
                    raise ValueError(f"Invalid option key '{k}'.")
                if len(str(desc)) > 600:
                    raise ValueError("Criteria description exceeds 600 chars.")
            return val
        if isinstance(val, list):
            if len(val) < 2 or len(val) > 255:
                raise ValueError("Score criteria rubric must contain between 2 and 255 ordered levels.")
            for item in val:
                if not str(item).strip() or len(str(item)) > 600:
                    raise ValueError("Score rubric level must be non-empty and <= 600 chars.")
            return val
        raise ValueError("Unsupported criteria structure.")


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
        for key, spec in qmap.items():
            if not QUESTION_KEY_RE.match(key):
                raise ValueError(f"Invalid question key '{key}'. Must match [A-Za-z0-9_-]{{1,64}}.")
            if spec.type == "choice" and not isinstance(spec.criteria, dict):
                raise ValueError(f"Question '{key}' of type 'choice' requires a criteria object map.")
            if spec.type == "score" and not isinstance(spec.criteria, list):
                raise ValueError(f"Question '{key}' of type 'score' requires an ordered criteria array.")
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


def create_decisions_demo_app(
    workspace_root: Path | None = None,
    engine_override: DecisionsControlPlaneEngine | None = None,
    session_secret: str | None = None,
) -> FastAPI:
    """Construct the hardened FastAPI application for the Gemini Decisions API Conference Studio."""
    static_dir = Path(__file__).resolve().parent / "static"
    engine = engine_override or DecisionsControlPlaneEngine(workspace_root=workspace_root)

    configured_secret = (session_secret or os.environ.get("DECISIONS_DEMO_SESSION_SECRET", "")).strip()
    signing_key = (configured_secret or secrets.token_urlsafe(48)).encode("utf-8")
    gcp_project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    revoked_sessions: dict[str, float] = {}
    rate_buckets: dict[str, deque[float]] = defaultdict(deque)

    allowed_origins = ["http://127.0.0.1:8090", "http://localhost:8090"]
    extra_origin = os.environ.get("DECISIONS_ALLOWED_ORIGIN", "").strip()
    if extra_origin.startswith("https://"):
        allowed_origins.append(extra_origin)

    app = FastAPI(
        title="Gemini Decisions API — Conference Keynote Studio",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST", "HEAD"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
        allow_credentials=True,
    )

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def _is_https(request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        fwd_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        return fwd_proto == "https"

    def _cookie_name(request: Request) -> str:
        return "__Host-decisions_sid" if _is_https(request) else "decisions_sid"

    def _byok_cookie_name(request: Request) -> str:
        return "__Host-decisions_byok" if _is_https(request) else "decisions_byok"

    byok_aes_key = hashlib.sha256(signing_key + b":byok-aes-256-gcm").digest()

    def _mask_api_key(raw_key: str) -> str:
        tail = raw_key[-4:] if len(raw_key) >= 4 else "****"
        return f"••••{tail}"

    def _encrypt_byok_cookie(raw_key: str, sess: DemoSession) -> str:
        nonce = secrets.token_bytes(12)
        body_bytes = json.dumps({"k": raw_key, "exp": int(sess.expires_at)}).encode("utf-8")
        ct = AESGCM(byok_aes_key).encrypt(nonce, body_bytes, sess.session_id.encode("utf-8"))
        return base64.urlsafe_b64encode(nonce + ct).decode("ascii")

    def _decrypt_byok_cookie(request: Request, sess: DemoSession | None) -> str | None:
        if sess is None:
            return None
        token = request.cookies.get(_byok_cookie_name(request), "")
        if not token:
            return None
        try:
            blob = base64.urlsafe_b64decode(token.encode("ascii"))
            if len(blob) < 29:
                return None
            nonce, ct = blob[:12], blob[12:]
            pt = AESGCM(byok_aes_key).decrypt(nonce, ct, sess.session_id.encode("utf-8"))
            parsed = json.loads(pt.decode("utf-8"))
            if float(parsed.get("exp", 0)) <= time.time():
                return None
            k = str(parsed.get("k", "")).strip()
            return k if len(k) >= 12 else None
        except Exception:
            return None

    def _prune_revoked(now: float) -> None:
        expired = [sid for sid, exp in revoked_sessions.items() if exp <= now]
        for sid in expired:
            revoked_sessions.pop(sid, None)

    def _encode_session_cookie(sess: DemoSession) -> str:
        payload = f"{sess.session_id}.{sess.csrf_token}.{int(sess.expires_at)}"
        sig = hmac.new(signing_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload}.{sig}"

    def _decode_session_cookie(raw_cookie: str, now: float) -> DemoSession | None:
        parts = raw_cookie.split(".")
        if len(parts) != 4:
            return None
        sid, csrf_tok, exp_str, presented_sig = parts
        if not sid or not csrf_tok or not exp_str.isdigit():
            return None
        payload = f"{sid}.{csrf_tok}.{exp_str}"
        expected_sig = hmac.new(signing_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(presented_sig, expected_sig):
            return None
        expires_at = float(int(exp_str))
        if expires_at <= now or sid in revoked_sessions:
            return None
        return DemoSession(session_id=sid, csrf_token=csrf_tok, expires_at=expires_at)

    def _get_or_create_session(request: Request) -> tuple[DemoSession, bool]:
        now = time.time()
        _prune_revoked(now)
        cname = _cookie_name(request)
        raw_val = request.cookies.get(cname, "")
        existing = _decode_session_cookie(raw_val, now) if raw_val else None
        if existing is not None:
            return existing, False
        new_sess = DemoSession(
            session_id=secrets.token_urlsafe(24),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now + SESSION_TTL_SECONDS,
        )
        return new_sess, True

    def _verify_csrf(request: Request) -> DemoSession:
        now = time.time()
        _prune_revoked(now)
        cname = _cookie_name(request)
        raw_val = request.cookies.get(cname, "")
        sess = _decode_session_cookie(raw_val, now) if raw_val else None
        if sess is None:
            raise HTTPException(status_code=403, detail="Session expired or missing. Refresh the studio.")
        presented = request.headers.get("X-CSRF-Token", "")
        if not presented or not hmac.compare_digest(presented, sess.csrf_token):
            raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")
        return sess

    @app.middleware("http")
    async def security_envelope_middleware(request: Request, call_next: Any) -> Response:
        if request.method not in {"GET", "POST", "HEAD"}:
            return JSONResponse(
                status_code=405,
                content={"detail": "Method not allowed."},
                headers={"Allow": "GET, POST, HEAD"},
            )

        # Sliding-window rate limit per client IP
        client_ip = request.client.host if request.client else "127.0.0.1"
        now = time.monotonic()
        bucket = rate_buckets[client_ip]
        while bucket and (now - bucket[0]) > RATE_LIMIT_WINDOW_SEC:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_MAX_REQ:
            return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded. Retry shortly."})
        bucket.append(now)

        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        started = time.perf_counter()

        response: Response = await call_next(request)

        if request.url.path.startswith("/api/"):
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            log_entry: dict[str, Any] = {
                "severity": "WARNING" if response.status_code >= 400 else "INFO",
                "message": f"{request.method} {request.url.path} -> {response.status_code} ({elapsed_ms}ms)",
                "component": "gemini-decisions-studio",
                "httpRequest": {
                    "requestMethod": request.method,
                    "requestUrl": request.url.path,
                    "status": response.status_code,
                    "latency": f"{elapsed_ms / 1000.0:.4f}s",
                },
            }
            trace_hdr = request.headers.get("x-cloud-trace-context", "")
            if trace_hdr and gcp_project:
                trace_id = trace_hdr.split("/")[0].strip()
                if re.fullmatch(r"[0-9a-fA-F]{16,32}", trace_id):
                    log_entry["logging.googleapis.com/trace"] = f"projects/{gcp_project}/traces/{trace_id}"
            sys.stdout.write(json.dumps(log_entry) + "\n")
            sys.stdout.flush()

        csp = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self'; "
            "img-src 'self' https://storage.googleapis.com data:; "
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
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/") or request.url.path == "/" or request.url.path.endswith((".js", ".css")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def serve_index(request: Request) -> Response:
        sess, is_new = _get_or_create_session(request)
        html_template = (static_dir / "index.html").read_text(encoding="utf-8")
        rendered = html_template.replace("__CSP_NONCE__", request.state.csp_nonce).replace(
            "__CSRF_TOKEN__", sess.csrf_token
        )
        resp = HTMLResponse(content=rendered)
        if is_new or _cookie_name(request) not in request.cookies:
            resp.set_cookie(
                key=_cookie_name(request),
                value=_encode_session_cookie(sess),
                max_age=SESSION_TTL_SECONDS,
                httponly=True,
                secure=_is_https(request),
                samesite="strict",
                path="/",
            )
        return resp

    @app.get("/api/bootstrap")
    async def get_bootstrap(request: Request) -> Response:
        sess, is_new = _get_or_create_session(request)
        user_key = _decrypt_byok_cookie(request, sess)
        default_finops = calculate_finops_roi()
        payload = {
            "status": "ok",
            "live_api_ready": bool(engine.is_live_configured or user_key),
            "byok": {
                "connected": bool(user_key),
                "masked_key": _mask_api_key(user_key) if user_key else None,
                "server_default_available": engine.is_live_configured,
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
            resp.set_cookie(
                key=_cookie_name(request),
                value=_encode_session_cookie(sess),
                max_age=SESSION_TTL_SECONDS,
                httponly=True,
                secure=_is_https(request),
                samesite="strict",
                path="/",
            )
        return resp

    @app.post("/api/session/api-key")
    async def connect_user_api_key(request: Request, body: ConnectApiKeyRequest) -> Response:
        sess = _verify_csrf(request)
        latency_ms: float | None = None
        if body.verify:
            try:
                check = await engine.verify_api_key(body.api_key)
                latency_ms = check.get("latency_ms")
            except APIError as exc:
                safe_msg = mask_pii(str(exc.message or "Invalid or unauthorized Gemini API key."))
                raise HTTPException(status_code=400, detail=f"Key verification failed: {safe_msg}") from exc
            except Exception as exc:
                status_c = getattr(exc, "status_code", None)
                if status_c in {400, 401, 403, 429}:
                    raise HTTPException(status_code=400, detail="Gemini API rejected the key. Verify your Google AI Studio key.") from exc
                raise HTTPException(status_code=400, detail="Unable to verify Gemini API key.") from exc

        encrypted_cookie = _encrypt_byok_cookie(body.api_key, sess)
        resp = JSONResponse(
            content={
                "connected": True,
                "masked_key": _mask_api_key(body.api_key),
                "verified": body.verify,
                "latency_ms": latency_ms,
                "live_api_ready": True,
            }
        )
        resp.set_cookie(
            key=_byok_cookie_name(request),
            value=encrypted_cookie,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=_is_https(request),
            samesite="strict",
            path="/",
        )
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
        resp.delete_cookie(key=_byok_cookie_name(request), path="/")
        return resp

    @app.post("/api/session/logout")
    async def logout_session(request: Request) -> Response:
        sess = _verify_csrf(request)
        revoked_sessions[sess.session_id] = sess.expires_at
        resp = JSONResponse(content={"status": "logged_out"})
        resp.delete_cookie(key=_cookie_name(request), path="/")
        resp.delete_cookie(key=_byok_cookie_name(request), path="/")
        return resp

    @app.post("/api/decisions/evaluate")
    async def evaluate_decisions(request: Request, body: EvaluateDecisionRequest) -> Response:
        sess = _verify_csrf(request)
        user_key = _decrypt_byok_cookie(request, sess)
        scenario = get_scenario_by_id(body.scenario_id or "") if body.scenario_id else None

        input_blocks: list[dict[str, Any]] = []

        # 1. Optional custom uploaded multimodal file (verified via magic bytes)
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
            # Include scenario's trusted HTTPS media blocks (strictly validated against SSRF allowlist)
            for blk in scenario.get("input_blocks", []):
                if blk.get("type") in {"image", "audio", "video", "document"}:
                    uri = str(blk.get("uri", ""))
                    if not uri.startswith(TRUSTED_MEDIA_PREFIXES):
                        raise HTTPException(status_code=400, detail="Untrusted media URI blocked.")
                    copied_blk = dict(blk)
                    if copied_blk.get("type") == "image":
                        copied_blk["resolution"] = body.image_resolution
                    input_blocks.append(copied_blk)

        # 2. Text context (custom override or scenario text blocks)
        if body.custom_text is not None and body.custom_text.strip():
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
            evaluation = await engine.evaluate(
                input_blocks=input_blocks,
                questions=serialized_questions,
                confidence_threshold=body.confidence_threshold,
                generative_baseline=scenario.get("generative_baseline") if scenario else None,
                api_key_override=user_key,
            )
        except APIError as exc:
            safe_msg = mask_pii(str(exc.message or "Gemini Decisions API returned an error."))
            raise HTTPException(status_code=400, detail=f"Gemini Decisions API error: {safe_msg}") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
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
            result = await engine.run_scaling_benchmark(counts=body.counts, api_key_override=user_key)
        except APIError as exc:
            safe_msg = mask_pii(str(exc.message or "Benchmark request failed."))
            raise HTTPException(status_code=400, detail=f"Gemini Decisions API error: {safe_msg}") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
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
        try:
            if body.case_id:
                res = await engine.evaluate_case_study(
                    case_id=body.case_id,
                    resolution_override=body.resolution,
                    confidence_threshold=body.confidence_threshold,
                    api_key_override=user_key,
                )
            else:
                res = await engine.evaluate_all_case_studies(
                    resolution_override=body.resolution,
                    confidence_threshold=body.confidence_threshold,
                    api_key_override=user_key,
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except APIError as exc:
            safe_msg = mask_pii(str(exc.message or "Gemini Decisions API returned an error."))
            raise HTTPException(status_code=400, detail=f"Gemini Decisions API error: {safe_msg}") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
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
