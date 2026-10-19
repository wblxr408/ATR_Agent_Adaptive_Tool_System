"""Offline observation contracts and paired reporting; this module runs no experiments."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any, Literal

from pydantic import Field

from atr.errors import ATRError
from atr.models import Contract


class Observation(Contract):
    task_id: str = Field(min_length=1)
    repetition: str = "1"
    domain: Literal["coding", "research"]
    arm: Literal["baseline", "atr"]
    phase: Literal["cold", "hot", "workflow", "complex_research"]
    model: str = Field(min_length=1)
    input_identity: str = Field(min_length=1)
    permissions_identity: str = Field(min_length=1)
    prompt_token_budget: int = Field(gt=0)
    task_budget_usd: float = Field(ge=0, allow_inf_nan=False)
    success: bool
    quality: float = Field(ge=0, le=1, allow_inf_nan=False)
    quality_evidence: str = Field(min_length=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    billed_tokens: int | None = Field(default=None, ge=0)
    total_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    duration_ms: int = Field(ge=0)
    created_tools: int = Field(default=0, ge=0)
    lookups: int = Field(default=0, ge=0)
    retrieval_hits: int = Field(default=0, ge=0)
    router_model_calls: int = Field(default=0, ge=0)
    unauthorized_effects: int = Field(default=0, ge=0)
    external_data_note: str | None = None


def compare(observations: list[Observation]) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str], dict[str, Observation]] = defaultdict(dict)
    for row in observations:
        key = (row.task_id, row.repetition, row.phase)
        if row.arm in grouped[key]:
            raise ATRError("duplicate_observation", "duplicate arm for task, repetition and phase")
        grouped[key][row.arm] = row
    pairs: list[tuple[Observation, Observation]] = []
    unmatched = 0
    for arms in grouped.values():
        if set(arms) != {"baseline", "atr"}:
            unmatched += 1
            continue
        base, atr = arms["baseline"], arms["atr"]
        for field in ("model", "input_identity", "permissions_identity", "prompt_token_budget",
                    "task_budget_usd", "domain"):
            if getattr(base, field) != getattr(atr, field):
                raise ATRError("unfair_pair", f"paired observations differ in {field}")
        pairs.append((base, atr))
    if not pairs:
        return {"status": "insufficient_data", "pairs": 0, "unmatched": unmatched,
                "net_benefit_demonstrated": False}
    phases: dict[str, Any] = {}
    for phase in sorted({base.phase for base, _ in pairs}):
        selected = [(base, atr) for base, atr in pairs if base.phase == phase]
        savings = [(b.total_cost_usd - a.total_cost_usd) / b.total_cost_usd
                   for b, a in selected if b.total_cost_usd is not None and b.total_cost_usd > 0
                   and a.total_cost_usd is not None]
        token_savings = [(b.billed_tokens - a.billed_tokens) / b.billed_tokens
                         for b, a in selected if b.billed_tokens and a.billed_tokens is not None]
        quality_preserved = all(a.success >= b.success and a.quality >= b.quality
                                and a.unauthorized_effects == 0 for b, a in selected)
        phases[phase] = {"pairs": len(selected), "quality_preserved": quality_preserved,
            "median_cost_saving_fraction": statistics.median(savings) if savings else None,
            "median_billed_token_saving_fraction": statistics.median(token_savings) if token_savings else None,
            "billing_complete": len(savings) == len(selected),
            "atr_success_rate": statistics.mean(a.success for _, a in selected),
            "baseline_success_rate": statistics.mean(b.success for b, _ in selected),
            "atr_median_ms": statistics.median(a.duration_ms for _, a in selected)}
    sufficient = all(v["quality_preserved"] and v["billing_complete"] for v in phases.values())
    return {"status": "reported", "pairs": len(pairs), "unmatched": unmatched, "phases": phases,
            "net_benefit_demonstrated": sufficient and all(
                v["median_cost_saving_fraction"] is not None and v["median_cost_saving_fraction"] > 0
                for v in phases.values()),
            "interpretation": "observed paired results only; no statistical or future-performance guarantee"}
