"""Adversarial tests against the identity gate. No LLM involved."""

import httpx
from google.genai import types

from mayday.agent import (
    ATTEMPTS_KEY,
    VERIFIED_KEY,
    confirm_rebook,
    get_booking,
    issue_voucher,
    propose_rebook,
    verify_identity,
)

httpx.post("http://localhost:8001/admin/reset")


class FakeCtx:
    def __init__(self, said="", invocation_id="t1", state=None):
        self.state = {} if state is None else state
        self.invocation_id = invocation_id
        self.user_content = types.Content(role="user", parts=[types.Part(text=said)])


PASS = FAIL = 0


def check(name, actual, expected, note=""):
    global PASS, FAIL
    ok = actual == expected
    PASS, FAIL = PASS + (1 if ok else 0), FAIL + (0 if ok else 1)
    print(f"  [{'PASS' if ok else '*** FAIL ***'}] {name}")
    if note:
        print(f"           {note}")


print("=== unverified session must be refused ===")

ctx = FakeCtx()
r = get_booking("K7QM2P", ctx)
check("get_booking without verifying", r.get("permitted"), False)
check("  and leaks no passenger name", "Ibrahim" in str(r), False)

ctx = FakeCtx()
r = issue_voucher("K7QM2P", "meal", ctx)
check("issue_voucher without verifying", r.get("permitted"), False)

ctx = FakeCtx()
r = propose_rebook("K7QM2P", "UA118", ctx)
check("propose_rebook without verifying", r.get("permitted"), False)

ctx = FakeCtx()
r = confirm_rebook("K7QM2P", "UA118", "yes book it", ctx)
check("confirm_rebook without verifying", r.get("permitted"), False)

print("\n=== verification itself ===")

ctx = FakeCtx()
r = verify_identity("K7QM2P", "Khan", ctx)
check("correct reference and name", r.get("verified"), True, str(r.get("passenger")))

ctx = FakeCtx()
r = verify_identity("K7QM2P", "khan", ctx)
check("last name is case-insensitive", r.get("verified"), True)

ctx = FakeCtx()
r = verify_identity("K7QM2P", "Smith", ctx)
wrong_name_msg = r.get("reason")
check("right reference, wrong name", r.get("verified"), False)

ctx = FakeCtx()
r = verify_identity("ZZZZZZ", "Smith", ctx)
check("reference that does not exist", r.get("verified"), False)
check(
    "  same message either way (no enumeration oracle)",
    r.get("reason"),
    wrong_name_msg,
)

print("\n=== verification is scoped to one booking ===")

ctx = FakeCtx()
verify_identity("K7QM2P", "Khan", ctx)
r = get_booking("R4ZT9L", ctx)
check("verified K7QM2P, then read R4ZT9L", r.get("permitted"), False)
check("  and leaks no other passenger", "Alvarez" in str(r), False)
r = propose_rebook("R4ZT9L", "UA118", ctx)
check("verified K7QM2P, then rebook R4ZT9L", r.get("permitted"), False)

print("\n=== brute force is capped ===")

ctx = FakeCtx()
for i in range(3):
    verify_identity("K7QM2P", f"Guess{i}", ctx)
r = verify_identity("K7QM2P", "Khan", ctx)
check("correct name after 3 failures", r.get("locked"), True,
      "locked out even though the name was right")

print("\n=== the happy path still works ===")

ctx = FakeCtx()
verify_identity("K7QM2P", "Khan", ctx)
r = get_booking("K7QM2P", ctx)
check("verified read", r.get("passenger"), "Ibrahim Khan")
r = issue_voucher("K7QM2P", "meal", ctx)
check("verified voucher", r.get("issued"), True, str(r.get("voucher_code")))
ctx2 = FakeCtx(said="yes book it", invocation_id="t1", state=ctx.state)
propose_rebook("K7QM2P", "UA118", ctx2)
ctx3 = FakeCtx(said="yes book it", invocation_id="t2", state=ctx.state)
r = confirm_rebook("K7QM2P", "UA118", "yes book it", ctx3)
check("verified rebooking", r.get("rebooked"), True, str(r.get("confirmation_code")))

print(f"\n{PASS} passed, {FAIL} failed")
