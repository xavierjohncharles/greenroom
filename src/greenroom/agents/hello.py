"""Step 1 round-trip proof: an ADK agent that reaches Gemini 3.5 Flash on Vertex AI.

This exists to prove the mandatory stack end to end — ADK -> Gemini -> Cloud Run —
before any real agent is built on top of it. It is exposed at GET /hello and is the
first thing to check when a deploy looks wrong.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent

from greenroom.agents.runtime import run_agent
from greenroom.models import GEMINI_MODEL

hello_agent = LlmAgent(
    name="hello",
    model=GEMINI_MODEL,
    description="Health-check agent that proves the ADK -> Gemini round trip works.",
    instruction=(
        "You are the deployment health check for Greenroom, an autonomous booking agent "
        "for a live music brand. Reply with exactly one short sentence confirming you are "
        "running, and name the model you are. Do not add pleasantries or formatting."
    ),
)


async def say_hello(prompt: str = "Report in.") -> str:
    return await run_agent(hello_agent, prompt)
