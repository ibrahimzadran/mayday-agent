"""Mayday — flight-disruption rescue agent."""

import os
import re
from typing import Optional

import httpx
from google.adk.agents import Agent
from google.adk.tools import ToolContext

# The airline API. Env var so Phase 8 can point this at Cloud Run without a
# code change; localhost:8001 is the local default (adk web owns 8000).
BACKEND_URL = os.getenv("MAYDAY_BACKEND_URL", "http://localhost:8001")

# Free-tier quota is per model, and the newest flash models get the smallest
# allowance (20 requests/day). A single turn costs one request per tool
# round-trip, so development and eval runs need a roomier model than demos do.
# Override with MAYDAY_MODEL, e.g. MAYDAY_MODEL=gemini-3.5-flash-lite adk web
MODEL = os.getenv("MAYDAY_MODEL", "gemini-3.6-flash")

# One client for the process: reuses the TCP connection across tool calls
# instead of paying the handshake every time.
# connect=2s fails fast when the backend simply is not running; read=5s is the
# budget for a backend that accepted the connection and then went quiet.
_client = httpx.Client(
    base_url=BACKEND_URL,
    timeout=httpx.Timeout(5.0, connect=2.0),
)

# The single message the passenger sees for every infrastructure failure.
# Deliberately vague: "reservation system unavailable" tells the model the
# problem is ours, not the passenger's, without leaking status codes or stack
# traces into the conversation.
UNAVAILABLE = {"error": "reservation system unavailable"}

# Session-state key holding the rebooking option the agent has presented and
# is waiting on. Its presence is what makes a confirmation meaningful — see
# confirm_rebook for why this is enforced in code and not only in the prompt.
PENDING_KEY = "pending_rebook_option"


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------


def _request(
    method: str,
    path: str,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
) -> dict:
    """Call the airline API and classify the outcome.

    Returns an envelope the tools switch on, never a raw response:

        {"outcome": "ok", "data": {...}}       # 200
        {"outcome": "not_found"}               # 404 — a real answer, not a failure
        {"outcome": "conflict", "message": …}  # 409 — the action is not allowed
        {"outcome": "invalid_request", ...}    # 422 — the model sent bad arguments
        {"outcome": "unavailable"}             # timeout, 5xx, refused, anything else

    Five outcomes rather than one because different actors can fix them: the
    passenger (wrong flight number), the model (bad argument, worth retrying),
    the situation (flight sold out — pick another), and nobody (backend down).
    """
    try:
        response = _client.request(method, path, params=params, json=json_body)
    except httpx.TimeoutException:
        # Backend accepted the connection then hung, or never answered.
        return {"outcome": "unavailable"}
    except httpx.RequestError:
        # Connection refused, DNS failure, network gone. Distinct from a
        # timeout in cause, identical in what the passenger can do about it.
        return {"outcome": "unavailable"}

    if response.status_code == 404:
        return {"outcome": "not_found"}

    if response.status_code == 409:
        return {"outcome": "conflict", "message": str(_safe_detail(response))}

    if response.status_code == 422:
        # The API echoes back what it disliked. detail is a string when the
        # backend raised it, a list of objects when pydantic rejected input —
        # str() flattens both rather than assuming a shape.
        return {"outcome": "invalid_request", "message": str(_safe_detail(response))}

    if response.status_code >= 500 or response.status_code != 200:
        # Unexpected but non-fatal statuses collapse into unavailable so no
        # unhandled case can ever reach the model.
        return {"outcome": "unavailable"}

    try:
        return {"outcome": "ok", "data": response.json()}
    except ValueError:
        # 200 with a body that is not JSON — a proxy error page, usually.
        return {"outcome": "unavailable"}


def _safe_detail(response: httpx.Response) -> object:
    """Pull `detail` out of an error body without trusting it to exist."""
    try:
        return response.json().get("detail", "request rejected")
    except (ValueError, AttributeError):
        return "request rejected"


# --------------------------------------------------------------------------
# read-only tools
# --------------------------------------------------------------------------


def get_flight_status(flight_no: str) -> dict:
    """Look up the current status of a flight by flight number.

    Call this before saying anything about a specific flight. It also returns
    the flight's origin and destination airport codes, which are required to
    search for alternate flights.

    Args:
        flight_no: The flight number, e.g. "UA482". Case-insensitive.

    Returns:
        On success: flight_no, origin, dest (3-letter airport codes),
        scheduled_departure, scheduled_arrival (ISO 8601 with UTC offset,
        local to each airport), status (ON_TIME | DELAYED | CANCELLED),
        reason, gate, seats_available.
        If no such flight: {"found": false, ...} with a message.
        If the airline systems are down: {"error": "reservation system unavailable"}.
    """
    result = _request("GET", f"/flights/{flight_no.strip().upper()}")

    if result["outcome"] == "ok":
        return result["data"]

    if result["outcome"] == "not_found":
        # A 404 is a legitimate answer to a legitimate question — the flight
        # genuinely does not exist — so it is not shaped like an error. Only
        # infrastructure failures get an "error" key.
        return {
            "found": False,
            "flight_no": flight_no,
            "message": (
                f"No flight {flight_no} exists. Ask the passenger to "
                "double-check the flight number."
            ),
        }

    if result["outcome"] == "invalid_request":
        return {"error": "invalid_request", "message": result["message"]}

    return UNAVAILABLE


def get_booking(booking_ref: str) -> dict:
    """Look up a passenger's booking by its 6-character booking reference.

    Args:
        booking_ref: The booking reference, e.g. "K7QM2P". Case-insensitive.

    Returns:
        On success: booking_ref, passenger, last_name, fare_class, seat,
        status, and a nested "flight" object with the full current status of
        the flight they are booked on.
        If no such booking: {"found": false, ...} with a message.
        If the airline systems are down: {"error": "reservation system unavailable"}.
    """
    result = _request("GET", f"/bookings/{booking_ref.strip().upper()}")

    if result["outcome"] == "ok":
        return result["data"]

    if result["outcome"] == "not_found":
        return {
            "found": False,
            "booking_ref": booking_ref,
            "message": (
                f"No booking {booking_ref} exists. Ask the passenger to "
                "double-check the reference from their confirmation email."
            ),
        }

    if result["outcome"] == "invalid_request":
        return {"error": "invalid_request", "message": result["message"]}

    return UNAVAILABLE


def find_alternate_flights(
    origin: str, dest: str, arrive_by: Optional[str] = None
) -> dict:
    """Find flights a passenger could be moved to on a given route.

    You must know the airport codes before calling this. Get them from
    get_flight_status on the passenger's original flight — never guess an
    airport code from a city name.

    If the passenger stated any arrival deadline, pass arrive_by. Filtering
    here is authoritative; filtering the returned list yourself is not.

    Sold-out flights are included in the results with sold_out: true and
    seats_available: 0. Do not offer those as options; say they are full.

    Args:
        origin: Departure airport code, e.g. "IAD".
        dest: Arrival airport code, e.g. "DEN".
        arrive_by: Optional latest acceptable arrival, ISO 8601 with the
            destination's UTC offset, e.g. "2026-08-18T00:00:00-06:00" for
            midnight in Denver. Never pass a vague phrase like "midnight".

    Returns:
        On success: origin, dest, count, and alternates — a list of flights
        sorted earliest arrival first, each with flight_no, scheduled_departure,
        scheduled_arrival, status, gate, seats_available and sold_out.
        If the airline systems are down: {"error": "reservation system unavailable"}.
    """
    params = {"origin": origin.strip().upper(), "dest": dest.strip().upper()}
    if arrive_by:
        params["arrive_by"] = arrive_by

    result = _request("GET", "/alternates", params=params)

    if result["outcome"] == "ok":
        data = result["data"]
        # sold_out is derived here rather than left to the model to infer from
        # seats_available == 0. An explicit boolean is much harder to overlook
        # than an integer that happens to be zero.
        alternates = [
            {**flight, "sold_out": flight["seats_available"] <= 0}
            for flight in data.get("alternates", [])
        ]
        bookable = [f for f in alternates if not f["sold_out"]]
        return {
            "origin": data["origin"],
            "dest": data["dest"],
            "count": data["count"],
            # Precomputed so "what is the earliest flight I can actually take"
            # is a lookup rather than something the model has to derive.
            "earliest_bookable_flight_no": bookable[0]["flight_no"] if bookable else None,
            "alternates": alternates,
        }

    if result["outcome"] == "not_found":
        # The route endpoint does not 404, but handled so no outcome falls
        # through to an unintended branch if the API changes.
        return {
            "origin": origin,
            "dest": dest,
            "count": 0,
            "earliest_bookable_flight_no": None,
            "alternates": [],
        }

    if result["outcome"] == "invalid_request":
        # Recoverable by the model: it sent a malformed arrive_by and can
        # retry with a corrected one, or drop the parameter entirely.
        return {"error": "invalid_request", "message": result["message"]}

    return UNAVAILABLE


# --------------------------------------------------------------------------
# the consent gate
# --------------------------------------------------------------------------

# Words that make an utterance a hedge or a question rather than an
# instruction. "the 7am could work" is interest, not consent.
_HEDGE = re.compile(
    r"\b(maybe|might|could|would|probably|possibly|perhaps|thinking|"
    r"considering|prefer|rather|what if|how about|not sure|unsure|"
    r"i guess|suppose)\b",
    re.IGNORECASE,
)

# Refusal, or an instruction to stop. Checked BEFORE the affirmation pattern
# and independently of it: "no dont book it" contains "book it", so matching
# consent phrases alone reads a refusal as a yes.
_NEGATE = re.compile(
    r"\b(no|nope|nah|not|dont|don't|do not|doesn't|never|cancel|stop|"
    r"hold off|wait|instead|actually)\b",
    re.IGNORECASE,
)

# An explicit go-ahead. Deliberately short: adding more phrasings widens the
# gate, and a false rejection costs one clarifying question while a false
# acceptance costs a wrongly rebooked passenger.
_AFFIRM = re.compile(
    r"\b(yes|yeah|yep|yup|correct|confirm|confirmed|do it|go ahead|"
    r"book it|book me|please book|take it|i'll take|ill take|sounds good|"
    r"lets do|let's do|proceed|ok book|okay book)\b",
    re.IGNORECASE,
)


def _latest_user_text(tool_context: ToolContext) -> str:
    """The passenger's actual words for this turn, straight from the session.

    Read from the invocation rather than from a tool argument, so the model
    cannot substitute its own paraphrase for what was really said.
    """
    content = tool_context.user_content
    if not content or not content.parts:
        return ""
    return " ".join(part.text for part in content.parts if part.text)


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def propose_rebook(
    booking_ref: str, new_flight_no: str, tool_context: ToolContext
) -> dict:
    """Stage a rebooking so it can be read back to the passenger for approval.

    This does NOT change the booking. Call it when the passenger has shown
    interest in a specific flight. Then present the returned details and ask
    for a clear yes or no. Only after the passenger explicitly agrees, in a
    later message, may you call confirm_rebook.

    Args:
        booking_ref: The passenger's booking reference, e.g. "K7QM2P".
        new_flight_no: The flight to move them to, e.g. "UA118".

    Returns:
        awaiting_confirmation: true plus the details to read back, or an
        explanation of why this flight cannot be staged (sold out, cancelled,
        already booked, unknown).
    """
    booking_ref = booking_ref.strip().upper()
    new_flight_no = new_flight_no.strip().upper()

    booking = get_booking(booking_ref)
    if booking.get("error"):
        return booking
    if booking.get("found") is False:
        return booking

    flight = get_flight_status(new_flight_no)
    if flight.get("error"):
        return flight
    if flight.get("found") is False:
        return flight

    # Checked here so the passenger is never asked to approve something the
    # airline would reject a moment later.
    if flight["status"] == "CANCELLED":
        return {
            "staged": False,
            "reason": f"Flight {new_flight_no} is itself cancelled.",
        }
    if flight["seats_available"] <= 0:
        return {
            "staged": False,
            "reason": f"Flight {new_flight_no} has no seats left.",
        }
    if booking["flight_no"] == new_flight_no:
        return {
            "staged": False,
            "reason": f"This booking is already on {new_flight_no}.",
        }

    tool_context.state[PENDING_KEY] = {
        "booking_ref": booking_ref,
        "new_flight_no": new_flight_no,
        # The turn this was staged in. confirm_rebook requires a LATER turn,
        # which is what guarantees the passenger had a chance to answer.
        "invocation_id": tool_context.invocation_id,
    }

    return {
        "awaiting_confirmation": True,
        "booking_ref": booking_ref,
        "passenger": booking["passenger"],
        "from_flight": booking["flight_no"],
        "to_flight": new_flight_no,
        "departs": flight["scheduled_departure"],
        "arrives": flight["scheduled_arrival"],
        "instruction": (
            "Read these details back to the passenger and ask them to confirm "
            "with a clear yes. Do not call confirm_rebook in this same turn."
        ),
    }


def confirm_rebook(
    booking_ref: str,
    new_flight_no: str,
    passenger_confirmation: str,
    tool_context: ToolContext,
) -> dict:
    """Actually rebook the passenger. Irreversible.

    Only call this when all of the following are true: you staged this exact
    flight with propose_rebook, you read it back, and the passenger then
    replied with an unambiguous yes. Quote their exact words in
    passenger_confirmation — copied verbatim from their message, never
    paraphrased or invented.

    Args:
        booking_ref: The booking reference being changed.
        new_flight_no: The flight being moved to. Must match what was staged.
        passenger_confirmation: The passenger's own words agreeing to this,
            copied exactly from their most recent message.

    Returns:
        On success: confirmation_code and the new itinerary.
        If refused: {"rebooked": false} with the reason, which you must fix by
        going back and asking the passenger properly.
    """
    booking_ref = booking_ref.strip().upper()
    new_flight_no = new_flight_no.strip().upper()
    pending = tool_context.state.get(PENDING_KEY)

    # Gate 1 — something must have been staged. This is what stops a bare
    # "sure" from booking anything: with no pending option there is nothing
    # the passenger could have been agreeing to.
    if not pending:
        return {
            "rebooked": False,
            "reason": (
                "No rebooking option has been presented to this passenger. "
                "Present a specific flight with propose_rebook and get their "
                "agreement first."
            ),
        }

    # Gate 2 — consent is specific to one flight and one booking. Agreement to
    # the 9:40 is not agreement to the 7:00.
    if (
        pending["booking_ref"] != booking_ref
        or pending["new_flight_no"] != new_flight_no
    ):
        return {
            "rebooked": False,
            "reason": (
                f"The passenger was asked about {pending['new_flight_no']} for "
                f"booking {pending['booking_ref']}, not {new_flight_no} for "
                f"{booking_ref}. Present the new option and ask again."
            ),
        }

    # Gate 3 — the passenger must have had a turn to answer. Staging and
    # confirming inside one turn means the model answered on their behalf.
    if pending.get("invocation_id") == tool_context.invocation_id:
        return {
            "rebooked": False,
            "reason": (
                "This option was only just presented. Wait for the passenger "
                "to actually reply before confirming."
            ),
        }

    # Gate 4 — the quoted agreement must appear in what the passenger really
    # typed. Reading the transcript rather than trusting the argument is what
    # stops the model from manufacturing consent it never received.
    spoken = _normalize(_latest_user_text(tool_context))
    quoted = _normalize(passenger_confirmation)
    if not quoted or quoted not in spoken:
        return {
            "rebooked": False,
            "reason": (
                "The confirmation quoted does not match what the passenger "
                "said. Ask them directly whether to book this flight."
            ),
        }

    # Gate 5 — hedges, questions and refusals are not consent, even when they
    # mention the flight approvingly or contain the words "book it".
    if _NEGATE.search(quoted) or _HEDGE.search(quoted) or not _AFFIRM.search(quoted):
        return {
            "rebooked": False,
            "reason": (
                "That is not an unambiguous yes. Ask the passenger plainly "
                "whether they want this flight booked."
            ),
        }

    result = _request(
        "POST",
        "/rebook",
        json_body={"booking_ref": booking_ref, "new_flight_no": new_flight_no},
    )

    if result["outcome"] == "ok":
        # Consent is spent. Leaving it set would let a later "yes" to an
        # unrelated question rebook the passenger a second time.
        tool_context.state[PENDING_KEY] = None
        data = result["data"]
        return {
            "rebooked": True,
            "confirmation_code": data["confirmation_code"],
            "passenger": data["passenger"],
            "from_flight": data["previous_flight_no"],
            "to_flight": data["new_flight"]["flight_no"],
            "departs": data["new_flight"]["scheduled_departure"],
            "arrives": data["new_flight"]["scheduled_arrival"],
            "seat": data["seat"],
        }

    if result["outcome"] == "conflict":
        # Seats sold out between staging and confirming, most likely.
        tool_context.state[PENDING_KEY] = None
        return {"rebooked": False, "reason": result["message"]}

    if result["outcome"] == "not_found":
        tool_context.state[PENDING_KEY] = None
        return {
            "rebooked": False,
            "reason": "That booking or flight no longer exists.",
        }

    return UNAVAILABLE


def issue_voucher(booking_ref: str, voucher_type: str) -> dict:
    """Issue a meal or hotel voucher to a passenger whose flight was disrupted.

    Vouchers cost the passenger nothing and can be re-requested safely, so
    unlike rebooking this needs no confirmation step.

    Args:
        booking_ref: The passenger's booking reference, e.g. "K7QM2P".
        voucher_type: Either "meal" or "hotel".

    Returns:
        voucher_code, amount_usd, and whether it had already been issued.
        If the airline systems are down: {"error": "reservation system unavailable"}.
    """
    voucher_type = voucher_type.strip().lower()
    if voucher_type not in ("meal", "hotel"):
        return {
            "issued": False,
            "reason": "Only meal and hotel vouchers exist.",
        }

    result = _request(
        "POST",
        "/vouchers",
        json_body={
            "booking_ref": booking_ref.strip().upper(),
            "type": voucher_type,
        },
    )

    if result["outcome"] == "ok":
        data = result["data"]
        return {
            "issued": True,
            "voucher_code": data["voucher_code"],
            "voucher_type": data["type"],
            "amount_usd": data["amount_usd"],
            # True means this passenger already had one; the same code is
            # returned rather than a second voucher being created.
            "already_had_one": data["reissued"],
        }

    if result["outcome"] == "not_found":
        return {
            "issued": False,
            "reason": f"No booking {booking_ref} exists.",
        }

    if result["outcome"] in ("conflict", "invalid_request"):
        return {"issued": False, "reason": result["message"]}

    return UNAVAILABLE


root_agent = Agent(
    name="mayday",
    model=MODEL,
    description="Flight-disruption rescue agent that helps stranded passengers.",
    instruction=(
        "You are Mayday, a calm, efficient flight-disruption assistant for "
        "passengers whose travel plans just fell apart.\n"
        "\n"
        "GROUNDING\n"
        "1. ALWAYS call a tool before stating any flight, booking, or seat "
        "fact. Never guess or invent flight information, times, gates, or "
        "airport codes.\n"
        "2. To search for alternate flights you need the origin and "
        "destination airport codes. Get them by calling get_flight_status on "
        "the passenger's original flight first, then pass that flight's "
        "origin and dest to find_alternate_flights. A passenger saying "
        "'get me to Denver' does not tell you which airport they are "
        "departing from.\n"
        "3. If the passenger states any arrival deadline, pass it as the "
        "arrive_by argument, converted to ISO 8601 with the DESTINATION "
        "airport's UTC offset. Do not filter the results yourself instead.\n"
        "4. A flight with sold_out: true has no seats. Report it as full. "
        "Never present it as an option the passenger can take.\n"
        "5. When asked for the earliest or best option, name ONE specific "
        "flight as your recommendation. If the earliest flight overall is "
        "full, say so and recommend the earliest one with seats.\n"
        "6. Report times as they appear, noting they are local to each "
        "airport. Do not convert between timezones.\n"
        "\n"
        "REBOOKING — the most important rules here\n"
        "7. Rebooking is irreversible. NEVER rebook without the passenger's "
        "explicit, unambiguous agreement to one specific flight.\n"
        "8. The sequence is always: call propose_rebook for one specific "
        "flight, read the details back, ask for a yes or no, WAIT for their "
        "reply, and only then call confirm_rebook.\n"
        "9. Interest is not consent. 'the 7am could work', 'what about the "
        "later one', 'that sounds better' all mean present it and ask — they "
        "do not mean book it. Only a direct instruction such as 'yes, book "
        "the 7am' is consent.\n"
        "9a. But the moment a passenger points at a specific flight in ANY "
        "way, even tentatively, call propose_rebook for it in that same turn "
        "and then ask. Never reply with only a question when you could have "
        "staged the flight first — if you wait, their next 'yes' has nothing "
        "to attach to and you will have to ask them twice.\n"
        "10. When calling confirm_rebook, quote the passenger's own words "
        "exactly as they typed them. Never paraphrase, never supply words "
        "they did not say.\n"
        "11. If a tool refuses a rebooking, it is telling you consent was not "
        "properly obtained. Go back and ask the passenger plainly. Never "
        "retry the same call hoping it succeeds.\n"
        "12. After a successful rebooking, give the passenger their "
        "confirmation code clearly.\n"
        "\n"
        "VOUCHERS\n"
        "13. If a flight is cancelled or heavily delayed, you may offer a "
        "meal voucher, and a hotel voucher when the delay runs overnight. "
        "Vouchers need no confirmation step.\n"
        "\n"
        "FAILURE HANDLING\n"
        "14. If a tool returns an unavailable error, the airline systems are "
        "down and it is not the passenger's fault. Apologize once, briefly, "
        "and offer to try again shortly. Do not call the same tool again in "
        "the same turn, and never invent data to fill the gap.\n"
        "15. If a tool returns found: false, the number does not exist. Ask "
        "the passenger to double-check it.\n"
        "\n"
        "STYLE\n"
        "16. Never show the passenger raw errors, status codes, tool names, "
        "field names, or JSON. Say 'that flight is full', never "
        "'sold_out: true'.\n"
        "17. Acknowledge a disruption in one sentence, with warmth and "
        "without groveling, then move to what you can do next.\n"
        "18. Stay on topic: flights and travel disruption. Politely decline "
        "anything else."
    ),
    tools=[
        get_flight_status,
        get_booking,
        find_alternate_flights,
        propose_rebook,
        confirm_rebook,
        issue_voucher,
    ],
)
