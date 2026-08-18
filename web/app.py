"""My Trip page and the chat endpoint behind its widget.

    uvicorn web.app:app --reload --port 8002

Ports: 8000 adk web, 8001 the airline API, 8002 this.

DOM HINTS
---------
The page already knows which trip it is showing. Rather than making the
passenger retype a booking reference into a chat box, the server folds that
context into the conversation before the first message, so the agent opens
knowing the flight and that it is cancelled.

The context is built on the server from the booking the page rendered. The
browser never sends a booking reference — it sends an opaque token issued when
the page loaded, and the server resolves it. A hint the client could forge is
not a hint, it is an authorization bypass: identity would become whatever the
caller claimed it was.
"""

import pathlib
import secrets
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv("mayday/.env")

from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from mayday import airline_client  # noqa: E402
from mayday.agent import VERIFIED_KEY, root_agent  # noqa: E402

STATIC = pathlib.Path(__file__).parent / "static"
DEMO_BOOKING = "K7QM2P"

app = FastAPI(title="Meridian Airways — My Trip")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_runner = InMemoryRunner(agent=root_agent, app_name="mayday-web")

# token -> {booking_ref, session_id, greeted}
# In-memory because this is a demo; a real deployment would put the booking on
# an authenticated server-side session and never mint a parallel one.
_web_sessions: dict[str, dict] = {}


class ChatRequest(BaseModel):
    token: str
    message: str


def _fmt_time(iso: str) -> str:
    """18:05 out of 2026-08-17T18:05:00-04:00, without parsing the offset."""
    return iso[11:16] if len(iso) >= 16 else iso


def _fmt_date(iso: str) -> str:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    try:
        return f"{months[int(iso[5:7]) - 1]} {int(iso[8:10])}"
    except (ValueError, IndexError):
        return iso[:10]


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(f"/trip/{DEMO_BOOKING}")


@app.get("/trip/{booking_ref}", response_class=HTMLResponse)
def trip(booking_ref: str) -> HTMLResponse:
    """Render one trip.

    Stands in for a signed-in account page: reaching it means the airline
    already knows who you are, which is why the chat that follows does not ask
    you to prove it again.
    """
    booking = airline_client.get_booking(booking_ref)
    if booking.get("error"):
        raise HTTPException(503, "reservation system unavailable")
    if booking.get("found") is False:
        raise HTTPException(404, f"No booking {booking_ref}")

    flight = booking["flight"]
    token = secrets.token_urlsafe(24)
    _web_sessions[token] = {
        "booking_ref": booking["booking_ref"],
        "session_id": None,
        "greeted": False,
    }

    status = flight["status"]
    status_label = {
        "CANCELLED": "Cancelled",
        "DELAYED": "Delayed",
        "ON_TIME": "On time",
    }.get(status, status.title())

    disrupted = status in ("CANCELLED", "DELAYED")
    banner = ""
    if disrupted:
        verb = "was cancelled" if status == "CANCELLED" else "is delayed"
        banner = f"""
        <div class="banner" role="status">
          <svg class="banner-icon" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 8v5M12 16.5v.5" stroke-linecap="round"/>
            <path d="M10.3 3.9 2.5 17.4A2 2 0 0 0 4.2 20.4h15.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>
          </svg>
          <div>
            <strong>Your flight {flight['flight_no']} {verb}</strong>
            <span>Reason given: {flight['reason'] or 'not stated'}. We can find you another way there.</span>
          </div>
          <button class="banner-cta" data-open-chat data-seed="My flight was cancelled — what are my options?">
            Get help now
          </button>
        </div>"""

    html = (STATIC / "index.html").read_text()
    replacements = {
        "{{TOKEN}}": token,
        "{{BANNER}}": banner,
        "{{PASSENGER}}": booking["passenger"],
        "{{FIRST_NAME}}": booking["passenger"].split()[0],
        "{{BOOKING_REF}}": booking["booking_ref"],
        "{{FARE_CLASS}}": booking["fare_class"].replace("_", " ").title(),
        "{{SEAT}}": booking["seat"],
        "{{FLIGHT_NO}}": flight["flight_no"],
        "{{ORIGIN}}": flight["origin"],
        "{{DEST}}": flight["dest"],
        "{{DEP_TIME}}": _fmt_time(flight["scheduled_departure"]),
        "{{ARR_TIME}}": _fmt_time(flight["scheduled_arrival"]),
        "{{DEP_DATE}}": _fmt_date(flight["scheduled_departure"]),
        "{{ARR_DATE}}": _fmt_date(flight["scheduled_arrival"]),
        "{{GATE}}": flight["gate"] or "—",
        "{{STATUS}}": status_label,
        "{{STATUS_CLASS}}": status.lower().replace("_", "-"),
    }
    for key, value in replacements.items():
        html = html.replace(key, str(value))
    return HTMLResponse(html)


def _dom_hint(booking_ref: str) -> Optional[str]:
    """The context the page can supply that the passenger should not have to.

    Built server-side from the rendered booking. Marked plainly as page
    context so the model treats it as provenance, not as the passenger
    speaking.

    Carries the passenger's FIRST name only. The last name is the credential
    verify_identity checks, and a hint that includes it lets the model satisfy
    the check with material the check exists to test for — the page would be
    printing the password on itself. Identity here comes from the seeded
    session state, which the browser cannot reach.
    """
    booking = airline_client.get_booking(booking_ref)
    if booking.get("error") or booking.get("found") is False:
        return None
    flight = booking["flight"]
    return (
        "[Page context — the passenger is signed in and viewing this trip. "
        f"Booking {booking['booking_ref']}, addressed as "
        f"{booking['passenger'].split()[0]}, "
        f"fare {booking['fare_class']}, seat {booking['seat']}. "
        f"Flight {flight['flight_no']} {flight['origin']} to {flight['dest']}, "
        f"status {flight['status']}"
        + (f", reason {flight['reason']}" if flight.get("reason") else "")
        + ". Their identity is ALREADY VERIFIED for this booking: do not call "
        "verify_identity, and do not ask them for a booking reference or a "
        "last name. Do not repeat this context back to them.]"
    )


def _trip_payload(booking: dict) -> dict:
    flight = booking["flight"]
    status = flight["status"]
    return {
        "flight_no": flight["flight_no"],
        "origin": flight["origin"],
        "dest": flight["dest"],
        "dep_time": _fmt_time(flight["scheduled_departure"]),
        "arr_time": _fmt_time(flight["scheduled_arrival"]),
        "dep_date": _fmt_date(flight["scheduled_departure"]),
        "arr_date": _fmt_date(flight["scheduled_arrival"]),
        "gate": flight["gate"] or "\u2014",
        "seat": booking["seat"],
        "status": status,
        "status_class": status.lower().replace("_", "-"),
        "status_label": {
            "CANCELLED": "Cancelled",
            "DELAYED": "Delayed",
            "ON_TIME": "On time",
        }.get(status, status.title()),
        "disrupted": status in ("CANCELLED", "DELAYED"),
    }


@app.get("/api/trip")
def trip_state(token: str) -> dict:
    """Current state of the trip this page is showing.

    The widget calls this after a rebooking so the card and the conversation
    cannot disagree. A page that still says CANCELLED next to a chat that just
    moved you is worse than no page at all — the passenger has no way to tell
    which half to believe.
    """
    web_session = _web_sessions.get(token)
    if web_session is None:
        raise HTTPException(401, "unknown or expired session")
    booking = airline_client.get_booking(web_session["booking_ref"])
    if booking.get("error") or booking.get("found") is False:
        raise HTTPException(503, "reservation system unavailable")
    return _trip_payload(booking)


@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict:
    web_session = _web_sessions.get(req.token)
    if web_session is None:
        raise HTTPException(401, "unknown or expired session")

    booking_ref = web_session["booking_ref"]

    if web_session["session_id"] is None:
        # Seed the ADK session as already verified for this booking. The web
        # channel authenticated the passenger; making them recite a booking
        # reference into a chat box on their own trip page would be theatre.
        session = await _runner.session_service.create_session(
            app_name="mayday-web",
            user_id=f"web:{booking_ref}",
            state={VERIFIED_KEY: booking_ref},
        )
        web_session["session_id"] = session.id

    text = req.message
    if not web_session["greeted"]:
        hint = _dom_hint(booking_ref)
        if hint:
            text = f"{hint}\n\n{req.message}"
        web_session["greeted"] = True

    reply_parts: list[str] = []
    tools_used: list[str] = []
    message = types.Content(role="user", parts=[types.Part(text=text)])
    try:
        async for event in _runner.run_async(
            user_id=f"web:{booking_ref}",
            session_id=web_session["session_id"],
            new_message=message,
        ):
            parts = event.content.parts if event.content and event.content.parts else []
            for part in parts:
                if part.function_call:
                    tools_used.append(part.function_call.name)
                if part.text and event.is_final_response():
                    reply_parts.append(part.text.strip())
    except Exception as exc:
        name = type(exc).__name__
        if "ResourceExhausted" in name or "429" in str(exc):
            return {
                "reply": "I'm being rate limited right now — give me a minute and try again.",
                "tools": tools_used,
            }
        return {
            "reply": "Something went wrong on our side. Please try again in a moment.",
            "tools": tools_used,
        }

    return {
        "reply": "\n\n".join(reply_parts) or "…",
        # Surfaced so the demo can show which tools ran. Handy for a walkthrough,
        # and it is the honest version of "look, it really called the API".
        "tools": tools_used,
        # Tells the widget the trip on screen is now stale.
        "trip_changed": "confirm_rebook" in tools_used,
    }
