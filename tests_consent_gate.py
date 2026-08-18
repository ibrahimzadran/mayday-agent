"""Adversarial tests against the consent gate. No LLM involved."""
from google.genai import types
import httpx
from mayday.agent import propose_rebook, confirm_rebook, PENDING_KEY
httpx.post("http://localhost:8001/admin/reset")  # known seed state every run

class FakeCtx:
    """Only the three attributes the tools actually touch."""
    def __init__(self, said="", invocation_id="turn-1", state=None):
        self.state = {} if state is None else state
        self.invocation_id = invocation_id
        self.user_content = types.Content(role="user", parts=[types.Part(text=said)])

PASS = FAIL = 0
def check(name, booked, expected_booked, note=""):
    global PASS, FAIL
    ok = booked == expected_booked
    globals().__setitem__('PASS', PASS + (1 if ok else 0))
    globals().__setitem__('FAIL', FAIL + (0 if ok else 1))
    verdict = "PASS" if ok else "*** FAIL ***"
    print(f"  [{verdict}] {name}")
    if note:
        print(f"           {note}")

print("=== attacks that must NOT book ===")

ctx = FakeCtx(said="sure")
r = confirm_rebook("K7QM2P", "UA118", "sure", ctx)
check("bare 'sure' with nothing staged", r.get("rebooked"), False, r.get("reason","")[:80])

ctx = FakeCtx(said="hmm the 9:40 could work", invocation_id="t1")
propose_rebook("K7QM2P", "UA118", ctx)
ctx2 = FakeCtx(said="hmm the 9:40 could work", invocation_id="t2", state=ctx.state)
r = confirm_rebook("K7QM2P", "UA118", "the 9:40 could work", ctx2)
check("hedge: 'could work'", r.get("rebooked"), False, r.get("reason","")[:80])

ctx = FakeCtx(said="what about the later one?", invocation_id="t1")
propose_rebook("K7QM2P", "UA118", ctx)
ctx2 = FakeCtx(said="what about the later one?", invocation_id="t2", state=ctx.state)
r = confirm_rebook("K7QM2P", "UA118", "what about the later one", ctx2)
check("question, not instruction", r.get("rebooked"), False, r.get("reason","")[:80])

ctx = FakeCtx(said="yes book it", invocation_id="t1")
propose_rebook("K7QM2P", "UA118", ctx)
ctx2 = FakeCtx(said="yes book it", invocation_id="t2", state=ctx.state)
r = confirm_rebook("K7QM2P", "UA77", "yes book it", ctx2)
check("consent to UA118 reused for UA77", r.get("rebooked"), False, r.get("reason","")[:80])

ctx = FakeCtx(said="what are my options?", invocation_id="t1")
propose_rebook("K7QM2P", "UA118", ctx)
ctx2 = FakeCtx(said="what are my options?", invocation_id="t2", state=ctx.state)
r = confirm_rebook("K7QM2P", "UA118", "yes please book that", ctx2)
check("model fabricates a quote", r.get("rebooked"), False, r.get("reason","")[:80])

ctx = FakeCtx(said="yes book it", invocation_id="same-turn")
propose_rebook("K7QM2P", "UA118", ctx)
r = confirm_rebook("K7QM2P", "UA118", "yes book it", ctx)
check("propose+confirm in one turn", r.get("rebooked"), False, r.get("reason","")[:80])

ctx = FakeCtx(said="no dont book it", invocation_id="t1")
propose_rebook("K7QM2P", "UA118", ctx)
ctx2 = FakeCtx(said="no dont book it", invocation_id="t2", state=ctx.state)
r = confirm_rebook("K7QM2P", "UA118", "no dont book it", ctx2)
check("explicit refusal containing 'book it'", r.get("rebooked"), False, r.get("reason","")[:80])

ctx = FakeCtx(said="actually not that one", invocation_id="t1")
propose_rebook("K7QM2P", "UA118", ctx)
ctx2 = FakeCtx(said="actually not that one", invocation_id="t2", state=ctx.state)
r = confirm_rebook("K7QM2P", "UA118", "actually not that one", ctx2)
check("change of mind", r.get("rebooked"), False, r.get("reason","")[:80])

print("\n=== the one that MUST book ===")
ctx = FakeCtx(said="yes, book the UA118 please", invocation_id="t1")
staged = propose_rebook("K7QM2P", "UA118", ctx)
ctx2 = FakeCtx(said="yes, book the UA118 please", invocation_id="t2", state=ctx.state)
r = confirm_rebook("K7QM2P", "UA118", "yes, book the UA118 please", ctx2)
check("explicit yes after staging", r.get("rebooked"), True, str(r.get("confirmation_code") or r.get("reason"))[:80])
print(f"           consent cleared after use: {ctx2.state.get(PENDING_KEY) is None}")

print("\n=== replay: same yes, second time ===")
r2 = confirm_rebook("K7QM2P", "UA118", "yes, book the UA118 please", ctx2)
check("double-booking on a spent consent", r2.get("rebooked"), False, r2.get("reason","")[:80])

print(f"\n{PASS} passed, {FAIL} failed")
