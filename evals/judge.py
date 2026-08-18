"""LLM-as-judge for the things assertions cannot check.

Scores tone, clarity and whether the reply actually helps. Advisory: a low
score is reported but does not fail the suite on its own, because a judge is
itself a model and a flaky gate is worse than no gate. Deterministic
assertions remain the hard gates.
"""

import os
from typing import Tuple

from evals.trace import Trace

# A different model from the agent on purpose: free-tier quota is per model,
# so judging on the same one halves how many cases a run can complete before
# hitting the per-minute cap. Independence from the model under test is a
# happy side effect, not the reason.
JUDGE_MODEL = os.getenv("MAYDAY_JUDGE_MODEL", "gemini-3.1-flash-lite")

_PROMPT = """You are grading a customer-service agent for an airline.

Rubric for this conversation:
{rubric}

Score 1 to 5, where:
5 = fully meets the rubric, nothing a supervisor would change
4 = meets the rubric with a minor blemish
3 = acceptable but noticeably flawed
2 = fails part of the rubric
1 = fails the rubric

Judge ONLY against the rubric. Do not reward or penalise anything else.
Be strict; 5 should be uncommon.

Transcript:
{transcript}
"""


_client = None


def _get_client():
    """Held at module level deliberately.

    `genai.Client().models.generate_content(...)` looks fine and is not: the
    Client is a temporary, `.models` does not keep it alive, so it can be
    collected — closing its httpx pool — before the request goes out. The
    failure surfaces as "Cannot send a request, as the client has been
    closed", which reads like a bug in httpx rather than a missing reference.
    """
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client()
    return _client


def judge(trace: Trace, rubric: str) -> Tuple[int, str]:
    from google.genai import types

    schema = types.Schema(
        type="OBJECT",
        properties={
            "score": types.Schema(type="INTEGER"),
            "reason": types.Schema(type="STRING"),
        },
        required=["score", "reason"],
    )
    try:
        response = _get_client().models.generate_content(
            model=JUDGE_MODEL,
            contents=_PROMPT.format(rubric=rubric, transcript=trace.transcript()),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                # A judge that varies run to run cannot gate anything.
                temperature=0.0,
            ),
        )
        import json

        data = json.loads(response.text)
        return int(data["score"]), str(data["reason"])
    except Exception as exc:
        # Message included, not just the type: swallowing it cost an hour once.
        return 0, f"judge unavailable: {type(exc).__name__}: {str(exc)[:160]}"
