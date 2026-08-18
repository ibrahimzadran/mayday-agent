"""Pretend to be Twilio, from the terminal.

    python -m sms.simulate "+15551234567" "my flight UA482 was cancelled"
    python -m sms.simulate "+15551234567"           # interactive

Signs requests the way Twilio does, so this exercises the real validation path
rather than skipping it — a simulator that bypasses the security check tests
the one thing least worth trusting.

Waits afterwards for anything the service sends out of band, so a reply that
missed the webhook deadline shows up here the way it would arrive on a phone.
"""

import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv("mayday/.env")

SERVICE = os.getenv("MAYDAY_SMS_URL", "http://localhost:8003")
WEBHOOK = f"{SERVICE}/sms"
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

DIM, BOLD, BLUE, GREY, OFF = "\033[2m", "\033[1m", "\033[34m", "\033[90m", "\033[0m"


def _signature(url: str, params: dict) -> str:
    from twilio.request_validator import RequestValidator

    return RequestValidator(AUTH_TOKEN).compute_signature(url, params)


def _outbox_count() -> int:
    try:
        return httpx.get(f"{SERVICE}/outbox", timeout=5).json()["total"]
    except Exception:
        return 0


def _drain_outbox(since: int, wait: float = 25.0) -> None:
    """Show anything delivered after the webhook already answered."""
    deadline = time.time() + wait
    seen = since
    while time.time() < deadline:
        try:
            data = httpx.get(f"{SERVICE}/outbox", params={"since": seen}, timeout=5).json()
        except Exception:
            return
        for msg in data["messages"]:
            state = "delivered" if msg["delivered"] else (msg["error"] or "not sent")
            print(f"\n{BLUE}  ← (follow-up){OFF} {msg['body']}")
            print(f"{GREY}     [{state}]{OFF}")
            seen = data["total"]
        if seen >= data["total"] and seen > since:
            return
        time.sleep(1.0)


def send(phone: str, body: str) -> None:
    params = {"From": phone, "Body": body}
    headers = {}
    if AUTH_TOKEN:
        headers["X-Twilio-Signature"] = _signature(WEBHOOK, params)

    before = _outbox_count()
    print(f"{BOLD}  → {OFF}{body}")
    started = time.time()
    try:
        r = httpx.post(WEBHOOK, data=params, headers=headers, timeout=40)
    except Exception as exc:
        print(f"  !! could not reach {WEBHOOK}: {exc}")
        return

    if r.status_code != 200:
        print(f"  !! HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code == 403:
            print("     signature rejected — check TWILIO_AUTH_TOKEN and MAYDAY_PUBLIC_URL")
        if r.status_code == 503:
            print("     no TWILIO_AUTH_TOKEN set, and unsigned requests are not allowed")
        return

    import re

    match = re.search(r"<Message>(.*?)</Message>", r.text, re.S)
    reply = match.group(1) if match else "(empty response)"
    elapsed = time.time() - started
    print(f"{BLUE}  ← {OFF}{reply}")
    print(f"{GREY}     [{len(reply)} chars, {elapsed:.1f}s]{OFF}")

    # Only an acknowledgement means the real answer is still coming.
    if "Searching now" in reply:
        print(f"{GREY}     waiting for the real answer…{OFF}")
        _drain_outbox(before)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    phone = sys.argv[1]
    print(f"{DIM}phone {phone} -> {WEBHOOK}"
          f"{'  (signed)' if AUTH_TOKEN else '  (UNSIGNED — needs MAYDAY_SMS_ALLOW_UNSIGNED=1)'}{OFF}\n")

    if len(sys.argv) > 2:
        send(phone, " ".join(sys.argv[2:]))
        return 0

    print(f"{DIM}type a message, or ctrl-c to stop{OFF}")
    while True:
        try:
            body = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if body:
            send(phone, body)


if __name__ == "__main__":
    sys.exit(main())
