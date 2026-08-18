"""Airline tools over MCP (stdio transport).

Run standalone:

    python -m mcp_server.server

The agent normally spawns this itself — see MCPToolset in mayday/agent.py.

WHAT IS NOT HERE, AND WHY
-------------------------
Rebooking is absent on purpose. An MCP server is callable by *any* client that
speaks the protocol, not only by our agent, so a control that lives in the
client is not a control at all — a second client could call `rebook` directly
and the consent gate would never run.

The gate also cannot move here intact. Its strongest check reads the
passenger's actual words out of the ADK session and verifies the quoted
agreement really appears in them. Across a process boundary the server has no
session and no transcript; it could only be handed a string claiming what the
passenger said, which is precisely the assertion the check exists to distrust.
Moving it would look like the same gate while silently degrading to "trust the
caller".

So the split is by trust boundary, not by convenience: MCP carries data access
and reversible actions, while the irreversible one stays in-process next to
the conversation it depends on.
"""

import sys

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# The policy index needs an API key to embed queries. Loaded here because this
# runs as its own process and inherits nothing from the agent.
load_dotenv("mayday/.env")

from mayday import airline_client, policy_index  # noqa: E402

mcp = FastMCP("mayday-airline")


@mcp.tool()
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
    return airline_client.get_flight_status(flight_no)


@mcp.tool()
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
    return airline_client.get_booking(booking_ref)


@mcp.tool()
def find_alternate_flights(origin: str, dest: str, arrive_by: str = "") -> dict:
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
            midnight in Denver. Pass "" if the passenger gave no deadline.
            Never pass a vague phrase like "midnight".

    Returns:
        On success: origin, dest, count, earliest_bookable_flight_no, and
        alternates — flights sorted earliest arrival first, each with
        flight_no, scheduled_departure, scheduled_arrival, status, gate,
        seats_available and sold_out.
        If the airline systems are down: {"error": "reservation system unavailable"}.
    """
    return airline_client.find_alternate_flights(origin, dest, arrive_by or None)


@mcp.tool()
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
    return airline_client.issue_voucher(booking_ref, voucher_type)


@mcp.tool()
def search_policy(query: str) -> dict:
    """Search Meridian Airways policy and passenger-rights regulations.

    Use this for any question about entitlements, compensation, refunds,
    vouchers, fare rules, or what the airline owes a passenger. Never answer
    such a question from memory — amounts and thresholds differ by regulation
    and route, and a confident wrong number is worse than no number.

    Write a specific query. Include the route or airports, the reason the
    flight was disrupted, and what the passenger is actually asking. A query
    like "am I owed anything" retrieves far worse than "LHR to JFK cancelled
    due to technical fault, compensation owed".

    Args:
        query: A specific natural-language question, enriched with the route
            and disruption reason you already know from the other tools.

    Returns:
        results: up to three policy extracts, each with citation (the document
        and clause to quote), text, and a relevance score.
        If the airline systems are down: {"error": "reservation system unavailable"}.
    """
    try:
        hits = policy_index.search(query, k=3)
    except RuntimeError:
        # Index not built. A deployment problem, but the passenger experience
        # is the same, so it degrades into the same message rather than
        # exposing setup instructions.
        return airline_client.UNAVAILABLE
    except Exception:
        # Embedding call failed — network, quota, or auth.
        return airline_client.UNAVAILABLE

    return {
        "results": [
            {
                "citation": f"{h['title']} § {h['section']}",
                "document": h["doc"],
                "text": h["text"],
                "score": h["score"],
            }
            for h in hits
        ]
    }


if __name__ == "__main__":
    # stdout is the protocol channel on stdio transport — anything printed
    # there corrupts the stream, so diagnostics go to stderr.
    print("mayday-airline MCP server on stdio", file=sys.stderr)
    mcp.run(transport="stdio")
