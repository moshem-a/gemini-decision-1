"""Unit, Security BFF, FinOps, and Golden Trajectory Tests for Gemini Decisions API Studio."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
import unittest

from fastapi.testclient import TestClient

from demos.gemini_decisions_conference.security_audit import audit_directory
from demos.gemini_decisions_conference import CYBERSTRIKE_MODEL_ID, DECISION_MODEL_ID
from demos.gemini_decisions_conference.app import create_decisions_demo_app
from demos.gemini_decisions_conference.engine import (
    DecisionsControlPlaneEngine,
    decode_validated_base64_upload,
    mask_pii,
)
from demos.gemini_decisions_conference.finops import calculate_finops_roi


class StubDecisionsEngine(DecisionsControlPlaneEngine):
    """Deterministic stub engine for fast offline unit & security verification."""

    def __init__(self) -> None:
        self._workspace_root = None
        self._api_key = "stub-configured"
        self._client = object()  # type: ignore[assignment]

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
                probs = {o: (0.91 if i == 0 else round(0.09 / max(1, len(opts) - 1), 4)) for i, o in enumerate(opts)}
                answers[key] = {
                    "type": "choice",
                    "choice": winner,
                    "probabilities": probs,
                    "confidence": 0.91,
                }
            elif qtype == "score":
                levels = list(spec.get("criteria") or [])
                legend = {str(i): desc for i, desc in enumerate(levels)}
                probs = {str(i): (0.85 if i == len(levels) - 1 else round(0.15 / max(1, len(levels) - 1), 4)) for i in range(len(levels))}
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
    def test_finops_zero_output_tokens_and_massive_savings(self) -> None:
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

    def test_magic_bytes_accepts_valid_png_and_rejects_spoofed_polyglot(self) -> None:
        valid_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        block = decode_validated_base64_upload(base64.b64encode(valid_png).decode("ascii"), "image/png")
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["mime_type"], "image/png")

        spoofed_html = b"<script>fetch('//attacker')</script>"
        with self.assertRaises(ValueError):
            decode_validated_base64_upload(base64.b64encode(spoofed_html).decode("ascii"), "image/png")

        with self.assertRaises(ValueError):
            decode_validated_base64_upload(base64.b64encode(valid_png).decode("ascii"), "application/x-sh")

    def test_pii_and_bearer_redaction(self) -> None:
        raw = "Contact alice.architect@example.com with Authorization: Bearer secretToken9912"
        masked = mask_pii(raw)
        self.assertNotIn("alice.architect@example.com", masked)
        self.assertNotIn("secretToken9912", masked)
        self.assertIn("[REDACTED]", masked)

    def test_golden_trajectories_and_cyberstrike_routing(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        dataset_path = repo_root / "evals" / "datasets" / "decisions_golden_trajectories.jsonl"
        engine = StubDecisionsEngine()
        lines = [l.strip() for l in dataset_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertGreaterEqual(len(lines), 3)

        for line in lines:
            case = json.loads(line)
            gate = engine._compute_confidence_gating(
                answers=case["mock_answers"],
                threshold=float(case["threshold"]),
            )
            self.assertEqual(gate["auto_execute_count"], case["expected_auto_count"], msg=case["id"])
            self.assertEqual(gate["cyberstrike_escalation_recommended"], case["expected_cyberstrike"], msg=case["id"])
            self.assertEqual(gate["cyberstrike_specialist_model"], CYBERSTRIKE_MODEL_ID)

    def test_bff_security_headers_csrf_evaluate_benchmark_and_logout(self) -> None:
        app = create_decisions_demo_app(engine_override=StubDecisionsEngine())
        client = TestClient(app, base_url="http://127.0.0.1:8090")

        # 1. Index, /docs Medium Article & Security Headers
        index_res = client.get("/")
        self.assertEqual(index_res.status_code, 200)
        self.assertIn("nonce-", index_res.headers["Content-Security-Policy"])
        self.assertEqual(index_res.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(index_res.headers["X-Frame-Options"], "DENY")
        self.assertEqual(index_res.headers["Cache-Control"], "no-store")

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

        # 2. HTTP Method allowlist blocks DELETE with 405
        del_res = client.delete("/api/decisions/evaluate")
        self.assertEqual(del_res.status_code, 405)

        # 3. Bootstrap retrieves CSRF token & scenarios
        boot = client.get("/api/bootstrap").json()
        csrf = boot["csrf_token"]
        self.assertEqual(boot["decision_model"], DECISION_MODEL_ID)
        self.assertEqual(len(boot["scenarios"]), 6)

        # 4. Missing CSRF token is rejected with 403
        no_csrf = client.post("/api/finops/calculate", json={"daily_requests": 100000})
        self.assertEqual(no_csrf.status_code, 403)

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

    def test_cloud_run_multi_replica_session_and_forwarded_https(self) -> None:
        import secrets

        shared_secret = secrets.token_hex(32)
        replica_a = TestClient(
            create_decisions_demo_app(engine_override=StubDecisionsEngine(), session_secret=shared_secret)
        )
        replica_b = TestClient(
            create_decisions_demo_app(engine_override=StubDecisionsEngine(), session_secret=shared_secret)
        )

        # Simulate Cloud Run TLS-terminating proxy via X-Forwarded-Proto: https on Replica A
        boot_a = replica_a.get("/api/bootstrap", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(boot_a.status_code, 200)
        set_cookie_hdr = boot_a.headers.get("set-cookie", "")
        self.assertIn("__Host-decisions_sid=", set_cookie_hdr)
        self.assertIn("Secure", set_cookie_hdr)
        csrf = boot_a.json()["csrf_token"]
        cookie_val = boot_a.cookies.get("__Host-decisions_sid")
        self.assertTrue(cookie_val)

        # Forward request to Replica B using cookie issued by Replica A
        replica_b.cookies.set("__Host-decisions_sid", cookie_val)
        res_b = replica_b.post(
            "/api/finops/calculate",
            headers={
                "X-CSRF-Token": csrf,
                "X-Forwarded-Proto": "https",
                "X-Cloud-Trace-Context": "105445aa7843bc8bf206b12000100000/1;o=1",
            },
            json={"daily_requests": 50000},
        )
        self.assertEqual(res_b.status_code, 200)

    def test_byok_encrypted_cookie_and_custom_scenario_evaluation(self) -> None:
        import secrets

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

        boot = replica_a.get("/api/bootstrap", headers={"X-Forwarded-Proto": "https"}).json()
        csrf = boot["csrf_token"]
        self.assertFalse(boot["byok"]["connected"])

        # Connect user BYOK key on Replica A (construct dynamically so static secret scanners stay happy)
        candidate_user_key = "UserDemoKey_" + secrets.token_urlsafe(18)
        conn_res = replica_a.post(
            "/api/session/api-key",
            headers={"X-CSRF-Token": csrf, "X-Forwarded-Proto": "https"},
            json={"api_key": candidate_user_key, "verify": True},
        )
        self.assertEqual(conn_res.status_code, 200)
        conn_json = conn_res.json()
        self.assertTrue(conn_json["connected"])
        self.assertNotIn(candidate_user_key, json.dumps(conn_json))
        self.assertTrue(conn_json["masked_key"].endswith(candidate_user_key[-4:]))

        sid_cookie = replica_a.cookies.get("__Host-decisions_sid")
        byok_cookie = replica_a.cookies.get("__Host-decisions_byok")
        self.assertTrue(byok_cookie)
        self.assertNotIn(candidate_user_key, byok_cookie)

        # Execute a pure Custom Scenario (scenario_id=None) on Replica B using encrypted BYOK cookie
        replica_b.cookies.set("__Host-decisions_sid", sid_cookie)
        replica_b.cookies.set("__Host-decisions_byok", byok_cookie)
        eval_res = replica_b.post(
            "/api/decisions/evaluate",
            headers={"X-CSRF-Token": csrf, "X-Forwarded-Proto": "https"},
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
        self.assertIn("is_vip_urgent", eval_json["answers"])

        # Replay defense: stealing __Host-decisions_byok into a different session must NOT decrypt!
        attacker = TestClient(
            create_decisions_demo_app(engine_override=StubDecisionsEngine(), session_secret=shared_secret),
            base_url="https://testserver",
        )
        att_boot = attacker.get("/api/bootstrap", headers={"X-Forwarded-Proto": "https"}).json()
        attacker.cookies.set("__Host-decisions_byok", byok_cookie)
        att_check = attacker.get("/api/bootstrap", headers={"X-Forwarded-Proto": "https"}).json()
        self.assertFalse(att_check["byok"]["connected"])
        _ = att_boot

        # Disconnect clears BYOK cookie
        clr = replica_b.post(
            "/api/session/api-key/clear",
            headers={"X-CSRF-Token": csrf, "X-Forwarded-Proto": "https"},
        )
        self.assertEqual(clr.status_code, 200)
        self.assertFalse(clr.json()["connected"])

    def test_entire_workspace_passes_zero_finding_security_auditor(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        report = audit_directory(repo_root)
        self.assertTrue(report["passed"], msg=json.dumps(report["findings"], indent=2))


if __name__ == "__main__":
    unittest.main()
