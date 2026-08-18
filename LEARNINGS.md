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

- **Refreshing stale UI is not the same as making it correct.** After a
  rebooking the card updated but the banner above it still read "your flight
  UA482 was cancelled" — because the replacement only ran when the new flight
  was healthy, and the passenger had been moved onto a delayed one. A banner
  that names a flight you are no longer travelling on is worse than no banner.
  It is now rebuilt from the current flight in every case, not only the happy
  one.

- **The double-yes came back through a different door.** The rule said to stage
  an option when the passenger names a flight. It did not cover the agent
  naming one — so "shall I move you to UA1220?" was asked with nothing staged,
  and the passenger's "yes" was spent on staging. Fixed by binding the rule to
  the question rather than to who chose the flight: never end a turn having
  suggested one specific flight without staging it.

- **Internal vocabulary leaks through the seams.** The agent told a passenger
  "I have staged your rebooking". "Staged" is a word from the tool design, not
  from travel. Prompt rules that forbid leaking field names have to cover the
  vocabulary of the mechanism too.

- **A page and the chat on it can contradict each other.** After a rebooking
  the trip card still read CANCELLED, seat 14C, gate C17 while the chat above
  it had just moved the passenger to a different flight and gate. Neither half
  was wrong on its own; together they left no way to know which to believe. The
  widget now re-reads the trip from the server after a confirmed rebooking
  rather than inferring the new state from the reply text.

- **Trust comes from the channel, not from the message.** The browser sends an
  opaque token minted when the page rendered, never a booking reference. If the
  client can name the booking it wants context for, the hint stops being
  context and becomes an authorization bypass.

- **A second service inherits the wrong model default.** The web app imported
  the agent without `MAYDAY_MODEL` set, so it ran on the 20-per-day model and
  returned rate-limit apologies while the eval suite next to it ran fine on the
  lite model. Each process needs the environment set, not just the one being
  actively worked on.

## The SMS channel

- **`asyncio.wait_for` would have thrown away a rebooking.** It cancels the
  task it is waiting on, so hitting Twilio's deadline mid-`confirm_rebook`
  would have aborted an irreversible action already in flight.
  `asyncio.wait({task}, timeout=...)` leaves the task running and only changes
  how the answer is delivered.

- **A channel instruction given once does not stick.** Told at the start of a
  conversation to keep replies short and plain, the model drifts back into
  bold text and bullet lists within a few turns. Prepending the note to every
  turn costs about twenty tokens and holds. On SMS the alternative is several
  billed segments of asterisks.

- **Twilio trial accounts cannot rent a phone number.** The 30-day trial
  covers the API, not number provisioning — buying one requires adding a
  payment method. Also worth separating early: the number you text *from* is
  your own phone and only needs verifying, while `TWILIO_FROM_NUMBER` must be
  a number the account actually owns. Confusing the two produces a config that
  looks complete and cannot send.

- **A health check that counts environment variables proves nothing.**
  `can_send_followups` reported true because three variables were set, while
  the account owned no numbers at all. Presence is not validity; checking
  credentials against the provider is what would have caught it.

- **Errors raised inside an asyncio done-callback disappear.** The
  after-the-deadline send runs there, so a failed delivery is recorded to an
  outbox rather than raised. Recording it also made the path observable
  locally, where there is no phone to receive anything.

- **Signature validation covers the URL Twilio signed, not the one you
  receive.** Behind ngrok those differ, so the public URL has to be configured
  explicitly or every request fails validation.

- **Fail closed on a missing secret.** With no `TWILIO_AUTH_TOKEN` the webhook
  refuses rather than skipping validation, and local testing without one has to
  opt in explicitly. A public endpoint that starts an LLM conversation and can
  move somebody's flight is not a good place for a convenient default.

- **Caller ID is an address, not a credential.** It was tempting to map a phone
  number to a booking and skip verification the way the web page does. Phone
  numbers are spoofable, so the same agent starts from different trust
  depending on which channel the message arrived through — a decision the
  channel makes, not the model.

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

## Tooling

- **Python 3.14 hid a missing import that CI's 3.12 caught immediately.** A
  dropped `from typing import Optional` left `Optional[dict]` referenced in an
  annotation with nothing defining it. Under PEP 649 annotations are evaluated
  lazily, so on 3.14 the module imported fine and every local test passed; on
  3.12 it raised `NameError` at import and the whole suite failed. Fixed by
  restoring the import, and guarded by `tests_annotations.py`, which calls
  `typing.get_type_hints` on everything so annotations resolve eagerly whatever
  interpreter is running. CI now runs both versions, because testing one hides
  bugs the other would catch in both directions.

- **An editing session can delete an import without deleting its use.** The
  import was lost while rewiring the module for MCP; the use was added later,
  by the identity gate. Neither change was wrong on its own, and nothing
  connected them until a different interpreter did.


- **A silent `str.replace` that matches nothing looks exactly like success.** A
  patch failed because the search text contained `\u2026` where the file held a
  literal `…`; the script printed "patched" and changed nothing, and the bug
  surfaced two steps later as a missing JSON key. Assert that the replacement
  landed, the same way ADK's swallowed `ImportError` and the eval judge's
  swallowed exception hid their real causes.

## Operations

- **Latency is essentially all model time.** Tool calls measured 2–22 ms
  against LLM calls of 600–1200 ms, with one outlier at **17.1 s**. Relevant to
  Phase 6: Twilio expects a webhook reply within 10 s, so the async
  "searching now…" pattern is required, not optional.

- **`git add -A` swept `adk web`'s local run artifacts into a commit.** Add
  generated state to `.gitignore` before the first broad add, not after.
