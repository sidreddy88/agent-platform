"""
LLM Gateway — abstracts provider selection and model routing from agent code.

Agents call LLMGateway.complete(messages, task_type) and the gateway reads
config/llm_routing.json to select the right provider and model.  All calls
are logged (provider, model, task_type, tokens, cost, latency) and costs
are accumulated for the /metrics endpoint.

LiteLLM is used as the single universal adapter — it supports Anthropic,
OpenAI, and 100+ other providers through a unified API.  Adding a new
provider requires only a config change, no new provider class.

Usage inside IncidentLoop:
    gateway = LLMGateway()
    triage_agent = TriageAgent(llm=gateway.get_llm_service_for("triage"))
"""
from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG = Path(__file__).parent.parent.parent / "config" / "llm_routing.json"


# ---------------------------------------------------------------------------
# Public data model
# ---------------------------------------------------------------------------

@dataclass
class LLMResponse:
    content: str
    input_tokens: int
    output_tokens: int
    provider: str
    model: str
    cost_usd: float


# ---------------------------------------------------------------------------
# Provider abstractions
# ---------------------------------------------------------------------------

class BaseProvider(ABC):
    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> LLMResponse: ...


class LiteLLMProvider(BaseProvider):
    """Universal provider backed by LiteLLM — works with any supported model."""

    async def complete(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> LLMResponse:
        import litellm

        system: str | None = kwargs.pop("system", None)
        msgs = list(messages)
        if system:
            msgs = [{"role": "system", "content": system}] + msgs

        response = await litellm.acompletion(
            model=model,
            messages=msgs,
            max_tokens=max_tokens,
        )
        usage = response.usage
        provider = _infer_provider(model)
        return LLMResponse(
            content=response.choices[0].message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            provider=provider,
            model=model,
            cost_usd=0.0,
        )


def _infer_provider(model: str) -> str:
    """Best-effort provider label for logging/cost tracking."""
    m = model.lower()
    if "/" in m:
        return m.split("/")[0]
    if "claude" in m:
        return "anthropic"
    if "gpt" in m or "o1" in m or "o3" in m:
        return "openai"
    return "unknown"


# ---------------------------------------------------------------------------
# GatewayLLMService — drop-in for LLMService, routes through the gateway
# ---------------------------------------------------------------------------

class GatewayLLMService:
    """
    Duck-typed replacement for LLMService.  Pass as llm= to any BaseAgent
    subclass; the ReAct loop calls complete() unchanged and all calls are
    routed through LLMGateway for config-driven model selection and logging.
    """

    def __init__(self, gateway: LLMGateway, task_type: str) -> None:
        self._gateway = gateway
        self._task_type = task_type
        self.last_input_tokens: int = 0
        self.last_output_tokens: int = 0

    async def complete(
        self,
        messages: list[dict],
        system: str | None = None,
        tracing_ctx: Any = None,
    ) -> str:
        resp = await self._gateway.complete(messages, self._task_type, system=system)
        self.last_input_tokens = resp.input_tokens
        self.last_output_tokens = resp.output_tokens
        return resp.content


# ---------------------------------------------------------------------------
# LLMGateway
# ---------------------------------------------------------------------------

class LLMGateway:
    def __init__(self, config_path: str | Path = _DEFAULT_CONFIG) -> None:
        self._config = self._load_config(Path(config_path))
        self._provider = LiteLLMProvider()
        self._daily_costs: dict[str, dict[str, float]] = {}
        self._validate_fix_review_providers()

    def _validate_fix_review_providers(self) -> None:
        """Enforce that fix and review always use different LLM providers."""
        _, fix_model = self._get_routing("fix")
        _, review_model = self._get_routing("review")
        fix_provider = _infer_provider(fix_model)
        review_provider = _infer_provider(review_model)
        if fix_provider == review_provider and fix_provider != "unknown":
            raise ValueError(
                f"fix and review must use different providers, but both resolved to "
                f"'{fix_provider}' (fix={fix_model}, review={review_model}). "
                "Update config/llm_routing.json to use opposite providers."
            )

    @staticmethod
    def _load_config(path: Path) -> dict:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            logger.warning("[gateway] Config not found at %s — using defaults", path)
            return {}

    def _get_routing(self, task_type: str) -> tuple[str, str]:
        entry = self._config.get("routing", {}).get(task_type, {})
        provider = entry.get("provider") or _infer_provider(entry.get("model", ""))
        return provider, entry.get("model", "claude-sonnet-4-6")

    def _compute_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        rates = self._config.get("cost_per_1k_tokens", {}).get(model, {})
        return (input_tokens * rates.get("input", 0.0) + output_tokens * rates.get("output", 0.0)) / 1000

    def _record_cost(self, task_type: str, provider: str, cost: float) -> None:
        today = date.today().isoformat()
        day = self._daily_costs.setdefault(today, {})
        day["_total"] = day.get("_total", 0.0) + cost
        day[f"task:{task_type}"] = day.get(f"task:{task_type}", 0.0) + cost
        day[f"provider:{provider}"] = day.get(f"provider:{provider}", 0.0) + cost

    async def _call_provider(
        self,
        provider: BaseProvider,
        messages: list[dict],
        model: str,
        task_type: str,
        system: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Call provider, wrapped in a Langfuse generation span when tracing is active."""
        from app.services.tracing import _get_client  # lazy import to avoid circular dep
        lf = _get_client()

        if lf is None:
            return await provider.complete(messages, model, system=system, **kwargs)

        provider_error: BaseException | None = None
        result: LLMResponse | None = None

        try:
            with lf.start_as_current_observation(
                name=f"llm/{task_type}",
                as_type="generation",
                model=model,
                input={"system": system or "", "messages": messages[-3:]},
            ) as gen:
                try:
                    result = await provider.complete(messages, model, system=system, **kwargs)
                    try:
                        gen.update(
                            output=result.content[:500],
                            usage={"input": result.input_tokens, "output": result.output_tokens},
                        )
                    except Exception:
                        pass
                except Exception as exc:
                    provider_error = exc
                    try:
                        gen.update(level="ERROR", status_message=str(exc))
                    except Exception:
                        pass
        except Exception:
            # Langfuse span setup/teardown failed — run without tracing
            if provider_error is None and result is None:
                return await provider.complete(messages, model, system=system, **kwargs)

        if provider_error is not None:
            raise provider_error  # type: ignore[misc]

        assert result is not None
        return result

    async def complete(
        self,
        messages: list[dict],
        task_type: str,
        system: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        provider_name, model = self._get_routing(task_type)

        start = time.perf_counter()
        raw = await self._call_provider(self._provider, messages, model, task_type, system=system, **kwargs)
        latency_ms = (time.perf_counter() - start) * 1000

        cost = self._compute_cost(model, raw.input_tokens, raw.output_tokens)
        resp = LLMResponse(
            content=raw.content,
            input_tokens=raw.input_tokens,
            output_tokens=raw.output_tokens,
            provider=provider_name,
            model=model,
            cost_usd=cost,
        )

        self._record_cost(task_type, provider_name, cost)
        logger.info(
            "[gateway] task=%s provider=%s model=%s in=%d out=%d cost=%.6f latency=%.0fms",
            task_type, provider_name, model,
            resp.input_tokens, resp.output_tokens, cost, latency_ms,
        )
        return resp

    async def complete_with_fallback(
        self,
        messages: list[dict],
        task_type: str,
        confidence_threshold: float = 0.70,
        system: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        resp = await self.complete(messages, task_type, system=system, **kwargs)

        confidence = self._extract_confidence(resp.content)
        if confidence is None or confidence >= confidence_threshold:
            return resp

        routing = self._config.get("routing", {}).get(task_type, {})
        fallback_model = routing.get("fallback_model")
        if not fallback_model:
            return resp

        provider_name = routing.get("provider") or _infer_provider(fallback_model)
        fallback_task = f"{task_type}_fallback"

        start = time.perf_counter()
        raw = await self._call_provider(self._provider, messages, fallback_model, fallback_task, system=system, **kwargs)
        latency_ms = (time.perf_counter() - start) * 1000

        cost = self._compute_cost(fallback_model, raw.input_tokens, raw.output_tokens)
        self._record_cost(fallback_task, provider_name, cost)
        logger.info(
            "[gateway] fallback task=%s model=%s in=%d out=%d cost=%.6f latency=%.0fms",
            task_type, fallback_model, raw.input_tokens, raw.output_tokens, cost, latency_ms,
        )
        return LLMResponse(
            content=raw.content,
            input_tokens=raw.input_tokens,
            output_tokens=raw.output_tokens,
            provider=provider_name,
            model=fallback_model,
            cost_usd=cost,
        )

    @staticmethod
    def _extract_confidence(content: str) -> float | None:
        import re
        m = re.search(r'"confidence"\s*:\s*([0-9]*\.?[0-9]+)', content)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
        return None

    def costs_today(self) -> dict[str, Any]:
        today = date.today().isoformat()
        day = self._daily_costs.get(today, {})
        total = day.get("_total", 0.0)
        by_task = {k[5:]: round(v, 6) for k, v in day.items() if k.startswith("task:")}
        by_provider = {k[9:]: round(v, 6) for k, v in day.items() if k.startswith("provider:")}
        return {
            "llm_costs_today_usd": round(total, 6),
            "cost_by_task_type": by_task,
            "cost_by_provider": by_provider,
        }

    def get_llm_service_for(self, task_type: str) -> GatewayLLMService:
        return GatewayLLMService(self, task_type)


# Module-level singleton — shared across all agents in a process
llm_gateway = LLMGateway()
