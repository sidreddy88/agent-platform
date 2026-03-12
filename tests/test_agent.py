"""
Quick smoke test for BaseAgent.
Run from project root: python test_agent.py
"""

import asyncio
from app.agents.base import BaseAgent


# ── Fake tools ────────────────────────────────────────────────────────────────

async def calculator(expression: str) -> str:
    try:
        return str(eval(expression, {"__builtins__": {}}))
    except Exception as e:
        return f"Error: {e}"


async def get_weather(city: str) -> str:
    # Fake — replace with a real API call later
    return f"The weather in {city} is 72°F and sunny."


# ── Run ───────────────────────────────────────────────────────────────────────

async def main():
    agent = BaseAgent()
    agent.register_tool("calculator", calculator, "Evaluate a math expression. Input: {expression}")
    agent.register_tool("get_weather", get_weather, "Get weather for a city. Input: {city}")

    question = "What is 123 * 456? Also, what's the weather in Tokyo?"
    print(f"Question: {question}\n")

    result = await agent.run(question)

    print("─" * 50)
    print(f"Answer: {result.answer}")
    print(f"Iterations: {result.iterations}")
    print("─" * 50)

    for step in result.steps:
        print(f"\n[Step {step.iteration}]")
        print(f"  Thought:     {step.thought}")
        if step.action:
            print(f"  Action:      {step.action}")
            print(f"  Input:       {step.action_input}")
            print(f"  Observation: {step.observation}")
        if step.answer:
            print(f"  Answer:      {step.answer}")


asyncio.run(main())
