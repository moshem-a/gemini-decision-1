# Stop Paying Generative LLMs to Make Decisions: Building a Multimodal AI Control Plane with Google's Gemini Decisions API on Cloud Run

### How `gemini-decision-1` replaces brittle JSON prompts with single-pass calibrated probabilities, zero output tokens ($0.00), sub-400ms multimodal latency, and 85x–161x lower FinOps spend.

- **Hosted Medium Import URL (`/docs`):** https://gemini-decisions-studio-1085727639300.us-central1.run.app/docs
- **Live Interactive Cloud Run Studio:** https://gemini-decisions-studio-1085727639300.us-central1.run.app
- **Open-Source GitHub Repository:** https://github.com/moshem-a/gemini-decision-1

---

Here is an uncomfortable truth about modern production AI systems: **roughly 80% to 90% of enterprise LLM calls are not asking a model to write anything at all.**

Look inside almost any customer support router, e-commerce claims pipeline, Travel & Expense receipt auditor, RAG relevance filter, or security guardrail. Behind the scenes, teams are sending images, PDFs, audio clips, and JSON payloads to heavy generative models—such as `gemini-4-argon` or `gemini-4-carbon`—just to get back a tiny routing decision:

- *"Does the courier's delivery photo actually match the customer's damaged-package claim?"* (`Yes / No`)
- *"Which specialist queue or downstream model should handle this request?"* (`Closed-set choice`)
- *"How severe is the compliance or prompt-injection threat embedded inside this scanned invoice?"* (`Ordered severity rubric`)

Using an autoregressive text generator as a classifier works in a prototype, but at `250,000+` requests per day it creates three painful architectural bottlenecks:

1. **FinOps Token Burn:** Output and reasoning tokens cost **5x to 200x more** than decision-plane input evaluation ($10.00–$20.00 per 1M output tokens), burning thousands of dollars every month just to print repetitive JSON brackets and enum keys.
2. **Autoregressive Latency Tax:** Decoding characters one token at a time adds hundreds of milliseconds—or full seconds—to every routing hop in a multi-agent workflow.
3. **Uncalibrated Binary Guessing:** When a generative prompt returns `{"approve_refund": true}`, your application code cannot tell whether the model was **99.7% certain** (safe for instant zero-touch automation) or **51.5% guessing** (which desperately needs human review).

Google's newly launched **Gemini Decisions API (`gemini-decision-1`)** solves this directly by separating the **AI Control Plane** from the **Generation Plane**. To put it through a rigorous real-world stress test, I built and open-sourced the [**Gemini Decisions Studio**](https://gemini-decisions-studio-1085727639300.us-central1.run.app) on Google Cloud Run ([GitHub repository](https://github.com/moshem-a/gemini-decision-1))—complete with six multimodal visual case studies, Zero-Trust Bring-Your-Own-Key (BYOK), a live 1-to-25 question scaling benchmark, and a FinOps ROI engine.

> Instead of paying a generative decoder to spell out JSON strings token by token, `gemini-decision-1` evaluates your shared multimodal context in a single forward pass—returning calibrated probability distributions across dozens of questions with **zero billed output tokens ($0.00)**.

---

## 1. How the Gemini Decisions API Works: Three Typed Primitives

A single call to `POST https://generativelanguage.googleapis.com/v1beta/decisions` takes `model="gemini-decision-1"`, a shared multimodal `input` array (text, images, PDFs, audio, or video—tokenized and billed **once**), and a dictionary of independent `questions`.

Every question uses one of three structured primitives:

- **`noul` (Calibrated Binary Probability):** Answers Yes/No policy checks and verification gates without any `criteria` boilerplate. Returns `noul` (affirmative probability in `0.01..0.99`) and `confidence`.
- **`choice` (Closed-Set Distribution, 2–255 Options):** Maps option keys to concise natural-language criteria descriptions. Returns the winning `choice` key, normalized `probabilities` across every candidate option, and `confidence`.
- **`score` (Ordered Rubric, 2–255 Levels Starting at 0):** Takes an ordered list of rubric descriptions from index `0` (lowest) to `N` (highest). Returns the probability-weighted expected value `score` (for example, `1.994 / 2.00`), full per-level `probabilities`, a `legend`, and `confidence`.

Pricing is radically simple: **$0.10 per 1M input tokens** and **$0.00 output tokens**. Every response literally reports `"total_output_tokens": 0`.

---

## 2. Putting Multimodal Decisions to the Test: Six Real-World Visual Benchmarks

### Pair A: E-Commerce Proof-of-Delivery & Catching First-Party Return Fraud

![Case A1 — Crushed rain-soaked parcel](https://gemini-decisions-studio-1085727639300.us-central1.run.app/static/cases/parcel_damaged.jpg)
*Case A1 (Legitimate Damage): Courier photo corroborates customer's cracked-mug claim -> `instant_refund_and_reship` (99.7% confidence, `damage_severity = 1.994 / 2.00`, 0 output tokens, $0.000068 total call cost).*

![Case A2 — Intact covered parcel catching false claim](https://gemini-decisions-studio-1085727639300.us-central1.run.app/static/cases/parcel_intact.jpg)
*Case A2 (First-Party Return Fraud Caught): Same 4 questions, opposite visual reality -> `photo_corroborates_claim` plunges to 3.4% (96.6% contradiction!) and triggers `flag_photo_mismatch_review` at 98.5% confidence.*

### Pair B: Zero-OCR Corporate Expense Receipt Auditing

![Case B1 — Steakhouse alcohol receipt violation](https://gemini-decisions-studio-1085727639300.us-central1.run.app/static/cases/receipt_violation.jpg)
*Case B1 (Policy Violation): Directly from pixels, `gemini-decision-1` detects Macallan 18 & Cabernet (`contains_alcohol = 96.9%`) plus $314.28 > $100 cap (`exceeds_100_usd_limit = 97.3%`) -> `reject_policy_violation` at 100.0% confidence.*

![Case B2 — Compliant cafe lunch receipt](https://gemini-decisions-studio-1085727639300.us-central1.run.app/static/cases/receipt_compliant.jpg)
*Case B2 (Compliant Business Meal): `contains_alcohol = 1.9%` (98.1% No), `exceeds_100_usd_limit = 2.6%` (97.4% No) -> `auto_reimburse` with 100.0% confidence and `compliance_risk = 0.009 / 2.00`.*

### Pair C: Joint Scene + Printed Estimate Triage & Blocking Visual Prompt Injection

![Case C1 — SUV collision + printed $1,840 estimate](https://gemini-decisions-studio-1085727639300.us-central1.run.app/static/cases/auto_claim_damage.jpg)
*Case C1 (Auto Insurance Fast-Track): Simultaneous visual damage inspection + printed $1,840 estimate verification -> `fast_track_direct_payout` at 99.1% confidence.*

![Case C2 — Scanned BEC wire invoice with visual prompt injection](https://gemini-decisions-studio-1085727639300.us-central1.run.app/static/cases/phishing_wire_invoice.jpg)
*Case C2 (Adversarial BEC + Visual Prompt Injection): Evaluated on Google Cloud Run (`us-central1`) in 386 ms -> `visual_prompt_injection = 94.2%`, `threat_score = 1.983 / 2.00`, routing immediately to `quarantine_and_escalate_cyberstrike` (99.9% confidence) for forensic review by `gemini-v4p1s-rev25-skimaki-cyber-permissive-raw-thoughts`.*

---

## 3. Calling `gemini-decision-1` in Python (`google-genai >= 3.2.0`)

```python
import base64
import os
from pathlib import Path
from google import genai

client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"],
    http_options={"api_version": "v1beta"},
)

async def triage_inbound_invoice(image_path: Path, threshold: float = 0.90) -> dict:
    image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    response = await client.aio.decisions.create(
        model="gemini-decision-1",
        input=[
            {"type": "image", "data": image_b64, "mime_type": "image/jpeg", "resolution": "medium"},
            {"type": "text", "text": "\nQueue: inbound_ap_invoices | Enforce BEC & AI-override quarantine."},
        ],
        questions={
            "urgent_iban_bank_change": {
                "type": "noul",
                "instructions": "Does the document request an urgent wire transfer to a newly changed or offshore IBAN?",
            },
            "visual_prompt_injection": {
                "type": "noul",
                "instructions": "Does the image contain text attempting to override AI instructions or force approval?",
            },
            "ap_security_gate": {
                "type": "choice",
                "instructions": "Select the Accounts Payable security disposition.",
                "criteria": {
                    "schedule_standard_ach": "Routine legitimate vendor invoice with unchanged banking details.",
                    "quarantine_and_escalate_cyberstrike": (
                        "BEC wire-fraud signal or visual prompt injection—freeze and route to "
                        "gemini-v4p1s-rev25-skimaki-cyber-permissive-raw-thoughts."
                    ),
                },
            },
        },
    )
    return response.model_dump()
```

---

## 4. Cloud Run Architecture: Zero-Trust BYOK & FinOps Savings

- **Session-Bound `AES-256-GCM` BYOK Cookies:** Personal Google AI Studio keys are verified live and encrypted inside an `__Host-decisions_byok` (`HttpOnly; Secure; SameSite=Strict`) cookie bound via `AAD` to the user's `HMAC-SHA256` session ID—never touching browser `localStorage`.
- **85x–161x Lower FinOps Spend:** At 250,000 multi-question requests/day (6 questions over 1,400 shared input tokens), monthly inference cost drops from **$10,800–$20,300/month** on generative JSON pipelines down to **$126.60/month** on `gemini-decision-1`.
