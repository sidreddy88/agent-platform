"""
RequirementsAgent — turns a raw product requirement into a structured technical spec.

Flow (driven by ReAct loop in BaseAgent):
  1. analyze_requirement  → breaks down the requirement into components
  2. estimate_effort      → produces effort estimate based on the analysis
  3. generate_spec        → assembles the final structured spec document
"""

from app.agents.base import AgentResult, BaseAgent
from app.services.llm import LLMService

# ---------------------------------------------------------------------------
# Tool implementations
# Each tool calls Claude with a focused prompt for its specific sub-task.
# ---------------------------------------------------------------------------

async def analyze_requirement(requirement: str, llm: LLMService) -> str:
    """Break down a raw requirement into structured components."""
    prompt = f"""Analyze this product requirement and extract key information.

REQUIREMENT:
{requirement}

Return a structured analysis covering:
- Core functionality (what exactly needs to be built)
- Affected systems / components
- User-facing changes
- Backend / data changes needed
- Edge cases and constraints mentioned
- Implicit requirements (things not stated but obviously needed)

Be specific and technical."""

    return await llm.complete(
        messages=[{"role": "user", "content": prompt}],
        system="You are a senior software architect analyzing product requirements.",
    )


async def estimate_effort(analysis: str, llm: LLMService) -> str:
    """Generate a time estimate based on the requirement analysis."""
    prompt = f"""Based on this requirement analysis, provide an effort estimate.

ANALYSIS:
{analysis}

Return:
- Total estimate (story points + days range)
- Breakdown by area (frontend, backend, database, testing, devops)
- Key assumptions behind the estimate
- What would make it faster or slower
- Confidence level (low / medium / high) and why

Be realistic. Flag any areas of high uncertainty."""

    return await llm.complete(
        messages=[{"role": "user", "content": prompt}],
        system="You are a senior engineer who estimates technical work accurately.",
    )


async def generate_spec(requirement: str, analysis: str, estimate: str, llm: LLMService) -> str:
    """Assemble the final structured technical specification."""
    prompt = f"""Write a complete technical specification using the inputs below.

ORIGINAL REQUIREMENT:
{requirement}

ANALYSIS:
{analysis}

EFFORT ESTIMATE:
{estimate}

Output the spec in exactly this format:

# Technical Specification

## Title
<concise feature title>

## Overview
<2-3 sentence summary of what is being built and why>

## Functional Requirements
- <requirement 1>
- <requirement 2>
- <add as many as needed>

## Technical Approach
<describe the implementation strategy, architecture decisions, key libraries or patterns>

## API Changes
<list any new endpoints, modified endpoints, or request/response schema changes. Write "None" if not applicable>

## Database Changes
<list schema changes, new tables, indexes, migrations needed. Write "None" if not applicable>

## Testing Strategy
- Unit tests: <what to test>
- Integration tests: <what to test>
- Edge cases: <list edge cases to cover>

## Effort Estimate
<summary from the estimate: story points, time range, breakdown>

## Risks
- <risk 1>
- <risk 2>

## Open Questions
- <question 1>
- <question 2>

Be thorough. A developer should be able to start implementation from this spec alone."""

    return await llm.complete(
        messages=[{"role": "user", "content": prompt}],
        system="You are a staff engineer writing a technical specification document.",
    )


# ---------------------------------------------------------------------------
# RequirementsAgent
# ---------------------------------------------------------------------------

class RequirementsAgent(BaseAgent):
    """
    Converts a raw product requirement or ticket into a structured technical spec.

    Usage:
        agent = RequirementsAgent()
        result = await agent.run("Add CSV export for dashboard charts...")
        print(result.answer)   # the full spec
    """

    def __init__(self) -> None:
        super().__init__()
        self._register_tools()

    def _register_tools(self) -> None:
        llm = self._llm  # capture for closures

        async def _analyze(requirement: str) -> str:
            return await analyze_requirement(requirement, llm)

        async def _estimate(analysis: str) -> str:
            return await estimate_effort(analysis, llm)

        async def _generate(requirement: str, analysis: str, estimate: str) -> str:
            return await generate_spec(requirement, analysis, estimate, llm)

        self.register_tool(
            "analyze_requirement",
            _analyze,
            "Break down a raw requirement into components. Input: {requirement: string}",
        )
        self.register_tool(
            "estimate_effort",
            _estimate,
            "Generate effort estimate from an analysis. Input: {analysis: string}",
        )
        self.register_tool(
            "generate_spec",
            _generate,
            "Assemble the final spec from all gathered info. Input: {requirement: string, analysis: string, estimate: string}",
        )

    async def run(self, user_input: str) -> AgentResult:
        """Run the requirements agent on a raw requirement string."""
        return await super().run(user_input)
