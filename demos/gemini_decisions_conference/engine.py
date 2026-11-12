"""Unified `google-genai` v3.2 Async Execution Engine for the Gemini Decisions API (`gemini-decision-1`).

Implements:
- Global connection-pooled `genai.Client(http_options={'api_version': 'v1beta'})`
- Async `client.aio.decisions.create(...)` with exponential backoff (`tenacity`)
- Server-side multimodal upload magic-byte verification (PNG, JPEG, WEBP, GIF, PDF, MP3, WAV)
- Live 1 -> 5 -> 12 -> 25 question scaling benchmark
- SDK code-snippet generation (Python, JavaScript, cURL REST) with zero leaked credentials
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any

from google import genai
from google.genai.errors import APIError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import CYBERSTRIKE_MODEL_ID, DECISION_MODEL_ID
from .case_studies import build_case_input_blocks, get_case_study_by_id, list_case_studies
from .finops import DECISION_INPUT_USD_PER_1M

logger = logging.getLogger("decisions_demo.engine")

MAX_UPLOAD_BYTES = 8 * 1024 * 1024  # 8 MB strict DoS cap

ALLOWED_UPLOAD_MIMES: dict[str, str] = {
    "image/png": "image",
    "image/jpeg": "image",
    "image/webp": "image",
    "image/gif": "image",
    "application/pdf": "document",
    "audio/mp3": "audio",
    "audio/mpeg": "audio",
    "audio/wav": "audio",
}

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def mask_pii(text: str) -> str:
    """Mask emails, payment card numbers, and bearer tokens for safe logging/UI previews."""
    masked = _EMAIL_RE.sub("***@***.redacted", text)
    masked = _CARD_RE.sub("***-***-REDACTED", masked)
    masked = _BEARER_RE.sub(r"\1[REDACTED]", masked)
    return masked


def verify_magic_bytes(raw_bytes: bytes, declared_mime: str) -> str:
    """Validate uploaded bytes against file header magic signatures; return normalized block type."""
    mime = declared_mime.lower().strip()
    if mime not in ALLOWED_UPLOAD_MIMES:
        raise ValueError(f"Unsupported MIME type '{declared_mime}'. Allowed: {', '.join(sorted(ALLOWED_UPLOAD_MIMES))}")
    if len(raw_bytes) == 0:
        raise ValueError("Uploaded media payload is empty.")
    if len(raw_bytes) > MAX_UPLOAD_BYTES:
        raise ValueError(f"Uploaded media exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB safety limit.")

    if mime == "image/png" and not raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("File content does not match PNG magic header.")
    if mime == "image/jpeg" and not raw_bytes.startswith(b"\xff\xd8\xff"):
        raise ValueError("File content does not match JPEG magic header.")
    if mime == "image/gif" and not (raw_bytes.startswith(b"GIF87a") or raw_bytes.startswith(b"GIF89a")):
        raise ValueError("File content does not match GIF magic header.")
    if mime == "image/webp" and not (raw_bytes.startswith(b"RIFF") and raw_bytes[8:12] == b"WEBP"):
        raise ValueError("File content does not match WEBP RIFF header.")
    if mime == "application/pdf" and not raw_bytes.startswith(b"%PDF-"):
        raise ValueError("File content does not match PDF header.")
    if mime == "audio/wav" and not (raw_bytes.startswith(b"RIFF") and raw_bytes[8:12] == b"WAVE"):
        raise ValueError("File content does not match WAV RIFF header.")
    if mime in {"audio/mp3", "audio/mpeg"} and not (
        raw_bytes.startswith(b"ID3") or (len(raw_bytes) >= 2 and raw_bytes[0] == 0xFF and (raw_bytes[1] & 0xE0) == 0xE0)
    ):
        raise ValueError("File content does not match MP3/ID3 frame header.")

    return ALLOWED_UPLOAD_MIMES[mime]


def resolve_gemini_api_key(workspace_root: Path | None = None) -> str | None:
    """Resolve `GEMINI_API_KEY` from environment or gitignored `.env.local` without hardcoding."""
    env_val = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_val and env_val != "your_google_ai_studio_api_key_here":
        return env_val

    candidates: list[Path] = []
    if workspace_root is not None:
        candidates.append(workspace_root / ".env.local")
        candidates.append(workspace_root / ".env")
    repo_root = Path(__file__).resolve().parents[2]
    candidates.append(repo_root / ".env.local")
    candidates.append(repo_root / ".env")

    for env_path in candidates:
        if env_path.is_file():
            try:
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#") or "=" not in stripped:
                        continue
                    k, v = stripped.split("=", 1)
                    if k.strip() == "GEMINI_API_KEY":
                        cleaned = v.strip().strip('"').strip("'")
                        if cleaned and cleaned != "your_google_ai_studio_api_key_here":
                            return cleaned
            except OSError:
                continue
    return None


class DecisionsControlPlaneEngine:
    """Async execution & benchmarking wrapper around `google-genai` `client.aio.decisions`."""

    def __init__(self, workspace_root: Path | None = None, api_key_override: str | None = None) -> None:
        self._workspace_root = workspace_root
        self._api_key = api_key_override or resolve_gemini_api_key(workspace_root)
        self._client: genai.Client | None = None
        self._bound_loop_id: int | None = None
        if self._api_key:
            self._client = genai.Client(
                api_key=self._api_key,
                http_options={"api_version": "v1beta"},
            )

    @property
    def is_live_configured(self) -> bool:
        return self._client is not None

    def _get_loop_safe_client(self, api_key_override: str | None = None) -> genai.Client:
        import asyncio

        if api_key_override:
            return genai.Client(
                api_key=api_key_override,
                http_options={"api_version": "v1beta"},
            )
        if not self._api_key:
            raise RuntimeError("No Gemini API key configured. Click 'Connect Gemini Key' to connect your Google AI Studio key.")
        loop_id = id(asyncio.get_running_loop())
        if self._client is None or self._bound_loop_id != loop_id:
            self._client = genai.Client(
                api_key=self._api_key,
                http_options={"api_version": "v1beta"},
            )
            self._bound_loop_id = loop_id
        return self._client

    async def verify_api_key(self, candidate_key: str) -> dict[str, Any]:
        """Verify a user-supplied Gemini API key via a minimal 1-question `gemini-decision-1` ping."""
        t0 = time.perf_counter()
        raw = await self._call_decisions_api(
            input_payload=[{"type": "text", "text": "Health check ping."}],
            questions={"ready": {"type": "noul", "instructions": "Is this a health check ping?"}},
            api_key_override=candidate_key,
        )
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        return {
            "verified": "answers" in raw,
            "latency_ms": latency_ms,
        }

    @retry(
        retry=retry_if_exception_type(APIError),
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=3),
        reraise=True,
    )
    async def _call_decisions_api(
        self,
        *,
        input_payload: str | list[dict[str, Any]],
        questions: dict[str, dict[str, Any]],
        api_key_override: str | None = None,
    ) -> dict[str, Any]:
        client = self._get_loop_safe_client(api_key_override=api_key_override)
        result = await client.aio.decisions.create(
            model=DECISION_MODEL_ID,
            input=input_payload,
            questions=questions,
        )
        return result.model_dump(mode="json")

    async def evaluate(
        self,
        *,
        input_blocks: list[dict[str, Any]],
        questions: dict[str, dict[str, Any]],
        confidence_threshold: float = 0.75,
        generative_baseline: dict[str, Any] | None = None,
        api_key_override: str | None = None,
    ) -> dict[str, Any]:
        """Run a live single-pass `gemini-decision-1` evaluation and enrich with FinOps & routing telemetry."""
        t0 = time.perf_counter()
        raw_response = await self._call_decisions_api(
            input_payload=input_blocks,
            questions=questions,
            api_key_override=api_key_override,
        )
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        usage = raw_response.get("usage") or {}
        total_input_tokens = int(usage.get("total_input_tokens", 0))
        total_output_tokens = int(usage.get("total_output_tokens", 0))
        live_cost_usd = (total_input_tokens / 1_000_000.0) * DECISION_INPUT_USD_PER_1M

        # Compare this exact call against generative Gemini 4 baseline
        baseline = generative_baseline or {
            "model": "gemini-4-argon",
            "estimated_output_and_thinking_tokens": max(240, len(questions) * 95),
            "estimated_latency_ms": 1100 + len(questions) * 120,
        }
        gen_out_tokens = int(baseline.get("estimated_output_and_thinking_tokens", 480))
        # Official Paid Standard rate ($2/1M in, $10/1M out promo; $4/$20 GA)
        gen_promo_cost_usd = (total_input_tokens / 1_000_000.0) * 2.00 + (gen_out_tokens / 1_000_000.0) * 10.00
        gen_ga_cost_usd = (total_input_tokens / 1_000_000.0) * 4.00 + (gen_out_tokens / 1_000_000.0) * 20.00
        est_gen_latency_ms = int(baseline.get("estimated_latency_ms", 1800))

        savings_vs_promo_pct = (
            round((gen_promo_cost_usd - live_cost_usd) / gen_promo_cost_usd * 100.0, 2)
            if gen_promo_cost_usd > 0
            else 0.0
        )
        cheaper_factor_promo = round(gen_promo_cost_usd / live_cost_usd, 1) if live_cost_usd > 0 else 1.0
        cheaper_factor_ga = round(gen_ga_cost_usd / live_cost_usd, 1) if live_cost_usd > 0 else 1.0

        answers = raw_response.get("answers") or {}
        routing_summary = self._compute_confidence_gating(
            answers=answers,
            threshold=confidence_threshold,
        )

        # Structured Cloud Logging compatible record (no PII or secrets)
        logger.info(
            json.dumps(
                {
                    "severity": "INFO",
                    "message": "gemini_decision_1_evaluated",
                    "model": DECISION_MODEL_ID,
                    "question_count": len(questions),
                    "latency_ms": elapsed_ms,
                    "total_input_tokens": total_input_tokens,
                    "total_output_tokens": total_output_tokens,
                    "live_cost_usd": round(live_cost_usd, 8),
                    "auto_approved_count": routing_summary["auto_execute_count"],
                    "escalated_count": routing_summary["escalate_count"],
                }
            )
        )

        sanitized_blocks_for_snippets = self._redact_large_inline_media(input_blocks)

        return {
            "model": DECISION_MODEL_ID,
            "key_source": "user_byok" if api_key_override else "studio_default",
            "latency_ms": elapsed_ms,
            "confidence_threshold": confidence_threshold,
            "answers": answers,
            "usage": {
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
                "total_tokens": int(usage.get("total_tokens", total_input_tokens)),
                "input_tokens_by_modality": usage.get("input_tokens_by_modality") or [],
            },
            "call_economics": {
                "decision_cost_usd": round(live_cost_usd, 8),
                "decisions_per_dollar": int(1.0 / live_cost_usd) if live_cost_usd > 0 else 0,
                "generative_baseline_model": baseline.get("model", "gemini-4-argon"),
                "generative_promo_cost_usd": round(gen_promo_cost_usd, 8),
                "generative_ga_cost_usd": round(gen_ga_cost_usd, 8),
                "generative_estimated_output_tokens": gen_out_tokens,
                "generative_estimated_latency_ms": est_gen_latency_ms,
                "savings_pct_vs_promo": savings_vs_promo_pct,
                "cheaper_factor_promo": cheaper_factor_promo,
                "cheaper_factor_ga": cheaper_factor_ga,
                "speedup_factor": round(est_gen_latency_ms / max(1.0, elapsed_ms), 1),
            },
            "routing_summary": routing_summary,
            "snippets": build_code_snippets(sanitized_blocks_for_snippets, questions),
            "raw_response": raw_response,
        }

    def _compute_confidence_gating(
        self,
        *,
        answers: dict[str, dict[str, Any]],
        threshold: float,
    ) -> dict[str, Any]:
        auto_count = 0
        escalate_count = 0
        cyberstrike_triggered = False
        per_question_gate: dict[str, dict[str, Any]] = {}

        for key, ans in answers.items():
            conf = float(ans.get("confidence", 0.0))
            qtype = ans.get("type", "")
            if qtype == "noul" and key in {
                "prompt_injection_attempt",
                "credential_exfiltration",
                "ssrf_metadata_probe",
            } and float(ans.get("noul", 0.0)) >= 0.5:
                cyberstrike_triggered = True
            if qtype == "choice" and CYBERSTRIKE_MODEL_ID in str(ans.get("choice", "")):
                cyberstrike_triggered = True
            if qtype == "choice" and ans.get("choice") == "block_and_escalate_cyberstrike":
                cyberstrike_triggered = True

            if conf >= threshold:
                auto_count += 1
                gate_status = "auto_execute"
                target = "deterministic_fast_path"
            else:
                escalate_count += 1
                gate_status = "escalate"
                target = CYBERSTRIKE_MODEL_ID if cyberstrike_triggered else "gemini-4-carbon_or_human_review"

            per_question_gate[key] = {
                "status": gate_status,
                "confidence": round(conf, 4),
                "threshold": threshold,
                "escalation_target": target if gate_status == "escalate" else None,
            }

        return {
            "auto_execute_count": auto_count,
            "escalate_count": escalate_count,
            "cyberstrike_escalation_recommended": cyberstrike_triggered,
            "cyberstrike_specialist_model": CYBERSTRIKE_MODEL_ID,
            "per_question": per_question_gate,
        }

    @staticmethod
    def _redact_large_inline_media(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cleaned: list[dict[str, Any]] = []
        for blk in blocks:
            copied = dict(blk)
            if "data" in copied and isinstance(copied["data"], str) and len(copied["data"]) > 64:
                copied["data"] = "<BASE64_MEDIA_BYTES>"
            if "text" in copied and isinstance(copied["text"], str):
                copied["text"] = mask_pii(copied["text"])
            cleaned.append(copied)
        return cleaned

    async def run_scaling_benchmark(
        self,
        counts: list[int] | None = None,
        api_key_override: str | None = None,
    ) -> dict[str, Any]:
        """Execute a live 1 -> 5 -> 12 -> 25 questions scaling benchmark on stage."""
        question_counts = counts or [1, 5, 12, 25]
        shared_context = [
            {
                "type": "text",
                "text": (
                    '{"enterprise_ticket": "TCK-90421", "account_tier": "Platinum", "arr_usd": 480000, '
                    '"region": "eu-west1", "service": "cloud-run-checkout-api", "error_rate_pct": 4.8}\n'
                    '{"customer_note": "Checkout latency spiked to 9s after our v4.2 canary rollout in Belgium. '
                    'No card data was leaked, but EU shoppers are abandoning carts. Need immediate rollback guidance."}'
                ),
            }
        ]

        atomic_bank: list[tuple[str, dict[str, Any]]] = [
            ("is_production_outage", {"type": "noul", "instructions": "Is the customer reporting an active production degradation?"}),
            ("is_platinum_account", {"type": "noul", "instructions": "Is `account_tier` Platinum?"}),
            ("mentions_canary_rollout", {"type": "noul", "instructions": "Did the issue start after a canary deployment?"}),
            ("eu_jurisdiction", {"type": "noul", "instructions": "Is the affected workload in an EU region?"}),
            ("data_breach_reported", {"type": "noul", "instructions": "Does `customer_note` report leaked payment card data?"}),
            ("revenue_impact", {"type": "noul", "instructions": "Are shoppers abandoning carts due to this incident?"}),
            ("needs_rollback_guide", {"type": "noul", "instructions": "Is the customer asking for immediate rollback guidance?"}),
            ("high_arr_customer", {"type": "noul", "instructions": "Is `arr_usd` above 100000?"}),
            ("checkout_service_affected", {"type": "noul", "instructions": "Does the incident impact a checkout service?"}),
            ("latency_symptom", {"type": "noul", "instructions": "Does `customer_note` describe elevated latency?"}),
            (
                "incident_queue",
                {
                    "type": "choice",
                    "instructions": "Which engineering squad should receive this ticket?",
                    "criteria": {
                        "sre_cloud_run_canary": "Canary rollout regressions, latency spikes, or traffic splitting.",
                        "secops_incident_response": "Active credential leaks or confirmed data breaches.",
                        "billing_invoicing": "Contract renewals, invoices, or tax questions.",
                        "other": "General product feedback.",
                    },
                },
            ),
            (
                "severity_rubric",
                {
                    "type": "score",
                    "instructions": "Rate the operational urgency of this enterprise ticket.",
                    "criteria": [
                        "P3 - Minor cosmetic question.",
                        "P2 - Partial degradation with easy workaround.",
                        "P1 - Production checkout impact for a Platinum customer.",
                    ],
                },
            ),
        ]
        # Expand deterministically up to max requested count (up to 40)
        max_needed = max(question_counts)
        idx = len(atomic_bank)
        while len(atomic_bank) < max_needed:
            idx += 1
            atomic_bank.append(
                (
                    f"policy_guardrail_{idx}",
                    {
                        "type": "noul",
                        "instructions": f"Policy check #{idx}: Does `customer_note` remain professional and free of abusive language?",
                    },
                )
            )

        points: list[dict[str, Any]] = []
        for count in question_counts:
            q_subset = dict(atomic_bank[:count])
            t0 = time.perf_counter()
            raw = await self._call_decisions_api(
                input_payload=shared_context,
                questions=q_subset,
                api_key_override=api_key_override,
            )
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            usage = raw.get("usage") or {}
            in_tok = int(usage.get("total_input_tokens", 0))
            out_tok = int(usage.get("total_output_tokens", 0))
            decision_usd = (in_tok / 1_000_000.0) * DECISION_INPUT_USD_PER_1M
            gen_est_out_tok = count * 90
            gen_est_usd = (in_tok / 1_000_000.0) * 2.00 + (gen_est_out_tok / 1_000_000.0) * 10.00
            gen_est_latency_ms = 920 + (count * 115)

            points.append(
                {
                    "question_count": count,
                    "decision_latency_ms": latency_ms,
                    "generative_estimated_latency_ms": gen_est_latency_ms,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "decision_cost_usd": round(decision_usd, 8),
                    "generative_cost_usd": round(gen_est_usd, 8),
                    "cheaper_multiplier": round(gen_est_usd / decision_usd, 1) if decision_usd > 0 else 1.0,
                }
            )

        first_lat = points[0]["decision_latency_ms"] if points else 1.0
        last_lat = points[-1]["decision_latency_ms"] if points else 1.0
        return {
            "model": DECISION_MODEL_ID,
            "points": points,
            "takeaway": (
                f"Scaling from {points[0]['question_count']} to {points[-1]['question_count']} independent questions "
                f"completed in {last_lat} ms (vs {first_lat} ms for 1 question) with 0 output tokens billed."
            ),
        }

    async def evaluate_case_study(
        self,
        case_id: str,
        resolution_override: str | None = None,
        confidence_threshold: float = 0.75,
        api_key_override: str | None = None,
    ) -> dict[str, Any]:
        """Evaluate one Nano Banana multimodal case study live against `gemini-decision-1`."""
        case = get_case_study_by_id(case_id)
        if case is None:
            raise ValueError(f"Unknown case study '{case_id}'.")
        blocks = build_case_input_blocks(case, resolution_override=resolution_override)
        evaluation = await self.evaluate(
            input_blocks=blocks,
            questions=case["questions"],
            confidence_threshold=confidence_threshold,
            generative_baseline={
                "model": "gemini-4-argon",
                "estimated_output_and_thinking_tokens": 560,
                "estimated_latency_ms": 1950,
            },
            api_key_override=api_key_override,
        )
        evaluation["case_id"] = case["id"]
        evaluation["highlight_question"] = case["highlight_question"]
        return evaluation

    async def evaluate_all_case_studies(
        self,
        resolution_override: str | None = None,
        confidence_threshold: float = 0.75,
        api_key_override: str | None = None,
    ) -> dict[str, Any]:
        """Evaluate all 6 Nano Banana multimodal case studies concurrently in parallel."""
        import asyncio

        cases = list_case_studies()
        t0 = time.perf_counter()
        tasks = [
            self.evaluate_case_study(
                c["id"],
                resolution_override=resolution_override,
                confidence_threshold=confidence_threshold,
                api_key_override=api_key_override,
            )
            for c in cases
        ]
        results = await asyncio.gather(*tasks)
        wall_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        total_in = sum(r["usage"]["total_input_tokens"] for r in results)
        total_out = sum(r["usage"]["total_output_tokens"] for r in results)
        total_dec_usd = sum(r["call_economics"]["decision_cost_usd"] for r in results)
        total_gen_usd = sum(r["call_economics"]["generative_promo_cost_usd"] for r in results)
        savings_pct = (
            round((total_gen_usd - total_dec_usd) / total_gen_usd * 100.0, 2)
            if total_gen_usd > 0
            else 0.0
        )

        return {
            "model": DECISION_MODEL_ID,
            "case_count": len(results),
            "parallel_wall_clock_ms": wall_ms,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_decision_cost_usd": round(total_dec_usd, 7),
            "total_generative_baseline_usd": round(total_gen_usd, 6),
            "batch_savings_pct": savings_pct,
            "cheaper_multiplier": round(total_gen_usd / total_dec_usd, 1) if total_dec_usd > 0 else 1.0,
            "results_by_case": {r["case_id"]: r for r in results},
        }


def build_code_snippets(
    input_blocks: list[dict[str, Any]],
    questions: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Generate clean, copy-ready Python, JavaScript, and cURL REST snippets with `$GEMINI_API_KEY`."""
    input_json_py = json.dumps(input_blocks, indent=4)
    questions_json_py = json.dumps(questions, indent=4)

    python_code = (
        "import os\n"
        "from google import genai\n\n"
        "# Requires google-genai >= 3.2.0 (`pip install -U google-genai`)\n"
        'client = genai.Client(\n'
        '    api_key=os.environ["GEMINI_API_KEY"],\n'
        '    http_options={"api_version": "v1beta"},\n'
        ")\n\n"
        "result = client.decisions.create(\n"
        f'    model="{DECISION_MODEL_ID}",\n'
        f"    input={input_json_py},\n"
        f"    questions={questions_json_py},\n"
        ")\n\n"
        "for key, answer in result.answers.items():\n"
        '    print(key, answer.type, f"confidence={answer.confidence:.2f}")\n'
        'print("Output tokens billed:", result.usage.total_output_tokens)  # Always 0\n'
    )

    js_payload = json.dumps(
        {
            "model": DECISION_MODEL_ID,
            "input": input_blocks,
            "questions": questions,
        },
        indent=2,
    )

    js_code = (
        'import { GoogleGenAI } from "@google/genai";\n\n'
        '// Requires @google/genai >= 3.2.0 (`npm install @google/genai@latest`)\n'
        'const ai = new GoogleGenAI({ apiVersion: "v1beta" });\n\n'
        f"const result = await ai.decisions.create({js_payload});\n\n"
        "console.log(result.answers);\n"
        'console.log("Billed output tokens:", result.usage?.total_output_tokens); // 0\n'
    )

    curl_body = json.dumps(
        {
            "model": DECISION_MODEL_ID,
            "input": input_blocks,
            "questions": questions,
        },
        indent=2,
    ).replace("'", "'\\''")

    curl_code = (
        'curl -X POST "https://generativelanguage.googleapis.com/v1beta/decisions" \\\n'
        '  -H "x-goog-api-key: $GEMINI_API_KEY" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        f"  -d '{curl_body}'"
    )

    return {
        "python": python_code,
        "javascript": js_code,
        "curl": curl_code,
    }


def decode_validated_base64_upload(data_b64: str, mime_type: str) -> dict[str, str]:
    """Decode base64 upload, enforce magic-byte & size checks, and return a Decisions API content block."""
    cleaned_b64 = data_b64.strip()
    if "," in cleaned_b64 and cleaned_b64.startswith("data:"):
        cleaned_b64 = cleaned_b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(cleaned_b64, validate=True)
    except Exception as exc:
        raise ValueError("Invalid base64 media encoding.") from exc

    block_type = verify_magic_bytes(raw, mime_type)
    canonical_mime = "audio/mp3" if mime_type.lower().strip() == "audio/mpeg" else mime_type.lower().strip()
    return {
        "type": block_type,
        "data": base64.b64encode(raw).decode("ascii"),
        "mime_type": canonical_mime,
    }
