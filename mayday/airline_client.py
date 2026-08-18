"""HTTP client for the airline API.

Shared deliberately: the MCP server exposes these to any client, while the
consent-gated rebooking tools call them in-process. Both paths must see the
same error shapes, so the classification lives here rather than in either
caller.
"""

import os
from typing import Optional

import httpx

# The airline API. Env var so Phase 8 can point this at Cloud Run without a
# code change; localhost:8001 is the local default (adk web owns 8000).
BACKEND_URL = os.getenv("MAYDAY_BACKEND_URL", "http://localhost:8001")

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


def request(
    method: str,
    path: str,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
) -> dict:
    """Call the airline API and classify the outcome.

    Returns an envelope callers switch on, never a raw response:

        {"outcome": "ok", "data": {...}}       # 200
        {"outcome": "not_found"}               # 404 — a real answer, not a failure
        {"outcome": "conflict", "message": …}  # 409 — the action is not allowed
        {"outcome": "invalid_request", ...}    # 422 — the caller sent bad arguments
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
        return {"outcome": "invalid_request", "message": str(_safe_detail(response))}
    if response.status_code != 200:
        # 5xx and anything unexpected collapse into unavailable so no
        # unhandled status can ever reach the model.
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
# shaped results — identical whether called over MCP or in-process
# --------------------------------------------------------------------------


def get_flight_status(flight_no: str) -> dict:
    result = request("GET", f"/flights/{flight_no.strip().upper()}")

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
    result = request("GET", f"/bookings/{booking_ref.strip().upper()}")

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
    params = {"origin": origin.strip().upper(), "dest": dest.strip().upper()}
    if arrive_by:
        params["arrive_by"] = arrive_by

    result = request("GET", "/alternates", params=params)

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
        return {
            "origin": origin,
            "dest": dest,
            "count": 0,
            "earliest_bookable_flight_no": None,
            "alternates": [],
        }
    if result["outcome"] == "invalid_request":
        return {"error": "invalid_request", "message": result["message"]}
    return UNAVAILABLE


def issue_voucher(booking_ref: str, voucher_type: str) -> dict:
    voucher_type = voucher_type.strip().lower()
    if voucher_type not in ("meal", "hotel"):
        return {"issued": False, "reason": "Only meal and hotel vouchers exist."}

    result = request(
        "POST",
        "/vouchers",
        json_body={"booking_ref": booking_ref.strip().upper(), "type": voucher_type},
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
        return {"issued": False, "reason": f"No booking {booking_ref} exists."}
    if result["outcome"] in ("conflict", "invalid_request"):
        return {"issued": False, "reason": result["message"]}
    return UNAVAILABLE


def rebook(booking_ref: str, new_flight_no: str) -> dict:
    """Raw rebooking. Returns the envelope, not a shaped result.

    Intentionally NOT exposed over MCP. See mcp_server/server.py for why.
    """
    return request(
        "POST",
        "/rebook",
        json_body={
            "booking_ref": booking_ref.strip().upper(),
            "new_flight_no": new_flight_no.strip().upper(),
        },
    )
