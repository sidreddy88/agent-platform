"""
Test the RequirementsAgent.
Run: python test_requirements_agent.py
"""

import asyncio
from app.agents.requirements import RequirementsAgent


REQUIREMENT = (
    "Add a feature that lets users export their dashboard data to CSV. "
    "Should work for all chart types. Need to handle large datasets without timing out."
)


async def main():
    agent = RequirementsAgent()

    print("Input requirement:")
    print(f"  {REQUIREMENT}\n")
    print("Running agent...\n")

    result = await agent.run(REQUIREMENT)

    print("=" * 60)
    print(result.answer)
    print("=" * 60)
    print(f"\nCompleted in {result.iterations} iterations")
    print("\nSteps:")
    for step in result.steps:
        print(f"\n  [{step.iteration}] Thought: {step.thought[:80]}...")
        if step.action:
            print(f"       Action: {step.action}")


asyncio.run(main())
