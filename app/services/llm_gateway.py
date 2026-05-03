"""
LLM Gateway — abstracts provider selection and model routing from agent code.

Agents call LLMGateway.complete(messages, task_type) and the gateway reads
config/llm_routing.json to select the right provider and model.  All calls
are logged (provider, model, task_type, tokens, cost, latency) and costs
are accumulated for the /metrics endpoint.

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

import anthropic

from app.core.config import settings
from app.services.circuit_breaker import circuit_breaker_registry

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


class AnthropicProvider(BaseProvider):
    def __init__(self) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def complete(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> LLMResponse:
        system: str | None = kwargs.pop("system", None)
        call_kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            call_kwargs["system"] = system

        async def _call() -> LLMResponse:
            response = await self._client.messages.create(**call_kwargs)
            return LLMResponse(
                content=response.content[0].text,
                input_tokens=response.usage.input_tokens if response.usage else 0,
                output_tokens=response.usage.output_tokens if response.usage else 0,
                provider="anthropic",
                model=model,
                cost_usd=0.0,
            )

        cb = circuit_breaker_registry.get_or_create(
            "anthropic_llm", failure_threshold=5, timeout_seconds=60.0
        )
        return await cb.call(_call())


class OpenAIProvider(BaseProvider):
    def __init__(self) -> None:
        try:
            import openai
            self._client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
        except ImportError as exc:
            raise ValueError("openai package is not installed") from exc

    async def complete(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int = 4096,
        **kwargs: Any,
    ) -> LLMResponse:
        system: str | None = kwargs.pop("system", None)
        msgs = list(messages)
        if system:
            msgs = [{"role": "system", "content": system}] + msgs

        response = await self._client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=msgs,
        )
        usage = response.usage
        return LLMResponse(
            content=response.choices[0].message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            provider="openai",
            model=model,
            cost_usd=0.0,
        )


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
        self._providers: dict[str, BaseProvider] = {
            "anthropic": AnthropicProvider(),
        }
        if settings.openai_api_key:
            try:
                self._providers["openai"] = OpenAIProvider()
            except Exception as exc:
                logger.warning("[gateway] OpenAI provider unavailable: %s", exc)

        # {date_iso: {"_total": float, "task:<n>": float, "provider:<n>": float}}
        self._daily_costs: dict[str, dict[str, float]] = {}

    @staticmethod
    def _load_config(path: Path) -> dict:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            logger.warning("[gateway] Config not found at %s — using defaults", path)
            return {}

    def _get_routing(self, task_type: str) -> tuple[str, str]:
        entry = self._config.get("routing", {}).get(task_type, {})
        return entry.get("provider", "anthropic"), entry.get("model", "claude-sonnet-4-6")

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
        provider = self._providers.get(provider_name) or self._providers["anthropic"]

        start = time.perf_counter()
        raw = await self._call_provider(provider, messages, model, task_type, system=system, **kwargs)
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

        provider_name = routing.get("provider", "anthropic")
        provider = self._providers.get(provider_name) or self._providers["anthropic"]
        fallback_task = f"{task_type}_fallback"

        start = time.perf_counter()
        raw = await self._call_provider(provider, messages, fallback_model, fallback_task, system=system, **kwargs)
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
