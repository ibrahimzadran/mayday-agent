"""Assertion primitives.

Each returns a callable taking a Trace and returning None on success or a
human-readable failure string. Composed into cases in cases.py.

These are hard gates: deterministic, no model involved. If one fails the suite
fails, regardless of how good the prose was.
"""

import re
from typing import Callable, Optional

import httpx

from evals.trace import Trace

Assertion = Callable[[Trace], Optional[str]]

BACKEND = "http://localhost:8001"

# Field names, JSON fragments and internal identifiers that should never reach
# a passenger. Checked as a group because a leak of any one of them is the
# same failure.
_LEAK_PATTERNS = [
    r"sold_out",
    r"seats_available",
    r"booking_ref\"",
    r"flight_no\"",
    r"\{\s*[\"']",
    r"Traceback",
    r"httpx",
    r"status_code",
    r"\b(get_flight_status|get_booking|find_alternate_flights|propose_rebook|confirm_rebook|issue_voucher|search_policy)\b",
]


def called(name: str) -> Assertion:
    def check(trace: Trace) -> Optional[str]:
        if not trace.called(name):
            return f"expected {name} to be called; called {trace.tool_names or 'nothing'}"
        return None
    return check


def not_called(name: str) -> Assertion:
    def check(trace: Trace) -> Optional[str]:
        if trace.called(name):
            return f"{name} must NOT have been called, but was {len(trace.calls_to(name))}x"
        return None
    return check


def called_before(first: str, second: str) -> Assertion:
    """Ordering matters when one tool's output is the other's input."""
    def check(trace: Trace) -> Optional[str]:
        names = trace.tool_names
        if first not in names:
            return f"{first} was never called"
        if second not in names:
            return f"{second} was never called"
        if names.index(first) > names.index(second):
            return f"{first} must be called before {second}; order was {names}"
        return None
    return check


def tool_arg_matches(name: str, key: str, pattern: str) -> Assertion:
    def check(trace: Trace) -> Optional[str]:
        calls = trace.calls_to(name)
        if not calls:
            return f"{name} was never called"
        for call in calls:
            if re.search(pattern, str(call.args.get(key, "")), re.IGNORECASE):
                return None
        return f"no {name} call had {key} matching /{pattern}/; saw {[c.args.get(key) for c in calls]}"
    return check


def final_contains(*needles: str) -> Assertion:
    """Every needle must appear. Case-insensitive."""
    def check(trace: Trace) -> Optional[str]:
        text = trace.final_text.lower()
        missing = [n for n in needles if n.lower() not in text]
        if missing:
            return f"final reply missing {missing}"
        return None
    return check


def final_contains_any(*needles: str) -> Assertion:
    def check(trace: Trace) -> Optional[str]:
        text = trace.final_text.lower()
        if any(n.lower() in text for n in needles):
            return None
        return f"final reply contained none of {list(needles)}"
    return check


def text_lacks(*needles: str) -> Assertion:
    """Checked across every reply, not just the last — a leak anywhere counts."""
    def check(trace: Trace) -> Optional[str]:
        text = trace.all_text.lower()
        found = [n for n in needles if n.lower() in text]
        if found:
            return f"reply contained forbidden {found}"
        return None
    return check


def no_internal_leak() -> Assertion:
    def check(trace: Trace) -> Optional[str]:
        hits = [p for p in _LEAK_PATTERNS if re.search(p, trace.all_text)]
        if hits:
            return f"internal detail leaked to passenger, matched {hits}"
        return None
    return check


def booking_is(booking_ref: str, flight_no: str) -> Assertion:
    """Reads the airline's own state. The only assertion that proves a
    rebooking really did or did not happen — the agent's prose is not
    evidence."""
    def check(trace: Trace) -> Optional[str]:
        try:
            data = httpx.get(f"{BACKEND}/bookings/{booking_ref}", timeout=5).json()
        except Exception as exc:
            return f"could not read booking {booking_ref}: {exc}"
        actual = data.get("flight_no")
        if actual != flight_no:
            return f"booking {booking_ref} is on {actual}, expected {flight_no}"
        return None
    return check


def voucher_issued(booking_ref: str, voucher_type: str) -> Assertion:
    def check(trace: Trace) -> Optional[str]:
        calls = [c for c in trace.calls_to("issue_voucher")
                 if str(c.args.get("voucher_type", "")).lower() == voucher_type]
        if not calls:
            return f"no {voucher_type} voucher was issued"
        return None
    return check


def ran_without_error() -> Assertion:
    def check(trace: Trace) -> Optional[str]:
        return f"run failed: {trace.error}" if trace.error else None
    return check
