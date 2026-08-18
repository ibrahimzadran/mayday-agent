# Mayday ✈️ — Flight-Disruption Rescue Agent

An AI agent that rescues stranded passengers: checks flight status, finds
alternates, rebooks with an explicit consent gate, and issues vouchers.
Built on Google ADK.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# API key: copy mayday/.env.example to mayday/.env
# and paste your key from https://aistudio.google.com

```

Two processes, two terminals — each needs `source .venv/bin/activate` first:

```bash
# terminal 1 — the fake airline API
uvicorn backend.main:app --reload --port 8001

# terminal 2 — the agent
adk web                      # http://localhost:8000
```

Try: `my flight UA482 just got cancelled, find me alternates to DEN`

## The fake airline API

A stand-in for an airline's reservation system, so the agent talks to a real
HTTP service instead of a dict in its own process.

| Endpoint | Purpose |
| --- | --- |
| `GET /flights/{flight_no}` | status, times, gate |
| `GET /bookings/{booking_ref}` | passenger, fare class, seat, current flight |
| `GET /alternates?origin=&dest=&arrive_by=` | rebooking candidates, sold-out ones included |
| `POST /rebook` | move a booking, decrement seats, return a confirmation code |
| `POST /vouchers` | meal or hotel voucher, idempotent per booking+type |
| `POST /admin/reset` | restore seed state |

## Evals

```bash
python -m evals                    # everything
python -m evals --fast             # the subset CI runs on every push
python -m evals --case consent-hedge --verbose
PYTHONPATH=. python tests_consent_gate.py   # gate unit tests, no model needed
```

Cases run against the real agent, real tools and the real backend — nothing is
mocked, because a suite that passes against mocks only proves the mocks agree
with each other. Each case resets the backend first and may inject a specific
failure through `POST /admin/chaos`.

Two kinds of check, deliberately unequal:

- **Deterministic assertions are hard gates.** Which tools were called, in what
  order, with what arguments, and — the only one that actually proves a booking
  did or did not happen — what the airline's own state says afterwards. The
  agent's prose is not evidence.
- **An LLM judge scores tone and helpfulness 1-5**, and is advisory. A judge is
  itself a model; making it a gate makes the suite flaky, and a flaky suite
  teaches you to ignore red.

The judge runs on a different model from the agent, because free-tier quota is
per model and sharing one halves how many cases a run completes.

## MCP

Flight lookups, bookings, alternates, vouchers and policy search are served by
an MCP server (`mcp_server/server.py`) that the agent spawns over stdio. The
agent does not import those functions; it discovers them over the protocol, so
any MCP client can use them.

```bash
python -m mcp_server.server     # standalone, for testing with any MCP client
```

**Rebooking is deliberately not exposed over MCP.** An MCP server answers any
client that speaks the protocol, so a safety control living in one client is
not a control — another client could call `rebook` directly and skip the
consent gate entirely. The gate also cannot cross the boundary intact: its
strongest check reads the passenger's real words from the ADK session, and a
separate process has no session to read. It could only be handed a string
asserting what the passenger said, which is exactly the claim the check exists
to distrust.

So the split is by trust boundary rather than convenience: MCP carries data
access and reversible actions, and the one irreversible action stays in-process
beside the conversation it depends on. Both paths share
`mayday/airline_client.py`, so error shapes cannot drift apart.

## Policy answers (RAG)

`policies/` holds ten markdown documents: Meridian Airways fare, rebooking,
cancellation, delay and voucher policy, plus plain-language summaries of
EU261, UK261, US DOT refund rules, extraordinary circumstances, and escalation
rules. Each clause is numbered so answers can cite one.

Build the index once, then it is cached on disk:

```bash
python -m mayday.policy_index      # chunks, embeds, writes policies/.index.*
```

Chunks split on level-2 headings, not fixed windows, because these documents
are already organised by clause and a window would cut a compensation table in
half. Embeddings are `gemini-embedding-001` truncated to 768 dimensions and
normalized, searched by brute-force cosine similarity over numpy — 43 chunks
does not need a vector database, and this has no server, no schema and no
version skew.

`search_policy(query)` returns the top three chunks with citations. The agent
answers only from them and hands off if they do not cover the question.

## Web channel

```bash
uvicorn web.app:app --reload --port 8002    # then open http://localhost:8002
```

A signed-in "My Trip" page for one booking, with the agent behind a chat
panel on it.

**DOM hints.** The page already knows which trip it is showing, so the server
folds that context into the conversation before the first message. The
passenger opens the chat and the agent already knows the flight, the route and
that it was cancelled — no reciting a booking reference into a chat box on
your own trip page.

Two rules make that safe:

- **The browser never sends a booking reference.** It sends an opaque token
  minted when the page rendered, and the server resolves it. A hint the client
  could forge is not a hint, it is an authorization bypass.
- **The hint never carries the last name.** That is the credential
  `verify_identity` checks, and a hint containing it lets the model satisfy the
  check with material the check exists to test for. Identity comes from seeded
  session state the browser cannot reach.

The consent gate is unchanged by the channel: a hedge still stages and asks, and
only an explicit yes books.

## Identity gate

Nothing scoped to a passenger — reading a booking, issuing a voucher against
it, rebooking it — happens until this conversation has called
`verify_identity` with the booking reference **and** the last name on it.

- Verification covers **one** booking. Asking about a second reference
  requires verifying that one too, with its own last name.
- A failed check returns the same message whether the reference does not
  exist or the name was wrong. Distinguishing them would turn the tool into
  an oracle for which references are real, which is most of the work of
  guessing one.
- Three failures locks the conversation out, because a six-character
  reference plus a guessable surname is weak against a channel that never
  gets tired.
- Flight status, schedules and policy stay open — they belong to nobody.

    python -m pytest tests_identity_gate.py   # or: python tests_identity_gate.py

## Consent gate

Rebooking is irreversible, so it is split into two tools: `propose_rebook`
stages an option, `confirm_rebook` executes it. `confirm_rebook` refuses
unless all five hold:

1. an option was staged for this passenger
2. it is the same booking and the same flight they were asked about
3. it was staged in an **earlier** turn, so the passenger had a chance to reply
4. the confirmation quoted by the model **appears verbatim** in what the
   passenger actually typed, read from session state rather than trusted
5. those words are an unambiguous yes — not a hedge, question, or refusal

Prompt rules alone cannot guarantee any of this; the checks live in code and
the pending option lives in session state.

    python -m pytest tests_consent_gate.py   # or: python tests_consent_gate.py

Identity is checked before consent: agreeing to a booking you do not own is
not consent to anything.

Seeded with 16 flights over 5 routes and 8 bookings. `UA482 IAD→DEN` is
cancelled; `UA905`, `DL388` and `AA88` are sold out.

`GET /alternates` returns `503 reservation system unavailable` about 20% of the
time on purpose, so the agent has to handle a failing dependency. Set
`MAYDAY_CHAOS_RATE=0` to turn it off (read at startup).

Interactive docs while it runs: http://localhost:8001/docs

## Layout

```
mayday/        the ADK agent, the consent gate, the policy index
mcp_server/    MCP server exposing the airline's read tools
backend/       the fake airline API (FastAPI)
policies/      policy and passenger-rights documents
LEARNINGS.md   engineering log
```
