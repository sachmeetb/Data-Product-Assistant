"""Shared LLM token-usage accounting.

A single place to (1) normalize the token usage carried on the SDK's
``ResultMessage``, (2) sum usage across multi-pass flows, and (3) persist a
:class:`LlmUsageEvent` ledger row. Every SDK call site funnels its usage through
here so global / per-data-product / per-Semantic-Q&A rollups all derive from one
source of truth.

Design rules:
- **Best-effort**: ``record_usage`` never raises into the caller. Token
  accounting must not break an LLM feature.
- **Token-first**: ``total_cost_usd`` may be ``None`` (e.g. when routed through
  Azure Foundry); token counts from ``message.usage`` stay reliable, so the
  ledger is keyed on tokens with cost as a bonus.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlmodel import Session

from . import telemetry
from .database import engine
from .models import LlmUsageEvent

_log = logging.getLogger(__name__)

# Anthropic usage keys → our normalized fields. cache_creation/cache_read are the
# prompt-cache write/read token counts.
_INPUT_KEYS = ("input_tokens",)
_OUTPUT_KEYS = ("output_tokens",)
_CACHE_CREATION_KEYS = ("cache_creation_input_tokens", "cache_creation_tokens")
_CACHE_READ_KEYS = ("cache_read_input_tokens", "cache_read_tokens")


def _first_int(d: dict, keys: tuple[str, ...]) -> int:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def empty_usage() -> dict[str, Any]:
    return {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "total_tokens": 0, "cost_usd": None, "model": None,
    }


def extract_usage(message: Any) -> dict[str, Any]:
    """Normalize the usage carried on an SDK ``ResultMessage`` into a flat dict.

    Reads ``message.usage`` (a dict) defensively and ``message.total_cost_usd``.
    Returns :func:`empty_usage` shape; never raises."""
    u = empty_usage()
    try:
        raw = getattr(message, "usage", None) or {}
        if isinstance(raw, dict):
            u["input_tokens"] = _first_int(raw, _INPUT_KEYS)
            u["output_tokens"] = _first_int(raw, _OUTPUT_KEYS)
            u["cache_creation_tokens"] = _first_int(raw, _CACHE_CREATION_KEYS)
            u["cache_read_tokens"] = _first_int(raw, _CACHE_READ_KEYS)
        # Headline "tokens" = the working set (uncached input + output). Anthropic
        # reports cached prompt portions separately (cache_read/cache_creation),
        # which for skill-driven calls are large + near-constant per pass — folding
        # them into the total would swamp the figure and make the Full-vs-Concept
        # comparison meaningless. They stay in their own fields for cost context.
        u["total_tokens"] = u["input_tokens"] + u["output_tokens"]
        cost = getattr(message, "total_cost_usd", None)
        u["cost_usd"] = float(cost) if isinstance(cost, (int, float)) else None
        # Pick a model name when the SDK exposes per-model usage.
        model_usage = getattr(message, "model_usage", None)
        if isinstance(model_usage, dict) and model_usage:
            u["model"] = next(iter(model_usage.keys()), None)
    except Exception:  # noqa: BLE001 — accounting must never raise
        return empty_usage()
    return u


class UsageAccumulator:
    """Sum usage across multiple SDK passes (e.g. the 3-pass Semantic Q&A flow)."""

    def __init__(self) -> None:
        self.total = empty_usage()
        self.passes = 0

    def add(self, usage: Optional[dict[str, Any]]) -> None:
        if not usage:
            return
        self.passes += 1
        for k in ("input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_creation_tokens", "total_tokens"):
            self.total[k] += int(usage.get(k) or 0)
        c = usage.get("cost_usd")
        if isinstance(c, (int, float)):
            self.total["cost_usd"] = (self.total["cost_usd"] or 0.0) + float(c)
        if usage.get("model") and not self.total.get("model"):
            self.total["model"] = usage["model"]

    def as_dict(self) -> dict[str, Any]:
        out = dict(self.total)
        out["passes"] = self.passes
        return out


def record_usage(
    *,
    source: str,
    usage: Optional[dict[str, Any]],
    project_code: Optional[str] = None,
    contract_id: Optional[str] = None,
    domain: Optional[str] = None,
    run_id: Optional[str] = None,
    retrieval_mode: Optional[str] = None,
    session: Optional[Session] = None,
) -> None:
    """Insert one :class:`LlmUsageEvent` ledger row. Best-effort — swallows all
    errors (logs at debug). Skips no-op rows (zero tokens AND no cost). When
    ``session`` is given it is reused (caller commits or it is committed here);
    otherwise a short-lived session is opened."""
    if not usage:
        return
    total = int(usage.get("total_tokens") or 0)
    cost = usage.get("cost_usd")
    if total <= 0 and not cost:
        return  # nothing worth recording (e.g. SDK missing / errored pass)
    # Mirror the same usage into OTel metrics (best-effort; no-op when telemetry
    # is off) so token/cost land in the configured collector alongside the CLI's
    # own native counters.
    telemetry.record_usage_metrics(source=source, usage=usage, project_code=project_code)
    row = LlmUsageEvent(
        source=source,
        project_code=project_code,
        contract_id=contract_id,
        domain=domain,
        run_id=run_id,
        retrieval_mode=retrieval_mode,
        model=usage.get("model"),
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_tokens") or 0),
        cache_creation_tokens=int(usage.get("cache_creation_tokens") or 0),
        total_tokens=total,
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
    )
    try:
        if session is not None:
            session.add(row)
            session.commit()
        else:
            with Session(engine) as s:
                s.add(row)
                s.commit()
    except Exception as e:  # noqa: BLE001
        _log.debug("record_usage failed (ignored): %s", e)
