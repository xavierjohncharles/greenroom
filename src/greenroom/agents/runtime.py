"""Shared ADK runtime: how Greenroom actually executes an agent.

https://adk.dev/                      (ADK 2.0 — graph Workflow Runtime)
https://adk.dev/sessions/state/       (Runner + SessionService)
https://adk.dev/tools-custom/         (FunctionTool, ToolContext)

ADK 2.x notes that bit us in 1.x and are worth remembering:
  * Agents are graph *nodes* now. Overriding `_run_async_impl()` is silently ignored.
  * Never append to `session.events` directly — yield the event and let the framework
    persist it.
  * Do not wrap agent bodies in broad `except Exception:` — it masks failures from the
    framework's own retry and human-in-the-loop pausing.
"""

from __future__ import annotations

import uuid

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.genai import types

from greenroom.obs import get_logger
from greenroom.settings import get_settings

log = get_logger(__name__)

APP_NAME = "greenroom"

# Loading settings has the side effect of exporting GOOGLE_GENAI_USE_VERTEXAI and
# friends into os.environ, which is where the genai SDK underneath ADK looks for them.
# Done at import of this module because every agent path goes through it: relying on
# the web app's lifespan to do it meant scripts, workers and tests silently fell back
# to the API-key path and failed with "No API key was provided".
get_settings()

# Sessions are ADK's own conversational scratchpad, deliberately kept separate from
# Greenroom's durable pipeline state in Firestore. Firestore is the system of record;
# an ADK session is throwaway working memory for one agent invocation. Keeping them
# apart means a lost session can never lose a booking.
_session_service: BaseSessionService | None = None


def get_session_service() -> BaseSessionService:
    global _session_service
    if _session_service is None:
        _session_service = InMemorySessionService()
    return _session_service


async def run_agent(
    agent: LlmAgent,
    prompt: str,
    *,
    user_id: str = "greenroom",
    session_id: str | None = None,
) -> str:
    """Run a single agent to completion and return its final text response.

    One invocation, one throwaway session. Agents that need continuity read it from
    Firestore and receive it in the prompt, rather than relying on session memory —
    which makes every agent step independently replayable after a crash.
    """
    session_id = session_id or f"{agent.name}-{uuid.uuid4().hex[:12]}"
    service = get_session_service()
    runner = Runner(app_name=APP_NAME, agent=agent, session_service=service)

    await service.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)

    message = types.Content(role="user", parts=[types.Part(text=prompt)])
    final_text = ""

    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=message
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(p.text or "" for p in event.content.parts)

    log.info(
        "agent run complete",
        extra={"agent": agent.name, "session_id": session_id, "chars": len(final_text)},
    )
    return final_text
