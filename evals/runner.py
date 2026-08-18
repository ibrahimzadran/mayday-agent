"""Execute eval cases against the real agent and capture what happened.

Runs the actual agent, actual tools and actual backend. Nothing is mocked:
a suite that passes against mocks tells you your mocks are consistent.
"""

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from google.genai import types

from evals.assertions import BACKEND
from evals.case import Case
from evals.trace import ToolCall, Trace, Turn

# Evals pin their own model. The default is a -lite model because a 20-case
# suite is roughly 60 requests and the newest flash models allow 20 per day.
EVAL_MODEL = os.getenv("MAYDAY_EVAL_MODEL", "gemini-3.5-flash-lite")


@dataclass
class Result:
    case: Case
    trace: Trace
    failures: list[str] = field(default_factory=list)
    score: Optional[int] = None
    score_reason: str = ""
    seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.failures and self.trace.error is None


def _reset_backend(case: Case) -> None:
    # Reset first, then apply chaos: reset clears any chaos left by the
    # previous case, so the order is load-bearing.
    httpx.post(f"{BACKEND}/admin/reset", timeout=5)
    if case.chaos:
        httpx.post(f"{BACKEND}/admin/chaos", json=case.chaos, timeout=5)


async def run_case(case: Case) -> Result:
    # Imported here so a stale module-level agent cannot outlive a prompt edit
    # between runs.
    from google.adk.runners import InMemoryRunner
    from mayday.agent import root_agent

    root_agent.model = EVAL_MODEL
    _reset_backend(case)

    trace = Trace(case_id=case.id)
    started = time.time()
    runner = InMemoryRunner(agent=root_agent, app_name="mayday-eval")
    session = await runner.session_service.create_session(
        app_name="mayday-eval", user_id="eval"
    )

    for user_text in case.turns:
        turn = Turn(user=user_text)
        pending: dict[str, ToolCall] = {}
        message = types.Content(role="user", parts=[types.Part(text=user_text)])
        try:
            async for event in runner.run_async(
                user_id="eval", session_id=session.id, new_message=message
            ):
                parts = event.content.parts if event.content and event.content.parts else []
                for part in parts:
                    if part.function_call:
                        call = ToolCall(
                            name=part.function_call.name,
                            args=dict(part.function_call.args or {}),
                        )
                        turn.tool_calls.append(call)
                        # Responses arrive later and out of order; the id is
                        # the only reliable link back to the call.
                        if part.function_call.id:
                            pending[part.function_call.id] = call
                    if part.function_response:
                        call = pending.get(part.function_response.id)
                        if call is not None:
                            call.response = part.function_response.response
                    if part.text and event.is_final_response():
                        turn.agent_text = part.text.strip()
        except Exception as exc:
            name = type(exc).__name__
            if "ResourceExhausted" in name or "429" in str(exc):
                trace.error = RATE_LIMIT_ERROR
            else:
                trace.error = f"{name}: {str(exc)[:200]}"
            trace.turns.append(turn)
            break
        trace.turns.append(turn)

    result = Result(case=case, trace=trace, seconds=round(time.time() - started, 1))

    if trace.error:
        return result

    for assertion in case.assertions:
        failure = assertion(trace)
        if failure:
            result.failures.append(failure)

    if case.rubric:
        from evals.judge import judge

        score, reason = judge(trace, case.rubric)
        result.score, result.score_reason = score, reason

    return result


RATE_LIMIT_ERROR = "rate limited by the model provider"


async def run_suite(
    cases: list[Case], pause: float = 0.0, retries: int = 1
) -> list[Result]:
    """Run every case, retrying ones the provider throttled.

    A throttled case is not a failing case — it never ran. Reporting it as a
    failure would train you to ignore red, which is the one thing a suite
    cannot survive.
    """
    results = []
    for case in cases:
        result = await run_case(case)
        attempt = 0
        while result.trace.error == RATE_LIMIT_ERROR and attempt < retries:
            attempt += 1
            print(f"       ...throttled, waiting 62s to retry {case.id}")
            await asyncio.sleep(62)
            result = await run_case(case)
        results.append(result)
        if pause:
            # Free-tier quota is per minute; a suite with no pacing spends the
            # whole allowance in the first few cases.
            await asyncio.sleep(pause)
    return results
