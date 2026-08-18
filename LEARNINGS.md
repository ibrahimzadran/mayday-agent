# LEARNINGS.md — engineering log

> What broke, what surprised me, how it got fixed.

## Environment and setup

- **A nested `mayday/mayday/` folder broke `.env` discovery** → "No API key
  provided". ADK looks for `.env` beside the agent package it loaded, and the
  duplicate nesting meant it was looking one level too deep. Fix: keep exactly
  one `mayday/` at the repo root.

- **Every new terminal needs `source .venv/bin/activate`.** `adk: command not
  found` and `uvicorn: command not found` are almost always this, not a broken
  install. The project runs two processes in two terminals — backend on 8001,
  `adk web` on 8000 — and both need it.

- **A masked API key copied from the AI Studio UI produced a doubled
  `GOOGLE_API_KEY=GOOGLE_API_KEY=…` line** → `API_KEY_INVALID`. The copy button
  included the variable name. Fix: paste the value only. Keys may also start
  with `AQ.` rather than `AIza` — that is a newer format, not a corrupted key.

- **A retired model name returns a 404 that contains its own fix.** Read the
  full error body rather than only the status code.

- **`.env` changes are only read at startup.** Restart `adk web` after editing
  it; the reloader does not pick it up.

- **zsh ties the lowercase variable `path` to `$PATH`.** A shell helper using
  `path="$2"` silently wiped PATH and produced `command not found: curl` for
  everything afterwards. Fix: never use `path` as a scratch variable in zsh.

## Model quotas

- **Gemini free tier enforces two stacked quotas, per model: 5 requests per
  minute and 20 per day.** One agent turn costs one request per tool
  round-trip, so roughly six conversations exhaust a day on `gemini-3.6-flash`.
  Fix: the model is selectable via `MAYDAY_MODEL`, and a `-lite` model — which
  has its own quota bucket — is used for development and evals.

- **`retryDelay` in a 429 lies about daily quotas.** It said "retry in 59s"
  while reporting `GenerateRequestsPerDayPerProjectPerModel-FreeTier`. Read
  `quotaId`: `PerMinute` clears in a minute, `PerDay` does not clear until
  midnight Pacific.

- **Put the eval judge on a different model from the agent.** Quota is per
  model, so sharing one halves how many cases a run completes before hitting
  the per-minute cap. Independence from the model under test is a bonus, not
  the reason.

## The consent gate

- **Pattern-matching for consent booked a refusal.** `"no dont book it"`
  contains `"book it"`, which was in the affirmation regex, so the gate read a
  refusal as a yes and rebooked the passenger. Cause: matching for consent
  without independently checking for negation. Fix: a separate `_NEGATE` check
  evaluated before the affirmation match. Lesson: a permit-list of consent
  phrases needs a deny-list beside it, and substring matching on natural
  language is unsafe by default.

- **A safety gate's false negatives show up as UX friction, not as errors.**
  Requiring confirmation in a later turn than the proposal is correct, but when
  the model failed to stage on the hedging turn, the passenger's "yes book it"
  got spent on staging and they had to agree twice. Fixed by making the model
  stage earlier, not by weakening the gate — the check was right, the
  sequencing was wrong.

- **The tool surface is a stronger control than the system prompt.** Breaking
  the consent rules in the prompt did not produce a wrong booking. Deleting
  every check inside `confirm_rebook` did not either. It only broke when
  `propose_rebook` was unregistered, leaving a single booking tool — then
  "hmm the 9:40 could work" booked a flight. An instruction can be overridden;
  the fact that booking requires calling a second, differently-named tool
  cannot.

## The identity gate

- **A leak that cannot be fixed where the data lives.** `get_booking` was on
  the MCP server, which has no session and therefore cannot know whether this
  conversation proved anything. The fix was to move everything scoped to a
  passenger back into the agent process and leave only impersonal data —
  flight status, schedules, policy — on MCP. The boundary turned out to be
  personal data, not convenience.

- **A failed identity check must not say which half was wrong.** Returning
  "no such booking" for a bad reference and "wrong name" for a bad name makes
  the tool an oracle for which references exist, which is most of the work of
  guessing one. Both return the same sentence.

- **The judge marked a correct answer 1/5.** It claimed "those details do not
  match our records" confirmed the booking existed. It does not — a
  nonexistent reference returns that exact string. The rubric had described
  the failure to avoid without describing what success looks like. This is
  why the judge is advisory and deterministic assertions are the gates.

- **An assertion that tests a path instead of an outcome will break on a
  correct change.** A case required `get_flight_status` before reporting a
  flight full, but `propose_rebook` validates seats itself, so a legitimate
  route through the code failed the test. Assert what the passenger ends up
  with, not which tools were used to get there.

- **Bad news is not an answer.** Told a flight was full, the agent stopped
  there rather than looking up alternates. Caught by the judge rather than by
  an assertion — tone rubrics find gaps that tool-call checks cannot express.

## The web channel

- **A page hint that carries the credential defeats the check it is meant to
  skip.** The first DOM hint included the passenger's full name so the agent
  could address them properly. The agent promptly called `verify_identity`
  using the surname it had read from that hint — passing an identity check with
  material the page had handed it. Fix: the hint carries the first name only,
  and identity comes from session state the browser cannot reach. A hint may
  carry context; it must never carry the answer to a question the system asks
  to establish trust.

- **Trust comes from the channel, not from the message.** The browser sends an
  opaque token minted when the page rendered, never a booking reference. If the
  client can name the booking it wants context for, the hint stops being
  context and becomes an authorization bypass.

- **A second service inherits the wrong model default.** The web app imported
  the agent without `MAYDAY_MODEL` set, so it ran on the 20-per-day model and
  returned rate-limit apologies while the eval suite next to it ran fine on the
  lite model. Each process needs the environment set, not just the one being
  actively worked on.

## Retrieval

- **Query specificity drove retrieval quality more than any indexing choice.**
  "am I owed anything" ranked *when compensation is NOT owed* first. The same
  question carrying the route and the disruption reason pulled exactly the
  clauses needed to answer. Fix: the prompt requires gathering the flight and
  its recorded reason before searching. Tuning chunk size or `k` would not
  have fixed this.

- **Pin the embedding model.** A silently upgraded model leaves cached vectors
  that no longer compare to fresh queries. Nothing raises; retrieval just
  quietly gets worse.

- **Chunk on headings, not fixed windows,** when the documents are already
  organised by clause. A fixed window cut a compensation table in half, and
  half a table retrieves just as confidently as a whole one while being wrong.

## MCP

- **`mcp` 2.0 broke ADK 2.7, silently.** ADK imports `mcp.shared.session`,
  removed in the 2.0 restructure. ADK catches the `ImportError` and logs it at
  debug level, so `google.adk.tools.mcp_tool` simply exports nothing — no
  error, no traceback, just missing names. Diagnosed by importing the submodule
  directly to surface the real exception. Fix: pin `mcp>=1.9,<2`.

- **A stdio MCP server must never write to stdout** — that is the protocol
  channel. Diagnostics go to stderr.

- **Some controls cannot cross a process boundary.** The consent gate's
  strongest check reads the passenger's real words from the ADK session. A
  separate process has no transcript, so it could only be handed a string
  claiming what was said — the exact assertion the check exists to distrust.
  Moving it to MCP would have preserved the shape of the gate while degrading
  it to trusting the caller. It stayed in-process.

## Evals

- **`genai.Client().models.generate_content(...)` fails with "Cannot send a
  request, as the client has been closed."** The `Client` is a temporary and
  `.models` does not keep it alive, so garbage collection closes its httpx pool
  before the request is sent. Fix: hold the client at module level. The error
  names httpx, which sends you looking in entirely the wrong place.

- **An assertion can be the thing that is wrong.** A case failed on
  `not_called("propose_rebook")` for a sold-out flight — but staging a sold-out
  flight is correct, because the tool checks seats and refuses. The assertion
  encoded "don't try" when the requirement was "don't book". A red eval means
  something disagrees; it does not say which side is mistaken.

- **A throttled case is not a failing case.** It never ran. Reporting it as a
  failure trains you to ignore red, which is the one thing a suite cannot
  survive. The runner retries rate-limited cases and reports them separately.

- **Assert against the system of record, not the agent's prose.** The only
  assertion that proves a rebooking did or did not happen reads the booking
  back from the airline API. An agent saying "I have not booked anything" is
  not evidence.

- **Swallowing an exception type without its message costs hours.** Both ADK's
  MCP import and my own eval judge hid the useful part of the error. Log the
  message, not just `type(exc).__name__`.

## Operations

- **Latency is essentially all model time.** Tool calls measured 2–22 ms
  against LLM calls of 600–1200 ms, with one outlier at **17.1 s**. Relevant to
  Phase 6: Twilio expects a webhook reply within 10 s, so the async
  "searching now…" pattern is required, not optional.

- **`git add -A` swept `adk web`'s local run artifacts into a commit.** Add
  generated state to `.gitignore` before the first broad add, not after.
