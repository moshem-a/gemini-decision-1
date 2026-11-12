"""FinOps & TCO Cost-Reduction Calculator for the Gemini Decisions API (`gemini-decision-1`).

Grounded in official Google AI Studio / Gemini Developer API pricing (November 2026):
- `gemini-decision-1`: $0.10 / 1M input tokens | $0.00 / 1M output tokens (0 output tokens, 0 thinking tokens)
- `gemini-4-argon` (Launch Promo): $2.00 / 1M input | $10.00 / 1M output (incl. thinking tokens)
- `gemini-4-argon` (Standard GA):  $4.00 / 1M input | $20.00 / 1M output (incl. thinking tokens)
- `gemini-4-carbon` (Standard GA): $4.00 / 1M input | $20.00 / 1M output (incl. deep thinking tokens)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DECISION_INPUT_USD_PER_1M = 0.10
DECISION_OUTPUT_USD_PER_1M = 0.00


@dataclass(frozen=True)
class ModelPricingTier:
    tier_id: str
    label: str
    input_usd_per_1m: float
    output_usd_per_1m: float
    avg_latency_ms_base: int
    avg_latency_ms_per_question: int
    thinking_multiplier: float


PRICING_TIERS: dict[str, ModelPricingTier] = {
    "gemini_4_argon_promo": ModelPricingTier(
        tier_id="gemini_4_argon_promo",
        label="Gemini 4 Argon (Promo: $2 In / $10 Out per 1M)",
        input_usd_per_1m=2.00,
        output_usd_per_1m=10.00,
        avg_latency_ms_base=950,
        avg_latency_ms_per_question=110,
        thinking_multiplier=1.0,
    ),
    "gemini_4_argon_ga": ModelPricingTier(
        tier_id="gemini_4_argon_ga",
        label="Gemini 4 Argon (Standard GA: $4 In / $20 Out per 1M)",
        input_usd_per_1m=4.00,
        output_usd_per_1m=20.00,
        avg_latency_ms_base=950,
        avg_latency_ms_per_question=110,
        thinking_multiplier=1.0,
    ),
    "gemini_4_carbon_ga": ModelPricingTier(
        tier_id="gemini_4_carbon_ga",
        label="Gemini 4 Carbon (Deep Reasoning GA: $4 In / $20 Out per 1M)",
        input_usd_per_1m=4.00,
        output_usd_per_1m=20.00,
        avg_latency_ms_base=1650,
        avg_latency_ms_per_question=165,
        thinking_multiplier=1.6,
    ),
}


def calculate_finops_roi(
    *,
    daily_requests: int = 250_000,
    shared_input_tokens: int = 1_400,
    questions_per_request: int = 6,
    tokens_per_question_spec: int = 48,
    generative_output_tokens_per_question: int = 95,
    baseline_tier_id: str = "gemini_4_argon_promo",
    escalation_rate_pct: float = 8.0,
    separate_calls_anti_pattern: bool = False,
) -> dict[str, Any]:
    """Compute exact per-request, monthly, and annual USD cost & latency savings."""
    tier = PRICING_TIERS.get(baseline_tier_id, PRICING_TIERS["gemini_4_argon_promo"])

    monthly_requests = daily_requests * 30
    question_spec_tokens_total = questions_per_request * tokens_per_question_spec

    # 1. Pure Gemini Decisions API (`gemini-decision-1`)
    # Shared input is billed ONCE per request + lightweight criteria tokens; output is ALWAYS 0!
    decision_input_tokens_per_req = shared_input_tokens + question_spec_tokens_total
    decision_cost_per_req = (decision_input_tokens_per_req / 1_000_000.0) * DECISION_INPUT_USD_PER_1M
    decision_monthly_usd = decision_cost_per_req * monthly_requests
    decision_latency_ms = 175 + int(questions_per_request * 1.4)

    # 2. Generative LLM Baseline (Structured Output + Thinking Tokens)
    if separate_calls_anti_pattern:
        # Microservice anti-pattern: N separate LLM calls re-billing shared context N times
        gen_input_tokens_per_req = (shared_input_tokens * questions_per_request) + question_spec_tokens_total
    else:
        # Batched structured JSON schema call
        gen_input_tokens_per_req = shared_input_tokens + question_spec_tokens_total + 120

    gen_output_tokens_per_req = int(
        questions_per_request * generative_output_tokens_per_question * tier.thinking_multiplier
    )
    gen_input_cost_per_req = (gen_input_tokens_per_req / 1_000_000.0) * tier.input_usd_per_1m
    gen_output_cost_per_req = (gen_output_tokens_per_req / 1_000_000.0) * tier.output_usd_per_1m
    gen_cost_per_req = gen_input_cost_per_req + gen_output_cost_per_req
    gen_monthly_usd = gen_cost_per_req * monthly_requests
    gen_latency_ms = tier.avg_latency_ms_base + (questions_per_request * tier.avg_latency_ms_per_question)

    # 3. Hybrid Smart Control Plane (100% screened by Decision 1 + `escalation_rate_pct`% escalated to Generative)
    esc_fraction = max(0.0, min(1.0, escalation_rate_pct / 100.0))
    hybrid_cost_per_req = decision_cost_per_req + (esc_fraction * gen_cost_per_req)
    hybrid_monthly_usd = hybrid_cost_per_req * monthly_requests
    hybrid_latency_ms = int(decision_latency_ms + (esc_fraction * gen_latency_ms))

    # Savings metrics (Pure Decision 1 vs Generative Baseline)
    monthly_savings_pure_usd = max(0.0, gen_monthly_usd - decision_monthly_usd)
    annual_savings_pure_usd = monthly_savings_pure_usd * 12.0
    savings_pure_pct = (monthly_savings_pure_usd / gen_monthly_usd * 100.0) if gen_monthly_usd > 0 else 0.0
    cost_multiplier = (gen_monthly_usd / decision_monthly_usd) if decision_monthly_usd > 0 else 1.0

    # Savings metrics (Hybrid Control Plane vs Generative Baseline)
    monthly_savings_hybrid_usd = max(0.0, gen_monthly_usd - hybrid_monthly_usd)
    annual_savings_hybrid_usd = monthly_savings_hybrid_usd * 12.0
    savings_hybrid_pct = (monthly_savings_hybrid_usd / gen_monthly_usd * 100.0) if gen_monthly_usd > 0 else 0.0

    # Compute time & output tokens eliminated per month
    monthly_output_tokens_eliminated = gen_output_tokens_per_req * monthly_requests
    latency_saved_hours_per_month = (
        max(0, gen_latency_ms - decision_latency_ms) * monthly_requests
    ) / (1000.0 * 3600.0)

    return {
        "inputs": {
            "daily_requests": daily_requests,
            "monthly_requests": monthly_requests,
            "shared_input_tokens": shared_input_tokens,
            "questions_per_request": questions_per_request,
            "tokens_per_question_spec": tokens_per_question_spec,
            "generative_output_tokens_per_question": generative_output_tokens_per_question,
            "baseline_tier_id": tier.tier_id,
            "baseline_label": tier.label,
            "escalation_rate_pct": round(escalation_rate_pct, 1),
            "separate_calls_anti_pattern": separate_calls_anti_pattern,
        },
        "decision_api": {
            "model": "gemini-decision-1",
            "input_tokens_per_req": decision_input_tokens_per_req,
            "output_tokens_per_req": 0,
            "cost_per_req_usd": round(decision_cost_per_req, 7),
            "decisions_per_dollar": int(1.0 / decision_cost_per_req) if decision_cost_per_req > 0 else 0,
            "monthly_cost_usd": round(decision_monthly_usd, 2),
            "annual_cost_usd": round(decision_monthly_usd * 12.0, 2),
            "avg_latency_ms": decision_latency_ms,
        },
        "generative_baseline": {
            "model_label": tier.label,
            "input_tokens_per_req": gen_input_tokens_per_req,
            "output_tokens_per_req": gen_output_tokens_per_req,
            "input_cost_share_pct": round((gen_input_cost_per_req / gen_cost_per_req * 100.0), 1)
            if gen_cost_per_req > 0
            else 0.0,
            "output_cost_share_pct": round((gen_output_cost_per_req / gen_cost_per_req * 100.0), 1)
            if gen_cost_per_req > 0
            else 0.0,
            "cost_per_req_usd": round(gen_cost_per_req, 7),
            "monthly_cost_usd": round(gen_monthly_usd, 2),
            "annual_cost_usd": round(gen_monthly_usd * 12.0, 2),
            "avg_latency_ms": gen_latency_ms,
        },
        "hybrid_cascade": {
            "description": f"100% Decision 1 Triage + {round(escalation_rate_pct, 1)}% Escalated to Frontier Tier",
            "cost_per_req_usd": round(hybrid_cost_per_req, 7),
            "monthly_cost_usd": round(hybrid_monthly_usd, 2),
            "annual_cost_usd": round(hybrid_monthly_usd * 12.0, 2),
            "avg_latency_ms": hybrid_latency_ms,
            "monthly_savings_usd": round(monthly_savings_hybrid_usd, 2),
            "annual_savings_usd": round(annual_savings_hybrid_usd, 2),
            "savings_pct": round(savings_hybrid_pct, 1),
        },
        "headline_savings": {
            "monthly_savings_usd": round(monthly_savings_pure_usd, 2),
            "annual_savings_usd": round(annual_savings_pure_usd, 2),
            "savings_pct": round(savings_pure_pct, 1),
            "cheaper_multiplier": round(cost_multiplier, 1),
            "speedup_multiplier": round(gen_latency_ms / max(1, decision_latency_ms), 1),
            "monthly_output_tokens_eliminated": monthly_output_tokens_eliminated,
            "latency_saved_hours_per_month": round(latency_saved_hours_per_month, 1),
        },
    }
