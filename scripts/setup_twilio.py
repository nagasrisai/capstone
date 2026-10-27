"""One-shot Twilio setup for the assistant. Run after filling the credentials in .env:

    python scripts/setup_twilio.py            # configure everything, ask before buying a number
    python scripts/setup_twilio.py --yes      # don't ask
    python scripts/setup_twilio.py --test-call       # ring OWNER_PHONE with a test message
    python scripts/setup_twilio.py --test-whatsapp   # send a test WhatsApp message (after joining the sandbox)

What it does:
  1. Checks the Twilio account (trial or upgraded) and your Anthropic key.
  2. Makes sure you own a voice-capable number (buys one with trial credit if you have none) and
     saves it as TWILIO_VOICE_FROM in .env.
  3. Points that number's "A call comes in" webhook at PUBLIC_BASE_URL/webhooks/voice/inbound.
  4. Checks that OWNER_PHONE is a verified caller ID (trial accounts can only call verified
     numbers) and starts the verification call if it isn't.
  5. Prints the two WhatsApp sandbox steps that Twilio only allows from its web console.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from twilio.base.exceptions import TwilioRestException  # noqa: E402
from twilio.rest import Client  # noqa: E402

from assistant.config import Settings  # noqa: E402

ENV_PATH = ROOT / ".env"


def ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def todo(msg: str) -> None:
    print(f"  [TODO] {msg}")


def fail(msg: str) -> None:
    print(f"  [!!] {msg}")


def write_env(key: str, value: str) -> None:
    """Set KEY=value in .env, replacing an existing line or appending."""
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    line = f"{key}={value}"
    text = pattern.sub(line, text) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"
    ENV_PATH.write_text(text)
    ok(f"wrote {key} to .env")


def check_account(client: Client, settings: Settings) -> bool:
    print("\n1. Account")
    try:
        acct = client.api.accounts(settings.twilio_account_sid).fetch()
    except TwilioRestException as e:
        fail(f"Twilio rejected the credentials: {e.msg}")
        return False
    trial = (acct.type or "").lower() == "trial"
    ok(f"Twilio account '{acct.friendly_name}' ({acct.type})")
    if trial:
        print("     Trial limits: calls only to verified numbers, a short 'trial account' notice at the start of each call,")
        print("     and the WhatsApp sandbox instead of a real sender. All fine for personal use.")
    try:
        bal = client.balance.fetch()
        ok(f"balance: {bal.balance} {bal.currency}")
    except TwilioRestException:
        pass
    if settings.anthropic_api_key and settings.anthropic_api_key.startswith("sk-ant-"):
        ok("ANTHROPIC_API_KEY present")
    else:
        fail("ANTHROPIC_API_KEY missing or malformed in .env (should start with sk-ant-)")
    if not settings.owner_phone.startswith("+"):
        fail("OWNER_PHONE must be in E.164 format, e.g. +919876543210")
        return False
    if "localhost" in settings.public_base_url or not settings.public_base_url.startswith("https://"):
        fail(f"PUBLIC_BASE_URL is '{settings.public_base_url}'. Twilio needs a public https:// URL (ngrok, Fly.io, a VPS...). Set it and rerun.")
    return True


def ensure_voice_number(client: Client, settings: Settings, assume_yes: bool) -> str | None:
    print("\n2. Voice number")
    owned = client.incoming_phone_numbers.list(limit=20)
    voice_capable = [n for n in owned if n.capabilities.get("voice")]
    chosen = None
    if settings.twilio_voice_from:
        match = [n for n in voice_capable if n.phone_number == settings.twilio_voice_from]
        if match:
            chosen = match[0]
            ok(f"TWILIO_VOICE_FROM {chosen.phone_number} is yours and voice-capable")
        else:
            fail(f"TWILIO_VOICE_FROM={settings.twilio_voice_from} is not a voice-capable number on this account")
    if chosen is None and voice_capable:
        chosen = voice_capable[0]
        ok(f"using your existing number {chosen.phone_number}")
        write_env("TWILIO_VOICE_FROM", chosen.phone_number)
    if chosen is None:
        print("     You own no voice-capable number yet. A trial account gets one for free (paid from trial credit).")
        candidates = client.available_phone_numbers("US").local.list(voice_enabled=True, limit=3)
        if not candidates:
            fail("no US local numbers available right now; pick one in the Twilio console and set TWILIO_VOICE_FROM")
            return None
        number = candidates[0].phone_number
        if not assume_yes:
            answer = input(f"     Buy {number} (US local, voice-capable)? [y/N] ").strip().lower()
            if answer != "y":
                todo("buy a voice number in the Twilio console and set TWILIO_VOICE_FROM, then rerun")
                return None
        chosen = client.incoming_phone_numbers.create(phone_number=number, friendly_name="Personal assistant")
        ok(f"bought {chosen.phone_number}")
        write_env("TWILIO_VOICE_FROM", chosen.phone_number)

    inbound_url = settings.public_url("/webhooks/voice/inbound")
    if chosen.voice_url != inbound_url or (chosen.voice_method or "").upper() != "POST":
        client.incoming_phone_numbers(chosen.sid).update(voice_url=inbound_url, voice_method="POST")
        ok(f"voice webhook set to {inbound_url}")
    else:
        ok("voice webhook already configured")
    return chosen.phone_number


def ensure_verified_caller_id(client: Client, settings: Settings) -> None:
    print("\n3. Your mobile as a verified caller ID (required on trial accounts)")
    existing = client.outgoing_caller_ids.list(phone_number=settings.owner_phone, limit=1)
    if existing:
        ok(f"{settings.owner_phone} is verified")
        return
    try:
        req = client.validation_requests.create(phone_number=settings.owner_phone, friendly_name="Assistant owner")
    except TwilioRestException as e:
        fail(f"could not start verification: {e.msg}")
        todo("verify your number under Phone Numbers > Verified Caller IDs in the Twilio console")
        return
    print(f"     Twilio is calling {settings.owner_phone} now. When asked, enter this code: {req.validation_code}")
    todo("answer the call and enter the code, then rerun this script to confirm")


def whatsapp_instructions(settings: Settings) -> None:
    print("\n4. WhatsApp sandbox (Twilio only allows these two steps from its web console)")
    print("     a) Open Twilio Console > Messaging > Try it out > Send a WhatsApp message.")
    print("        From your phone, WhatsApp the sandbox number with the message 'join <your-code>' shown there.")
    print("     b) On the Sandbox settings tab, set 'When a message comes in' to:")
    print(f"        {settings.public_url('/webhooks/whatsapp')}   (method POST), then Save.")
    if settings.twilio_whatsapp_from:
        ok(f"TWILIO_WHATSAPP_FROM is {settings.twilio_whatsapp_from}")
    else:
        todo("set TWILIO_WHATSAPP_FROM=whatsapp:+14155238886 (the sandbox number shown in the console) in .env")


def test_call(client: Client, settings: Settings, voice_from: str) -> None:
    print("\nTest call")
    call = client.calls.create(
        to=settings.owner_phone,
        from_=voice_from,
        twiml=(
            f'<Response><Say voice="{settings.voice_name}" language="{settings.voice_language}">'
            f"Hi {settings.owner_name}, your personal assistant is set up and can call you. Goodbye."
            "</Say></Response>"
        ),
    )
    ok(f"call placed (sid {call.sid}); your phone should ring in a few seconds")


def test_whatsapp(client: Client, settings: Settings) -> None:
    print("\nTest WhatsApp message")
    if not settings.twilio_whatsapp_from:
        fail("TWILIO_WHATSAPP_FROM is not set")
        return
    try:
        msg = client.messages.create(
            from_=settings.twilio_whatsapp_from,
            to=settings.owner_whatsapp_address,
            body=f"Hi {settings.owner_name}, your personal assistant is connected on WhatsApp.",
        )
        ok(f"message queued (sid {msg.sid}). If nothing arrives, send 'join <code>' to the sandbox first.")
    except TwilioRestException as e:
        fail(f"Twilio refused: {e.msg}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="buy a number without asking")
    parser.add_argument("--test-call", action="store_true", help="place a test call to OWNER_PHONE")
    parser.add_argument("--test-whatsapp", action="store_true", help="send a test WhatsApp message to the owner")
    args = parser.parse_args()

    if not ENV_PATH.exists():
        fail("no .env file found. Copy .env.example to .env and fill in your keys first.")
        return 1
    settings = Settings()
    if not settings.twilio_configured:
        fail("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set in .env")
        return 1
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    if not check_account(client, settings):
        return 1
    voice_from = ensure_voice_number(client, settings, args.yes)
    ensure_verified_caller_id(client, settings)
    whatsapp_instructions(settings)

    if args.test_call:
        if voice_from:
            test_call(client, settings, voice_from)
        else:
            fail("no voice number configured; skipping the test call")
    if args.test_whatsapp:
        test_whatsapp(client, settings)

    print("\nDone. Start the server (uvicorn assistant.server:create_app --factory) and message the sandbox number.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
