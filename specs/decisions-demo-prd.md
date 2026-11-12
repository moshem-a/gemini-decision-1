# PRD: Gemini Decisions API — Conference Keynote Studio

## 1. Objective
Deliver a live, stage-ready interactive demonstration for a **Google Solution AI Architect** keynote showcasing the **Gemini Decisions API** (`gemini-decision-1`), highlighting:
1. **Calibrated Multimodal Primitives (`noul`, `choice`, `score`)** evaluated in a single forward pass across text, images, audio, PDFs, and structured JSON.
2. **Flat Single-Pass Latency (`~150–250 ms`)** from 1 to 100+ independent questions without token-by-token autoregressive decoding.
3. **Breakthrough FinOps Economics**: `$0.10` per 1M input tokens (shared input billed once per request) and **`$0.00` output / thinking token cost** (`total_output_tokens = 0`), reducing control-plane inference spend by **90%–98%** compared to full generative Gemini 4 calls.

## 2. Target User Journeys
- **Journey A (Multimodal Live Triage)**: Select any of 6 enterprise scenarios (Dynamic Gemini 4 Model Router, Smart Facility Vision, Zero-STT Voice Triage, CyberStrike MCP Firewall, VIP Claims Triage, 12-Rule Policy Matrix) or upload live media, adjust visual resolution (`low`/`medium`/`high`), and inspect calibrated probabilities + confidence thresholds in real time.
- **Journey B (Single-Pass Scaling Proof)**: Execute live `1 -> 5 -> 12 -> 25` question scaling benchmarks against `gemini-decision-1` on stage to demonstrate near-flat latency and zero output tokens.
- **Journey C (Executive TCO & FinOps Modeling)**: Adjust traffic volume, context tokens, number of checks, and hybrid escalation thresholds ($\tau$) to project monthly and annual savings vs. `gemini-4-argon` and `gemini-4-carbon`.

## 3. Latency & Cost SLAs
- **Model**: `gemini-decision-1` via `google-genai >= 3.2.0` (`client.aio.decisions.create`, `v1beta`).
- **CyberStrike Escalation Model**: `gemini-v4p1s-rev25-skimaki-cyber-permissive-raw-thoughts` when adversarial predicates trigger or confidence falls below security policy bounds.
- **Target Model Compute Latency**: 150–350 ms p50 end-to-end network round-trip; `total_output_tokens == 0`.
