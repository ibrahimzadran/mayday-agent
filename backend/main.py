"""Fake airline API — the system of record behind Mayday's tools.

Run it on 8001 (adk web owns 8000):

    source .venv/bin/activate
    uvicorn backend.main:app --reload --port 8001

State lives in memory and mutates: rebooking decrements seats and moves the
booking. POST /admin/reset restores the seed so a demo or eval run starts clean.

GET /alternates fails ~20% of the time on purpose, so the agent has to degrade
gracefully instead of only ever seeing the happy path. Tune with MAYDAY_CHAOS_RATE
(0 = never fail, 1 = always fail); the value is read at startup.
"""

import os
import random
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from backend import seed

app = FastAPI(
    title="Fake Airline API",
    description="Flights, bookings, rebooking and vouchers for the Mayday agent.",
    version="0.1.0",
)

CHAOS_RATE = float(os.getenv("MAYDAY_CHAOS_RATE", "0.2"))

# Runtime-adjustable so an eval run can demand deterministic failure for one
# case and none for the next, without restarting the process.
#   mode "error"   -> 503, the flaky-dependency case
#   mode "timeout" -> sleep past any sane client read timeout
#   scope          -> "alternates" (the default flaky endpoint) or "all"
_chaos = {"rate": CHAOS_RATE, "mode": "error", "scope": "alternates"}

# Longer than the agent's 5s read timeout, short enough not to stall a suite.
CHAOS_SLEEP_SECONDS = 8.0

# Confirmation codes skip O/0 and I/1 — passengers read these over the phone.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

VOUCHER_AMOUNTS_USD = {"meal": 15, "hotel": 120}
VOUCHER_PREFIXES = {"meal": "MEAL-", "hotel": "HTL-"}


# --------------------------------------------------------------------------
# in-memory store
# --------------------------------------------------------------------------

# Keyed by flight_no / booking_ref for O(1) lookup; deep-copied so mutations
# never write back into the seed module.
_flights: dict[str, dict] = {}
_bookings: dict[str, dict] = {}
_vouchers: dict[tuple[str, str], dict] = {}


def _load_seed() -> None:
    global _flights, _bookings, _vouchers
    _flights = {f["flight_no"]: deepcopy(f) for f in seed.FLIGHTS}
    _bookings = {b["booking_ref"]: deepcopy(b) for b in seed.BOOKINGS}
    _vouchers = {}


_load_seed()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _norm_flight_no(flight_no: str) -> str:
    """Passengers type 'ua 482'; the store keys on 'UA482'."""
    return flight_no.strip().upper().replace(" ", "")


def _norm_ref(booking_ref: str) -> str:
    return booking_ref.strip().upper().replace(" ", "")


def _new_code(prefix: str = "", length: int = 6) -> str:
    body = "".join(random.choices(_CODE_ALPHABET, k=length))
    return f"{prefix}{body}" if prefix else body


def _maybe_fail(endpoint: str) -> None:
    """Simulate a flaky downstream.

    Raises 503 with the message the agent's tool layer is expected to
    translate into a user-facing apology, or stalls past the client's read
    timeout — a different failure path that should degrade identically.
    """
    if _chaos["scope"] != "all" and endpoint != _chaos["scope"]:
        return
    if random.random() >= _chaos["rate"]:
        return
    if _chaos["mode"] == "timeout":
        time.sleep(CHAOS_SLEEP_SECONDS)
        return
    raise HTTPException(status_code=503, detail="reservation system unavailable")


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _get_flight_or_404(flight_no: str) -> dict:
    flight = _flights.get(_norm_flight_no(flight_no))
    if flight is None:
        raise HTTPException(status_code=404, detail=f"No flight {flight_no}")
    return flight


def _get_booking_or_404(booking_ref: str) -> dict:
    booking = _bookings.get(_norm_ref(booking_ref))
    if booking is None:
        raise HTTPException(status_code=404, detail=f"No booking {booking_ref}")
    return booking


def _booking_view(booking: dict) -> dict:
    """A booking plus the live flight it points at, so the agent gets the
    itinerary in one call instead of chaining two."""
    view = deepcopy(booking)
    flight = _flights.get(booking["flight_no"])
    view["flight"] = deepcopy(flight) if flight else None
    return view


# --------------------------------------------------------------------------
# request models
# --------------------------------------------------------------------------


class RebookRequest(BaseModel):
    booking_ref: str = Field(..., examples=["K7QM2P"])
    new_flight_no: str = Field(..., examples=["UA118"])


class VoucherRequest(BaseModel):
    booking_ref: str = Field(..., examples=["K7QM2P"])
    type: Literal["meal", "hotel"]


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "flights": len(_flights),
        "bookings": len(_bookings),
        "chaos": dict(_chaos),
    }


@app.get("/flights/{flight_no}")
def get_flight(flight_no: str) -> dict:
    """Current status, times and gate for one flight."""
    _maybe_fail("flights")
    return _get_flight_or_404(flight_no)


@app.get("/bookings/{booking_ref}")
def get_booking(booking_ref: str) -> dict:
    """Passenger, fare class, seat, and the flight they're currently on."""
    _maybe_fail("bookings")
    return _booking_view(_get_booking_or_404(booking_ref))


@app.get("/alternates")
def find_alternates(
    origin: str = Query(..., examples=["IAD"]),
    dest: str = Query(..., examples=["DEN"]),
    arrive_by: str | None = Query(
        None,
        description=(
            "Optional ISO 8601 cutoff, e.g. 2026-08-18T02:00:00-06:00. "
            "Without an offset it is read as local time at the destination."
        ),
        examples=["2026-08-18T02:00:00-06:00"],
    ),
) -> dict:
    """Flights on a route that a passenger could be moved to.

    Sold-out flights are returned with seats_available: 0 rather than filtered
    out — the agent should be able to say "the 7:30 is full" instead of
    silently pretending it doesn't exist.
    """
    _maybe_fail("alternates")

    cutoff = None
    if arrive_by:
        try:
            cutoff = _parse_dt(arrive_by)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"arrive_by must be ISO 8601, got {arrive_by!r}",
            )

    origin_u, dest_u = origin.strip().upper(), dest.strip().upper()
    matches = []
    for flight in _flights.values():
        if flight["origin"] != origin_u or flight["dest"] != dest_u:
            continue
        if flight["status"] == "CANCELLED":
            continue
        arrival = _parse_dt(flight["scheduled_arrival"])
        if cutoff is not None:
            # A naive cutoff means the passenger said "by 11pm" — compare
            # against the arrival's wall-clock time at the destination.
            left = arrival if cutoff.tzinfo else arrival.replace(tzinfo=None)
            if left > cutoff:
                continue
        matches.append((arrival, flight))

    matches.sort(key=lambda pair: pair[0])
    return {
        "origin": origin_u,
        "dest": dest_u,
        "arrive_by": arrive_by,
        "count": len(matches),
        "alternates": [deepcopy(flight) for _, flight in matches],
    }


@app.post("/rebook")
def rebook(req: RebookRequest) -> dict:
    """Move a booking onto a new flight. Decrements that flight's seats.

    Refuses cancelled and sold-out flights — the seat check is here, in the
    system of record, not only in the agent's prompt.
    """
    booking = _get_booking_or_404(req.booking_ref)
    new_flight = _get_flight_or_404(req.new_flight_no)

    if new_flight["flight_no"] == booking["flight_no"]:
        raise HTTPException(
            status_code=409,
            detail=f"Booking {booking['booking_ref']} is already on {new_flight['flight_no']}",
        )
    if new_flight["status"] == "CANCELLED":
        raise HTTPException(
            status_code=409,
            detail=f"Flight {new_flight['flight_no']} is cancelled and cannot be booked",
        )
    if new_flight["seats_available"] <= 0:
        raise HTTPException(
            status_code=409,
            detail=f"Flight {new_flight['flight_no']} is sold out",
        )

    previous_flight_no = booking["flight_no"]
    new_flight["seats_available"] -= 1
    booking["flight_no"] = new_flight["flight_no"]
    booking["previous_flight_no"] = previous_flight_no
    booking["seat"] = "UNASSIGNED"
    booking["status"] = "REBOOKED"
    booking["confirmation_code"] = _new_code()

    return {
        "booking_ref": booking["booking_ref"],
        "confirmation_code": booking["confirmation_code"],
        "passenger": booking["passenger"],
        "previous_flight_no": previous_flight_no,
        "new_flight": deepcopy(new_flight),
        "seat": booking["seat"],
        "rebooked_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/vouchers")
def issue_voucher(req: VoucherRequest) -> dict:
    """Issue a meal or hotel voucher against a booking.

    Idempotent per (booking, type): a retry — or a confused agent asking twice
    — gets the same code back with reissued: true, not a second voucher.
    """
    booking = _get_booking_or_404(req.booking_ref)
    key = (booking["booking_ref"], req.type)

    existing = _vouchers.get(key)
    if existing is not None:
        return {**deepcopy(existing), "reissued": True}

    voucher = {
        "voucher_code": _new_code(prefix=VOUCHER_PREFIXES[req.type]),
        "booking_ref": booking["booking_ref"],
        "type": req.type,
        "amount_usd": VOUCHER_AMOUNTS_USD[req.type],
        "passenger": booking["passenger"],
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    _vouchers[key] = voucher
    return {**deepcopy(voucher), "reissued": False}


class ChaosRequest(BaseModel):
    rate: float = Field(0.0, ge=0.0, le=1.0)
    mode: Literal["error", "timeout"] = "error"
    scope: Literal["alternates", "flights", "bookings", "all"] = "alternates"


@app.post("/admin/chaos")
def set_chaos(req: ChaosRequest) -> dict:
    """Point failures at a specific endpoint for the duration of one test."""
    _chaos.update(rate=req.rate, mode=req.mode, scope=req.scope)
    return dict(_chaos)


@app.post("/admin/reset")
def reset() -> dict:
    """Restore seed state and clear any injected chaos."""
    _load_seed()
    _chaos.update(rate=CHAOS_RATE, mode="error", scope="alternates")
    return {"status": "reset", "flights": len(_flights), "bookings": len(_bookings)}
