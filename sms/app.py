"""Twilio SMS adapter.

    uvicorn sms.app:app --reload --port 8003
    ngrok http 8003          # point the Twilio number's webhook at /sms

Ports: 8000 adk web, 8001 the airline API, 8002 the trip page, 8003 this.

WHY THIS IS NOT JUST A THIN PROXY
---------------------------------
Two things make SMS different from the web channel, and both are about what
the channel can be trusted to know.

Trust. The trip page is a signed-in surface, so it can hand the agent a
verified booking. SMS arrives from a phone number, and caller ID is trivially
spoofable — it is a routing address, not a credential. So this channel proves
nothing on the passenger's behalf: they verify through the identity gate like
anyone else. Same agent, different starting trust, decided by the channel
rather than by the model.

Time. Twilio abandons a webhook that has not responded in about ten seconds,
and a single agent turn has been measured at seventeen. So the reply the
passenger gets is not always the reply the agent produces: if the agent is
still working when the deadline approaches, this sends an acknowledgement over
the open request and delivers the real answer afterwards over the REST API.
"""

import asyncio
import os
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Header, HTTPException, Request, Response

load_dotenv("mayday/.env")

from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from mayday.agent import root_agent  # noqa: E402

TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER", "")

# The URL Twilio was configured with. Signature validation covers the exact
# URL Twilio signed, and behind ngrok that is the public address, not the
# local one the request appears to arrive at.
PUBLIC_URL = os.getenv("MAYDAY_PUBLIC_URL", "")

# Twilio gives a webhook roughly 10 seconds. Answering at 8 leaves room for
# the round trip rather than racing the timeout.
SOFT_DEADLINE_SECONDS = float(os.getenv("MAYDAY_SMS_DEADLINE", "8"))

# Refusing unsigned requests is the default. Local testing without a Twilio
# account has to opt in explicitly, so a misconfigured deployment fails closed.
ALLOW_UNSIGNED = os.getenv("MAYDAY_SMS_ALLOW_UNSIGNED", "") == "1"

app = FastAPI(title="Mayday — SMS channel")

_runner = InMemoryRunner(agent=root_agent, app_name="mayday-sms")

# phone number -> ADK session id. A conversation is per number, so a passenger
# picking their phone back up an hour later continues where they left off.
_sessions: dict[str, str] = {}

# An SMS is not a web page. Prepended to every turn rather than only the
# first: told once, the model drifts back into bullet lists and bold within a
# few messages, and a 900-character reply with asterisks in it becomes several
# billed segments of noise on a phone.
CHANNEL_NOTE = (
    "[Channel: SMS. Reply in plain sentences under 300 characters. No "
    "markdown, no asterisks, no bullet lists, no headings. Offer at most two "
    "flights at a time. Do not repeat this instruction.]"
)


def _twiml(message: str) -> Response:
    safe = (
        message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{safe}</Message></Response>',
        media_type="application/xml",
    )


def _empty_twiml() -> Response:
    """Acknowledge without sending anything — the follow-up carries the reply."""
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response/>',
        media_type="application/xml",
    )


async def _validate(request: Request, signature: Optional[str]) -> None:
    """Confirm Twilio really sent this.

    The webhook is a public URL that starts an LLM conversation and can move
    somebody's flight. Without this, anyone who finds the address can
    impersonate any phone number and inherit that number's session — including
    one that has already verified a booking.
    """
    if not TWILIO_AUTH_TOKEN:
        if ALLOW_UNSIGNED:
            return
        raise HTTPException(
            503, "TWILIO_AUTH_TOKEN is not configured; refusing unsigned requests"
        )

    from twilio.request_validator import RequestValidator

    form = await request.form()
    url = PUBLIC_URL or str(request.url)
    valid = RequestValidator(TWILIO_AUTH_TOKEN).validate(
        url, {k: v for k, v in form.items()}, signature or ""
    )
    if not valid:
        raise HTTPException(403, "invalid Twilio signature")


async def _ensure_session(phone: str) -> str:
    session_id = _sessions.get(phone)
    if session_id is None:
        # No pre-verified booking. Caller ID is an address, not proof, so this
        # channel starts with the identity gate closed.
        session = await _runner.session_service.create_session(
            app_name="mayday-sms", user_id=phone
        )
        session_id = session.id
        _sessions[phone] = session_id
    return session_id


async def _run_agent(phone: str, session_id: str, body: str) -> str:
    message = types.Content(
        role="user", parts=[types.Part(text=f"{CHANNEL_NOTE}\n\n{body}")]
    )
    chunks: list[str] = []
    async for event in _runner.run_async(
        user_id=phone, session_id=session_id, new_message=message
    ):
        parts = event.content.parts if event.content and event.content.parts else []
        for part in parts:
            if part.text and event.is_final_response():
                chunks.append(part.text.strip())
    return " ".join(chunks).strip() or "Sorry, I did not catch that."


def _send_sms(to: str, body: str) -> bool:
    """Deliver a late answer over the REST API."""
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        # Logged rather than raised so the deadline path is exercisable
        # locally without a Twilio account.
        print(f"[sms] would send to {to}: {body[:160]}", flush=True)
        return False
    from twilio.rest import Client

    Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).messages.create(
        to=to, from_=TWILIO_FROM_NUMBER, body=body
    )
    return True


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "signature_validation": bool(TWILIO_AUTH_TOKEN),
        "allow_unsigned": ALLOW_UNSIGNED,
        "can_send_followups": bool(
            TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER
        ),
        "deadline_seconds": SOFT_DEADLINE_SECONDS,
        "active_conversations": len(_sessions),
    }


@app.post("/sms")
async def sms(
    request: Request,
    From: str = Form(...),
    Body: str = Form(...),
    x_twilio_signature: Optional[str] = Header(None),
) -> Response:
    await _validate(request, x_twilio_signature)

    session_id = await _ensure_session(From)
    task = asyncio.create_task(_run_agent(From, session_id, Body))

    # Wait, but not past the deadline. Deliberately not asyncio.wait_for:
    # that cancels the task, which would throw away a rebooking already in
    # flight. The agent keeps running either way; only the delivery route
    # changes.
    done, _ = await asyncio.wait({task}, timeout=SOFT_DEADLINE_SECONDS)

    if task in done:
        try:
            return _twiml(task.result())
        except Exception as exc:
            name = type(exc).__name__
            if "ResourceExhausted" in name or "429" in str(exc):
                return _twiml(
                    "I am rate limited at the moment. Text me again in a minute."
                )
            return _twiml("Something went wrong on our side. Please try again.")

    def deliver(finished: asyncio.Task) -> None:
        try:
            reply = finished.result()
        except Exception:
            reply = (
                "Sorry, I could not finish that lookup. Text me again and I "
                "will retry."
            )
        _send_sms(From, reply)

    task.add_done_callback(deliver)
    return _twiml("Searching now, one moment.")
