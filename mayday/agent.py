"""Mayday — flight-disruption rescue agent."""

import os
import pathlib
import re
import sys
from typing import Optional

from google.adk.agents import Agent
from google.adk.tools import ToolContext
from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
from mcp import StdioServerParameters

from mayday import airline_client
from mayday.airline_client import UNAVAILABLE

# Free-tier quota is per model, and the newest flash models get the smallest
# allowance (20 requests/day). A single turn costs one request per tool
# round-trip, so development and eval runs need a roomier model than demos do.
# Override with MAYDAY_MODEL, e.g. MAYDAY_MODEL=gemini-3.5-flash-lite adk web
MODEL = os.getenv("MAYDAY_MODEL", "gemini-3.6-flash")

# Session-state keys for identity. Verification is per conversation: proving
# you are the passenger on one booking says nothing about any other.
VERIFIED_KEY = "verified_booking_ref"
ATTEMPTS_KEY = "identity_attempts"

# A booking reference is six characters and a last name is guessable, so the
# pair is weak on its own. Capping attempts is what stops it being brute
# forced in a channel that never gets tired.
MAX_IDENTITY_ATTEMPTS = 3

# Session-state key holding the rebooking option the agent has presented and
# is waiting on. Its presence is what makes a confirmation meaningful — see
# confirm_rebook for why this is enforced in code and not only in the prompt.
PENDING_KEY = "pending_rebook_option"

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Flight lookups, bookings, alternates, vouchers and policy search come from
# the MCP server as a subprocess over stdio. The agent no longer imports those
# functions; it discovers them over the protocol at startup, which is what
# makes them reusable by any other MCP client.
airline_tools = MCPToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_server.server"],
            # Explicit so the subprocess resolves `mayday` and `mcp_server`
            # regardless of where adk web was launched from.
            cwd=str(_REPO_ROOT),
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        ),
        timeout=15.0,
    ),
)


# --------------------------------------------------------------------------
# the identity gate
# --------------------------------------------------------------------------


def _require_verified(booking_ref: str, tool_context: ToolContext) -> Optional[dict]:
    """Refuse unless this conversation has proved it owns this booking.

    Returns None to proceed, or the refusal to hand back to the model.
    """
    verified = tool_context.state.get(VERIFIED_KEY)
    if verified and verified == booking_ref.strip().upper():
        return None
    return {
        "permitted": False,
        "reason": (
            "This conversation has not verified ownership of booking "
            f"{booking_ref.strip().upper()}. Ask the passenger for their "
            "booking reference and the last name on the booking, then call "
            "verify_identity. Do not reveal or change anything until it "
            "succeeds."
        ),
    }


def verify_identity(
    booking_ref: str, last_name: str, tool_context: ToolContext
) -> dict:
    """Check that the passenger owns the booking they are asking about.

    Required before any booking can be read, rebooked, or have a voucher
    issued against it. Ask for both the booking reference and the last name on
    the booking, in the passenger's own words, before calling this.

    Args:
        booking_ref: The 6-character booking reference, e.g. "K7QM2P".
        last_name: The passenger's last name as it appears on the booking.

    Returns:
        verified: true and the passenger's name on success. On failure, a
        refusal — which never says whether the reference or the name was the
        part that was wrong.
    """
    booking_ref = booking_ref.strip().upper()
    attempts = int(tool_context.state.get(ATTEMPTS_KEY, 0))

    if attempts >= MAX_IDENTITY_ATTEMPTS:
        return {
            "verified": False,
            "locked": True,
            "reason": (
                "Too many failed verification attempts in this conversation. "
                "Stop trying and hand the passenger to a human colleague."
            ),
        }

    booking = airline_client.get_booking(booking_ref)
    if booking.get("error"):
        return UNAVAILABLE

    # One failure message for "no such booking" and for "wrong name" alike.
    # Distinguishing them turns the tool into an oracle for which references
    # exist, which is most of the work of guessing one.
    ok = (
        booking.get("found") is not False
        and booking.get("last_name", "").strip().lower() == last_name.strip().lower()
    )
    if not ok:
        tool_context.state[ATTEMPTS_KEY] = attempts + 1
        remaining = MAX_IDENTITY_ATTEMPTS - (attempts + 1)
        return {
            "verified": False,
            "attempts_remaining": max(remaining, 0),
            "reason": (
                "Those details do not match. Ask the passenger to check the "
                "reference and the last name exactly as they appear on the "
                "confirmation email."
            ),
        }

    tool_context.state[VERIFIED_KEY] = booking_ref
    tool_context.state[ATTEMPTS_KEY] = 0
    return {
        "verified": True,
        "booking_ref": booking_ref,
        "passenger": booking["passenger"],
    }


def get_booking(booking_ref: str, tool_context: ToolContext) -> dict:
    """Look up a booking. Requires verify_identity to have succeeded first.

    Args:
        booking_ref: The booking reference, e.g. "K7QM2P". Case-insensitive.

    Returns:
        On success: booking_ref, passenger, fare_class, seat, status, and a
        nested "flight" object with the current status of the flight they are
        on. If this conversation has not verified the booking, a refusal.
    """
    refusal = _require_verified(booking_ref, tool_context)
    if refusal:
        return refusal
    return airline_client.get_booking(booking_ref)


def issue_voucher(
    booking_ref: str, voucher_type: str, tool_context: ToolContext
) -> dict:
    """Issue a meal or hotel voucher to a passenger whose flight was disrupted.

    Vouchers cost the passenger nothing and can be re-requested safely, so
    unlike rebooking this needs no confirmation step — but it does need a
    verified booking, since it spends the airline's money against one.

    Args:
        booking_ref: The passenger's booking reference, e.g. "K7QM2P".
        voucher_type: Either "meal" or "hotel".

    Returns:
        voucher_code, amount_usd, and whether it had already been issued.
    """
    refusal = _require_verified(booking_ref, tool_context)
    if refusal:
        return refusal
    return airline_client.issue_voucher(booking_ref, voucher_type)


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

    refusal = _require_verified(booking_ref, tool_context)
    if refusal:
        return refusal

    # Called in-process rather than over MCP: these are validation reads for
    # a decision the gate is about to make, not tool calls the model chose.
    booking = airline_client.get_booking(booking_ref)
    if booking.get("error"):
        return booking
    if booking.get("found") is False:
        return booking

    flight = airline_client.get_flight_status(new_flight_no)
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

    # Identity before consent: agreeing to a booking you do not own is not
    # consent to anything.
    refusal = _require_verified(booking_ref, tool_context)
    if refusal:
        return refusal

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

    result = airline_client.rebook(booking_ref, new_flight_no)

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


root_agent = Agent(
    name="mayday",
    model=MODEL,
    description="Flight-disruption rescue agent that helps stranded passengers.",
    instruction=(
        "You are Mayday, a calm, efficient flight-disruption assistant for "
        "passengers whose travel plans just fell apart.\n"
        "\n"
        "IDENTITY — before anything else\n"
        "0a. Before reading, changing, or issuing anything against a booking, "
        "you must call verify_identity with the booking reference AND the "
        "last name on the booking. Ask the passenger for both. Never supply a "
        "last name yourself, and never accept one the passenger did not "
        "state.\n"
        "0b. Until verification succeeds, do not reveal ANY detail of a "
        "booking — not the passenger name, the flight, the seat, or whether "
        "the reference exists at all.\n"
        "0c. Verification covers one booking. If the passenger asks about a "
        "different reference, ask them for the last name on THAT booking and "
        "verify it separately. Never reuse a last name from earlier in the "
        "conversation — a different booking belongs to a different person "
        "until proved otherwise.\n"
        "0d. Flight status, schedules and policy are public: answer those "
        "without verification.\n"
        "0d-i. If page context states this booking is already verified, it is. "
        "Do not call verify_identity for it. Never call verify_identity with a "
        "last name you read from context rather than heard from the "
        "passenger.\n"
        "0e. If verification fails repeatedly, stop and offer a human "
        "colleague. Never hint at which part was wrong.\n"
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
        "5a. If the passenger asks for a specific flight and it turns out to "
        "be full or cancelled, do not stop at the bad news. Look up "
        "alternates on that route in the same turn and offer a named "
        "replacement.\n"
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
        "9a. Call propose_rebook in the SAME turn as any question about a "
        "specific flight — whether the passenger named it or you recommended "
        "it. If you ask 'shall I move you to UA1220?' without staging UA1220 "
        "first, their 'yes' has nothing to attach to and you will have to ask "
        "them twice. Never end a turn having named one flight as the "
        "suggestion without having staged it.\n"
        "9b. propose_rebook, staging and confirmation are internal words. To "
        "the passenger say 'I can move you to UA1220 — shall I go ahead?', "
        "never 'I have staged your rebooking'.\n"
        "10. When calling confirm_rebook, quote the passenger's own words "
        "exactly as they typed them. Never paraphrase, never supply words "
        "they did not say.\n"
        "11. If a tool refuses a rebooking, it is telling you consent was not "
        "properly obtained. Go back and ask the passenger plainly. Never "
        "retry the same call hoping it succeeds.\n"
        "12. After a successful rebooking, give the passenger their "
        "confirmation code clearly.\n"
        "\n"
        "POLICY AND PASSENGER RIGHTS\n"
        "13a. For any question about compensation, refunds, entitlements, or "
        "what the airline owes, call search_policy. Never answer from your "
        "own knowledge of EU261, UK261 or DOT rules — the figures differ by "
        "route and regulation and you will get them wrong.\n"
        "13b. Before searching, find out what the flight was and why it was "
        "disrupted. Build a query containing the route and the recorded "
        "reason, e.g. 'LHR to JFK cancelled due to technical fault, "
        "compensation owed'. A vague query retrieves the wrong clause.\n"
        "13c. Answer ONLY from the text the search returns. If it does not "
        "cover the question, say you are not certain and offer a human "
        "colleague. Never state an amount, deadline or threshold that is not "
        "in the retrieved text.\n"
        "13d. Cite the clause you relied on, in plain words: 'under UK261, "
        "clause UK261-2'. Name the rule, not the filename.\n"
        "13e. Note which regulation applies before quoting an amount. "
        "Departures from UK airports fall under UK261, not EU261, and the "
        "amounts differ. A purely domestic US itinerary has no compensation "
        "scheme at all.\n"
        "13f. You cannot approve or pay a compensation claim. Explain what "
        "the passenger appears to be owed and why, then hand off to a "
        "colleague to file it.\n"
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
        # Non-personal data only: flight status, schedules, policy text.
        airline_tools,
        # Everything scoped to one passenger stays here, where the session
        # knows who has proved what.
        verify_identity,
        get_booking,
        issue_voucher,
        # The irreversible one stays here: its checks read the live session
        # and the passenger's actual words, neither of which survives a
        # process boundary. See mcp_server/server.py.
        propose_rebook,
        confirm_rebook,
    ],
)
