"""Unit, Security BFF, Adversarial PoC, FinOps, and Golden Trajectory Tests for Gemini Decisions API Studio."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import secrets
import struct
from typing import Any
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from demos.gemini_decisions_conference import CYBERSTRIKE_MODEL_ID, DECISION_MODEL_ID
from demos.gemini_decisions_conference.app import LLM_RATE_LIMIT_MAX_REQ, _safe_compare_str, create_decisions_demo_app
from demos.gemini_decisions_conference.engine import (
    DecisionsControlPlaneEngine,
    decode_validated_base64_upload,
    mask_pii,
)
from demos.gemini_decisions_conference.finops import calculate_finops_roi
from demos.gemini_decisions_conference.security_audit import audit_directory


def _make_png_bytes(width: int = 64, height: int = 64) -> bytes:
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00" + b"\x00" * 12


class StubDecisionsEngine(DecisionsControlPlaneEngine):
    """Deterministic stub engine for fast offline unit & security verification."""

    def __init__(self) -> None:
        super().__init__(api_key_override="stub-configured")
        self.last_used_key: str | None = None

    async def _call_decisions_api(
        self,
        *,
        input_payload: str | list[dict[str, Any]],
        questions: dict[str, dict[str, Any]],
        api_key_override: str | None = None,
    ) -> dict[str, Any]:
        self.last_used_key = api_key_override or self._api_key
        answers: dict[str, Any] = {}
        for key, spec in questions.items():
            qtype = spec["type"]
            if qtype == "noul":
                answers[key] = {"type": "noul", "noul": 0.96, "confidence": 0.96}
            elif qtype == "choice":
                opts = list((spec.get("criteria") or {}).keys())
                winner = opts[0]
                probs = {
                    o: (0.91 if i == 0 else round(0.09 / max(1, len(opts) - 1), 4))
                    for i, o in enumerate(opts)
                }
                answers[key] = {
                    "type": "choice",
                    "choice": winner,
                    "probabilities": probs,
                    "confidence": 0.91,
                }
            elif qtype == "score":
                levels = list(spec.get("criteria") or [])
                legend = {str(i): desc for i, desc in enumerate(levels)}
                probs = {
                    str(i): (0.85 if i == len(levels) - 1 else round(0.15 / max(1, len(levels) - 1), 4))
                    for i in range(len(levels))
                }
                expected = sum(int(k) * v for k, v in probs.items())
                answers[key] = {
                    "type": "score",
                    "score": round(expected, 3),
                    "legend": legend,
                    "probabilities": probs,
                    "confidence": 0.85,
                }
        return {
            "answers": answers,
            "usage": {
                "total_input_tokens": 340 + len(questions) * 28,
                "total_output_tokens": 0,
                "total_tokens": 340 + len(questions) * 28,
                "input_tokens_by_modality": [{"modality": "text", "tokens": 340 + len(questions) * 28}],
            },
        }


class GeminiDecisionsConferenceDemoTest(unittest.TestCase):
    def test_finops_zero_output_tokens_and_hybrid_anti_pattern_math(self) -> None:
        roi = calculate_finops_roi(
            daily_requests=500_000,
            shared_input_tokens=1_500,
            questions_per_request=8,
            generative_output_tokens_per_question=100,
            baseline_tier_id="gemini_4_argon_promo",
            escalation_rate_pct=10.0,
        )
        self.assertEqual(roi["decision_api"]["output_tokens_per_req"], 0)
        self.assertGreater(roi["headline_savings"]["savings_pct"], 94.0)
        self.assertGreater(roi["headline_savings"]["cheaper_multiplier"], 20.0)
        self.assertGreater(roi["headline_savings"]["annual_savings_usd"], 1_000_000.0)
        self.assertLess(roi["hybrid_cascade"]["monthly_cost_usd"], roi["generative_baseline"]["monthly_cost_usd"])

        # Hybrid Smart Cascade escalated pass should remain single-pass even when comparing against N-call anti-pattern
        roi_anti = calculate_finops_roi(
            daily_requests=500_000,
            shared_input_tokens=1_500,
            questions_per_request=8,
            generative_output_tokens_per_question=100,
            baseline_tier_id="gemini_4_argon_promo",
            escalation_rate_pct=10.0,
            separate_calls_anti_pattern=True,
        )
        self.assertEqual(roi_anti["hybrid_cascade"]["monthly_cost_usd"], roi["hybrid_cascade"]["monthly_cost_usd"])
        self.assertGreater(
            roi_anti["generative_baseline"]["monthly_cost_usd"],
            roi["generative_baseline"]["monthly_cost_usd"],
        )

    def test_magic_bytes_decompression_bomb_and_mp3_bom_guards(self) -> None:
        valid_png = _make_png_bytes(128, 128)
        block = decode_validated_base64_upload(base64.b64encode(valid_png).decode("ascii"), "image/png")
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["mime_type"], "image/png")

        # Reject spoofed HTML polyglot
        spoofed_html = b"<script>fetch('//attacker')</script>"
        with self.assertRaises(ValueError):
            decode_validated_base64_upload(base64.b64encode(spoofed_html).decode("ascii"), "image/png")

        # Reject disallowed MIME
        with self.assertRaises(ValueError):
            decode_validated_base64_upload(base64.b64encode(valid_png).decode("ascii"), "application/x-sh")

        # Reject PNG decompression bomb (30,000 x 30,000 = 900 MP > 100 MP limit)
        bomb_png = _make_png_bytes(30_000, 30_000)
        with self.assertRaises(ValueError):
            decode_validated_base64_upload(base64.b64encode(bomb_png).decode("ascii"), "image/png")

        # Reject PNG missing IHDR header
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        with self.assertRaises(ValueError):
            decode_validated_base64_upload(base64.b64encode(fake_png).decode("ascii"), "image/png")

        # Reject UTF-16LE BOM masquerading as MP3 frame sync (`0xFF 0xFE 'H' 0x00 'i' 0x00`)
        utf16_text = b"\xff\xfeH\x00i\x00!\x00"
        with self.assertRaises(ValueError):
            decode_validated_base64_upload(base64.b64encode(utf16_text).decode("ascii"), "audio/mpeg")

        # Reject data: URI MIME mismatch
        png_data_url = "data:image/png;base64," + base64.b64encode(valid_png).decode("ascii")
        with self.assertRaises(ValueError):
            decode_validated_base64_upload(png_data_url, "application/pdf")

    def test_pii_and_api_secret_redaction(self) -> None:
        fake_aiza = "AIza" + "SyDummyKeyMaterial1234567890abcdefXYZ"
        fake_vertex = "AQ." + "Ab8RN6LDummyVertexExpressKey99887766"
        fake_oauth = "ya29." + "a0AfH6SMBDummyOAuthAccessTokenValue12345"
        raw = (
            f"Contact alice.architect@example.com with Authorization: Bearer secretToken9912 "
            f"or key={fake_aiza} or x-goog-api-key: {fake_vertex} or token {fake_oauth}"
        )
        masked = mask_pii(raw)
        self.assertNotIn("alice.architect@example.com", masked)
        self.assertNotIn("secretToken9912", masked)
        self.assertNotIn(fake_aiza, masked)
        self.assertNotIn(fake_vertex, masked)
        self.assertNotIn(fake_oauth, masked)
        self.assertIn("[REDACTED", masked)

    def test_golden_trajectories_and_cyberstrike_routing(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        dataset_path = repo_root / "evals" / "datasets" / "decisions_golden_trajectories.jsonl"
        engine = StubDecisionsEngine()
        lines = [l.strip() for l in dataset_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertGreaterEqual(len(lines), 4)

        for line in lines:
            case = json.loads(line)
            gate = engine._compute_confidence_gating(
                answers=case["mock_answers"],
                threshold=float(case["threshold"]),
            )
            self.assertEqual(gate["auto_execute_count"], case["expected_auto_count"], msg=case["id"])
            self.assertEqual(gate["cyberstrike_escalation_recommended"], case["expected_cyberstrike"], msg=case["id"])
            self.assertEqual(gate["cyberstrike_specialist_model"], CYBERSTRIKE_MODEL_ID)

            # Verify two-pass dict-order invariance by reversing mock_answers keys
            reversed_answers = dict(reversed(list(case["mock_answers"].items())))
            gate_rev = engine._compute_confidence_gating(
                answers=reversed_answers,
                threshold=float(case["threshold"]),
            )
            self.assertEqual(
                gate_rev["cyberstrike_escalation_recommended"],
                case["expected_cyberstrike"],
                msg=f"{case['id']} reversed order",
            )

    def test_bff_security_headers_csrf_evaluate_benchmark_and_logout(self) -> None:
        app = create_decisions_demo_app(engine_override=StubDecisionsEngine())
        with TestClient(app, base_url="http://127.0.0.1:8090") as client:
            # 1. Index, /docs Medium Article & Security Headers
            index_res = client.get("/")
            self.assertEqual(index_res.status_code, 200)
            self.assertIn("nonce-", index_res.headers["Content-Security-Policy"])
            self.assertIn("style-src 'self'", index_res.headers["Content-Security-Policy"])
            self.assertEqual(index_res.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(index_res.headers["X-Frame-Options"], "DENY")
            self.assertEqual(index_res.headers["Cross-Origin-Opener-Policy"], "same-origin-allow-popups")
            self.assertEqual(index_res.headers["Cross-Origin-Resource-Policy"], "same-origin")
            self.assertEqual(index_res.headers["Cache-Control"], "no-store")
            self.assertNotIn('style="', index_res.text)
            self.assertIn('id="securityAuditDialog"', index_res.text)

            fav_res = client.get("/favicon.ico")
            self.assertEqual(fav_res.status_code, 200)
            robots_res = client.get("/robots.txt")
            self.assertEqual(robots_res.status_code, 200)
            self.assertIn("Disallow: /api/", robots_res.text)

            docs_res = client.get("/docs")
            self.assertEqual(docs_res.status_code, 200)
            self.assertEqual(client.head("/docs").status_code, 200)
            self.assertIn("nonce-", docs_res.headers["Content-Security-Policy"])
            self.assertEqual(docs_res.headers["Cache-Control"], "no-store")
            self.assertIn('id="mediumArticleBody"', docs_res.text)
            self.assertNotIn("__CSP_NONCE__", docs_res.text)

            case_img_res = client.get("/static/cases/parcel_damaged.jpg")
            self.assertEqual(case_img_res.status_code, 200)
            self.assertEqual(case_img_res.headers.get("Cross-Origin-Resource-Policy"), "cross-origin")
            self.assertEqual(case_img_res.headers.get("Cache-Control"), "public, max-age=86400")

            # 2. HTTP Method allowlist blocks DELETE with 405 AND still attaches security headers
            del_res = client.delete("/api/decisions/evaluate")
            self.assertEqual(del_res.status_code, 405)
            self.assertEqual(del_res.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertIn("Content-Security-Policy", del_res.headers)

            # 2b. CORS OPTIONS preflight works cleanly AND receives full security headers
            opt_res = client.options(
                "/api/decisions/evaluate",
                headers={
                    "Origin": "http://127.0.0.1:8090",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "Content-Type,X-CSRF-Token",
                },
            )
            self.assertEqual(opt_res.status_code, 200)
            self.assertEqual(opt_res.headers.get("access-control-allow-origin"), "http://127.0.0.1:8090")
            self.assertEqual(opt_res.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertIn("Content-Security-Policy", opt_res.headers)

            # 3. Bootstrap retrieves CSRF token, scenarios & deduplicated Vary header
            boot_raw = client.get("/api/bootstrap", headers={"Origin": "http://127.0.0.1:8090"})
            vary_tokens = [v.strip() for v in boot_raw.headers.get("Vary", "").split(",") if v.strip()]
            self.assertEqual(len(vary_tokens), len({v.lower() for v in vary_tokens}))
            self.assertIn("Origin", vary_tokens)
            boot = boot_raw.json()
            csrf = boot["csrf_token"]
            self.assertEqual(boot["decision_model"], DECISION_MODEL_ID)
            self.assertEqual(len(boot["scenarios"]), 6)
            self.assertEqual(boot["security_posture"]["status"], "HARDENED")
            self.assertEqual(boot["security_posture"]["open_findings"], 0)
            self.assertGreaterEqual(len(boot["security_posture"]["controls"]), 6)

            # 4. Missing CSRF token, cross-site Sec-Fetch-Site, or untrusted Origin are rejected with 403
            no_csrf = client.post("/api/finops/calculate", json={"daily_requests": 100000})
            self.assertEqual(no_csrf.status_code, 403)

            cross_site = client.post(
                "/api/finops/calculate",
                headers={"X-CSRF-Token": csrf, "Sec-Fetch-Site": "cross-site"},
                json={"daily_requests": 100000},
            )
            self.assertEqual(cross_site.status_code, 403)

            bad_origin = client.post(
                "/api/finops/calculate",
                headers={"X-CSRF-Token": csrf, "Origin": "https://evil.attacker.example"},
                json={"daily_requests": 100000},
            )
            self.assertEqual(bad_origin.status_code, 403)

            # 4b. Schema validation: `noul` with criteria or invalid FinOps tier returns 422
            bad_noul = client.post(
                "/api/decisions/evaluate",
                headers={"X-CSRF-Token": csrf},
                json={
                    "custom_text": "hello",
                    "questions": {
                        "q1": {
                            "type": "noul",
                            "instructions": "Is this valid?",
                            "criteria": ["unexpected"],
                        }
                    },
                },
            )
            self.assertEqual(bad_noul.status_code, 422)

            bad_tier = client.post(
                "/api/finops/calculate",
                headers={"X-CSRF-Token": csrf},
                json={"baseline_tier_id": "nonexistent_tier"},
            )
            self.assertEqual(bad_tier.status_code, 422)

            # 5. Valid CSRF evaluate & benchmark succeed with 0 output tokens
            first_sc = boot["scenarios"][0]
            eval_res = client.post(
                "/api/decisions/evaluate",
                headers={"X-CSRF-Token": csrf},
                json={
                    "scenario_id": first_sc["id"],
                    "questions": first_sc["questions"],
                    "confidence_threshold": 0.75,
                },
            )
            self.assertEqual(eval_res.status_code, 200)
            eval_json = eval_res.json()
            self.assertEqual(eval_json["usage"]["total_output_tokens"], 0)
            self.assertIn("python", eval_json["snippets"])

            bench_res = client.post(
                "/api/decisions/benchmark",
                headers={"X-CSRF-Token": csrf},
                json={"counts": [1, 5, 12]},
            )
            self.assertEqual(bench_res.status_code, 200)
            self.assertEqual(len(bench_res.json()["points"]), 3)

            # 5b. Visual Nano Banana Multimodal Case Studies (single + all-6 parallel batch)
            self.assertEqual(len(boot["case_studies"]), 6)
            cases_res = client.post(
                "/api/cases/evaluate",
                headers={"X-CSRF-Token": csrf},
                json={"confidence_threshold": 0.75},
            )
            self.assertEqual(cases_res.status_code, 200)
            cases_json = cases_res.json()
            self.assertEqual(cases_json["case_count"], 6)
            self.assertEqual(cases_json["total_output_tokens"], 0)

            # 6. Logout invalidates session token
            logout_res = client.post("/api/session/logout", headers={"X-CSRF-Token": csrf})
            self.assertEqual(logout_res.status_code, 200)
            after_logout = client.post("/api/finops/calculate", headers={"X-CSRF-Token": csrf}, json={})
            self.assertEqual(after_logout.status_code, 403)

    def test_cloud_run_multi_replica_hkdf_keyring_rotation_and_hsts(self) -> None:
        old_secret = secrets.token_hex(32)
        new_secret = secrets.token_hex(32)

        # Short secret (< 32 chars) must fail fast at startup
        with self.assertRaises(ValueError):
            create_decisions_demo_app(engine_override=StubDecisionsEngine(), session_secret="too-short")

        replica_a = TestClient(
            create_decisions_demo_app(engine_override=StubDecisionsEngine(), session_secret=old_secret)
        )
        # Replica B rotates to `new_secret` as primary while retaining `old_secret` in comma-separated key ring
        replica_b = TestClient(
            create_decisions_demo_app(
                engine_override=StubDecisionsEngine(),
                session_secret=f"{new_secret},{old_secret}",
            )
        )

        boot_a = replica_a.get("/api/bootstrap", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(boot_a.status_code, 200)
        self.assertIn("max-age=", boot_a.headers.get("Strict-Transport-Security", ""))
        set_cookie_hdr = boot_a.headers.get("set-cookie", "")
        self.assertIn("__Host-decisions_sid=", set_cookie_hdr)
        self.assertIn("Secure", set_cookie_hdr)
        csrf = boot_a.json()["csrf_token"]
        cookie_val = boot_a.cookies.get("__Host-decisions_sid")
        self.assertTrue(cookie_val)

        # Leftmost X-Forwarded-Proto downgrade spoof (`http, https`) must still be treated as HTTPS
        replica_b.cookies.set("__Host-decisions_sid", cookie_val)
        res_b = replica_b.post(
            "/api/finops/calculate",
            headers={
                "X-CSRF-Token": csrf,
                "X-Forwarded-Proto": "http, https",
                "X-Cloud-Trace-Context": "105445aa7843bc8bf206b12000100000/1;o=1",
            },
            json={"daily_requests": 50000},
        )
        self.assertEqual(res_b.status_code, 200)
        self.assertIn("Strict-Transport-Security", res_b.headers)

    def test_byok_session_rotation_secure_cookie_deletion_and_aad_binding(self) -> None:
        shared_secret = secrets.token_hex(32)
        stub_a = StubDecisionsEngine()
        stub_b = StubDecisionsEngine()
        replica_a = TestClient(
            create_decisions_demo_app(engine_override=stub_a, session_secret=shared_secret),
            base_url="https://testserver",
        )
        replica_b = TestClient(
            create_decisions_demo_app(engine_override=stub_b, session_secret=shared_secret),
            base_url="https://testserver",
        )

        boot_res = replica_a.get("/api/bootstrap", headers={"X-Forwarded-Proto": "https"})
        old_sid_cookie = replica_a.cookies.get("__Host-decisions_sid")
        old_csrf = boot_res.json()["csrf_token"]
        self.assertFalse(boot_res.json()["byok"]["connected"])

        # Connect user BYOK key -> rotates session ID & CSRF token
        candidate_user_key = "UserDemoKey_" + secrets.token_urlsafe(18)
        conn_res = replica_a.post(
            "/api/session/api-key",
            headers={"X-CSRF-Token": old_csrf, "X-Forwarded-Proto": "https"},
            json={"api_key": candidate_user_key, "verify": True},
        )
        self.assertEqual(conn_res.status_code, 200)
        conn_json = conn_res.json()
        self.assertTrue(conn_json["connected"])
        rotated_csrf = conn_json["csrf_token"]
        self.assertNotEqual(rotated_csrf, old_csrf)
        self.assertNotIn(candidate_user_key, json.dumps(conn_json))

        rotated_sid_cookie = replica_a.cookies.get("__Host-decisions_sid")
        byok_cookie = replica_a.cookies.get("__Host-decisions_byok")
        self.assertNotEqual(rotated_sid_cookie, old_sid_cookie)
        self.assertTrue(byok_cookie)
        self.assertNotIn(candidate_user_key, byok_cookie)

        # Pre-elevation session cookie is revoked on Replica A (session-fixation defense)
        stale_client = TestClient(replica_a.app, base_url="https://testserver")
        stale_client.cookies.set("__Host-decisions_sid", old_sid_cookie)
        stale_attempt = stale_client.post(
            "/api/finops/calculate",
            headers={"X-CSRF-Token": old_csrf, "X-Forwarded-Proto": "https"},
            json={},
        )
        self.assertEqual(stale_attempt.status_code, 403)

        # Execute Custom Scenario on Replica B using rotated session + encrypted BYOK cookie
        replica_b.cookies.set("__Host-decisions_sid", rotated_sid_cookie)
        replica_b.cookies.set("__Host-decisions_byok", byok_cookie)
        eval_res = replica_b.post(
            "/api/decisions/evaluate",
            headers={"X-CSRF-Token": rotated_csrf, "X-Forwarded-Proto": "https"},
            json={
                "scenario_id": None,
                "custom_text": "לקוח VIP מדווח על תקלה דחופה במערכת הסליקה ומבקש פתרון מיידי.",
                "include_scenario_media": False,
                "questions": {
                    "is_vip_urgent": {
                        "type": "noul",
                        "instructions": "Is this an urgent VIP payment issue?",
                    },
                    "team_route": {
                        "type": "choice",
                        "instructions": "Select response team.",
                        "criteria": {
                            "sre_payments": "Urgent payment gateway outage.",
                            "general_support": "Routine question.",
                        },
                    },
                },
                "confidence_threshold": 0.80,
            },
        )
        self.assertEqual(eval_res.status_code, 200)
        eval_json = eval_res.json()
        self.assertEqual(eval_json["key_source"], "user_byok")
        self.assertEqual(stub_b.last_used_key, candidate_user_key)

        # Replay defense: stealing __Host-decisions_byok into a different session must NOT decrypt!
        attacker = TestClient(
            create_decisions_demo_app(engine_override=StubDecisionsEngine(), session_secret=shared_secret),
            base_url="https://testserver",
        )
        attacker.get("/api/bootstrap", headers={"X-Forwarded-Proto": "https"})
        attacker.cookies.set("__Host-decisions_byok", byok_cookie)
        att_check = attacker.get("/api/bootstrap", headers={"X-Forwarded-Proto": "https"}).json()
        self.assertFalse(att_check["byok"]["connected"])

        # SEC-02 PoC: Disconnect emits RFC 6265bis `Secure; HttpOnly; SameSite=strict` on `__Host-` Set-Cookie deletion!
        clr = replica_b.post(
            "/api/session/api-key/clear",
            headers={"X-CSRF-Token": rotated_csrf, "X-Forwarded-Proto": "https"},
        )
        self.assertEqual(clr.status_code, 200)
        clr_cookie_hdr = clr.headers.get("set-cookie", "")
        self.assertIn("__Host-decisions_byok=", clr_cookie_hdr)
        self.assertIn("Secure", clr_cookie_hdr)
        self.assertIn("HttpOnly", clr_cookie_hdr)

        # Logout over HTTPS emits `Secure` cookie deletion AND `Clear-Site-Data: "cookies"`
        out_res = replica_b.post(
            "/api/session/logout",
            headers={"X-CSRF-Token": rotated_csrf, "X-Forwarded-Proto": "https"},
        )
        self.assertEqual(out_res.status_code, 200)
        self.assertEqual(out_res.headers.get("Clear-Site-Data"), '"cookies"')
        self.assertIn("Secure", out_res.headers.get("set-cookie", ""))

    def test_xff_spoofing_cannot_bypass_llm_rate_limiter(self) -> None:
        """SEC-01 PoC: Rotating spoofed leftmost X-Forwarded-For headers cannot bypass LLM rate limits."""
        with patch.dict(os.environ, {"DECISIONS_TRUST_PROXY": "1"}):
            app = create_decisions_demo_app(engine_override=StubDecisionsEngine())
            client = TestClient(app, base_url="https://testserver")
            boot = client.get("/api/bootstrap", headers={"X-Forwarded-For": "198.51.100.77"}).json()
            csrf = boot["csrf_token"]

            last_status = 200
            for idx in range(LLM_RATE_LIMIT_MAX_REQ + 2):
                # Attacker rotates leftmost XFF IP on every call, while GFE appends real rightmost peer IP `198.51.100.77`
                spoofed_xff = f"203.0.113.{idx}, 198.51.100.77"
                res = client.post(
                    "/api/decisions/evaluate",
                    headers={"X-CSRF-Token": csrf, "X-Forwarded-For": spoofed_xff},
                    json={
                        "custom_text": "Rate limit PoC",
                        "questions": {"q1": {"type": "noul", "instructions": "Is this rate limited?"}},
                    },
                )
                last_status = res.status_code
                if last_status == 429:
                    self.assertEqual(res.headers.get("X-Content-Type-Options"), "nosniff")
                    self.assertIn("Content-Security-Policy", res.headers)
                    break

            self.assertEqual(last_status, 429)

    def test_non_ascii_headers_and_oversized_streaming_body_do_not_crash(self) -> None:
        """SEC-04 & SEC-05 PoC: Raw latin-1 header bytes return 403 (never 500) and >13.5 MB bodies return 413."""
        self.assertFalse(_safe_compare_str("éÿþ-non-ascii", "expected_ascii_token"))
        self.assertTrue(_safe_compare_str("exact_match_token_123", "exact_match_token_123"))

        app = create_decisions_demo_app(engine_override=StubDecisionsEngine())
        client = TestClient(app, base_url="https://testserver")
        boot_res = client.get("/api/bootstrap")
        csrf = boot_res.json()["csrf_token"]
        sid_cookie = client.cookies.get("__Host-decisions_sid") or ""

        # Raw latin-1 bytes (`\xff\xfe`) sent over wire in X-CSRF-Token and Cookie must never raise 500 TypeError
        non_ascii_csrf = client.post(
            "/api/finops/calculate",
            headers=[
                (b"x-csrf-token", b"\xff\xfe-non-ascii-token"),
                (b"cookie", f"__Host-decisions_sid={sid_cookie}".encode("ascii")),
            ],
            json={"daily_requests": 50000},
        )
        self.assertEqual(non_ascii_csrf.status_code, 403)

        bad_cookie_res = client.get(
            "/api/bootstrap",
            headers=[(b"cookie", b"__Host-decisions_sid=v1.deadbeef.sid.csrf.9999999999.\xff\xfe")],
        )
        self.assertEqual(bad_cookie_res.status_code, 200)

        # Streaming body exceeding 13.5 MB returns 413 Payload Too Large with security headers + HSTS
        oversized_payload = b'{"custom_text":"' + (b"A" * 13_600_000) + b'"}'
        big_res = client.post(
            "/api/decisions/evaluate",
            content=oversized_payload,
            headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
        )
        self.assertEqual(big_res.status_code, 413)
        self.assertEqual(big_res.headers.get("x-content-type-options"), "nosniff")
        self.assertIn("strict-transport-security", {k.lower() for k in big_res.headers.keys()})

    def test_cloud_run_production_cors_excludes_localhost_and_locks_allowed_origin(self) -> None:
        prod_origin = "https://gemini-decisions-studio-1085727639300.us-central1.run.app"
        with patch.dict(
            os.environ,
            {
                "K_SERVICE": "gemini-decisions-studio",
                "DECISIONS_ALLOWED_ORIGIN": prod_origin,
                "DECISIONS_DEMO_SESSION_SECRET": secrets.token_hex(32),
            },
        ):
            app = create_decisions_demo_app(engine_override=StubDecisionsEngine())
            client = TestClient(app, base_url=prod_origin)
            boot = client.get("/api/bootstrap").json()
            csrf = boot["csrf_token"]

            # Localhost Origin must NOT be accepted against Cloud Run production
            localhost_attempt = client.post(
                "/api/finops/calculate",
                headers={"X-CSRF-Token": csrf, "Origin": "http://localhost:8090"},
                json={"daily_requests": 50000},
            )
            self.assertEqual(localhost_attempt.status_code, 403)

            # Locked Cloud Run HTTPS Origin succeeds
            prod_ok = client.post(
                "/api/finops/calculate",
                headers={"X-CSRF-Token": csrf, "Origin": prod_origin},
                json={"daily_requests": 50000},
            )
            self.assertEqual(prod_ok.status_code, 200)

    def test_entire_workspace_passes_zero_finding_security_auditor(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        report = audit_directory(repo_root)
        self.assertTrue(report["passed"], msg=json.dumps(report["findings"], indent=2))


if __name__ == "__main__":
    unittest.main()
