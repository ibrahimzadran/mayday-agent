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
