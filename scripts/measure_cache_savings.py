"""
Measures prompt caching savings by replaying the diagnosis system prompt
against a fixed set of messages and comparing cached vs uncached token counts.

Usage:
    python scripts/measure_cache_savings.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

RESET = "\033[0m"
CYAN  = "\033[36m"
GREEN = "\033[32m"
BOLD  = "\033[1m"
GREY  = "\033[90m"

# Simulate 6 ReAct loop iterations with a minimal user message
# (represents one diagnosis run — each iteration re-sends the system prompt)
ITERATIONS = 6
USER_MSG = "Diagnose this error: S3 NoSuchKey on copyObject in moveAndRemoveFileFromS3"


async def measure(with_cache: bool) -> dict:
    from dotenv import load_dotenv
    load_dotenv()

    from app.agents.base import BaseAgent
    from app.services.llm_gateway import LLMGateway

    gateway = LLMGateway()
    agent = BaseAgent.__new__(BaseAgent)
    agent._harness_docs = agent._load_harness_docs()

    system_str = "You are a senior engineer diagnosing production incidents."
    system = agent._with_harness(system_str) if with_cache else f"{agent._harness_docs}\n\n---\n\n{system_str}"

    total_input = 0
    total_cache_read = 0
    total_cache_write = 0
    total_cost = 0.0
    latencies = []

    messages = [{"role": "user", "content": USER_MSG}]

    for i in range(ITERATIONS):
        start = time.perf_counter()
        resp = await gateway.complete(messages, "diagnosis", system=system)
        latency = (time.perf_counter() - start) * 1000

        total_input      += resp.input_tokens
        total_cache_read += resp.cache_read_input_tokens
        total_cache_write += resp.cache_creation_input_tokens
        total_cost       += resp.cost_usd
        latencies.append(latency)

        # Grow conversation (simulate ReAct loop)
        messages.append({"role": "assistant", "content": resp.content[:200]})
        messages.append({"role": "user", "content": "Continue."})

    return {
        "total_input_tokens": total_input,
        "cache_read_tokens": total_cache_read,
        "cache_write_tokens": total_cache_write,
        "total_cost_usd": total_cost,
        "avg_latency_ms": sum(latencies) / len(latencies),
        "p1_latency_ms": latencies[0],
    }


async def main():
    print(f"{BOLD}Prompt Caching — Before/After Measurement ({ITERATIONS} iterations){RESET}\n")

    print(f"{CYAN}Running WITHOUT cache_control (baseline)...{RESET}")
    before = await measure(with_cache=False)

    print(f"{CYAN}Running WITH cache_control (harness docs cached)...{RESET}")
    after = await measure(with_cache=True)

    cache_pct = (after["cache_read_tokens"] / after["total_input_tokens"] * 100) if after["total_input_tokens"] else 0
    cost_saving = before["total_cost_usd"] - after["total_cost_usd"]
    cost_saving_pct = (cost_saving / before["total_cost_usd"] * 100) if before["total_cost_usd"] else 0

    print(f"\n{'Metric':<35}  {'Without cache':>14}  {'With cache':>14}  {'Delta':>10}")
    print("─" * 80)
    print(f"  {'Total input tokens':<33}  {before['total_input_tokens']:>14,}  {after['total_input_tokens']:>14,}")
    print(f"  {'Cache read tokens':<33}  {'–':>14}  {after['cache_read_tokens']:>14,}  {GREEN}({cache_pct:.0f}% of input){RESET}")
    print(f"  {'Cache write tokens':<33}  {'–':>14}  {after['cache_write_tokens']:>14,}")
    print(f"  {'Total cost (USD)':<33}  ${before['total_cost_usd']:>13.6f}  ${after['total_cost_usd']:>13.6f}  {GREEN}-${cost_saving:.6f} ({cost_saving_pct:.0f}%){RESET}")
    print(f"  {'Avg latency (ms)':<33}  {before['avg_latency_ms']:>13.0f}  {after['avg_latency_ms']:>13.0f}")
    print(f"  {'First-call latency (ms)':<33}  {before['p1_latency_ms']:>13.0f}  {after['p1_latency_ms']:>13.0f}  {GREY}(cache write, slightly slower){RESET}")

    harness_tokens = len(after.get("harness_docs", "")) // 4  # rough estimate
    print(f"\n{GREY}Note: cache_read_tokens = 0 if LiteLLM does not forward cache metadata.{RESET}")
    print(f"{GREY}Check gateway logs for [cache_read=N] to confirm caching is active.{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
