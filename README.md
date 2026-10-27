# Personal AI assistant (WhatsApp + phone calls)

A single-owner personal assistant powered by Claude. You talk to it on WhatsApp like a real
assistant; it keeps your instructions, remembers facts about you, schedules reminders, and
**calls your mobile** when a reminder is due or when it needs a clarification that cannot wait.
On a call it listens to your spoken answer and keeps the conversation going.

```
 You (WhatsApp / phone)  <-- Twilio -->  FastAPI webhooks  -->  Claude agent loop (tools)
                                              |                        |
                                              +--- SQLite memory ------+
                                              |   (instructions, facts, reminders, history)
                                              +--- reminder scheduler --> WhatsApp / call
```

## What it can do

| You say (WhatsApp or voice) | What happens |
|---|---|
| "Remind me to take my tablets at 9pm, call me" | Reminder stored with channel `call`; at 21:00 your phone rings, the assistant speaks the reminder and listens ("done" / "snooze 15 minutes"). |
| "Remind me tomorrow morning to send the report" | Reminder at 09:00 local time over WhatsApp. |
| "From now on always call me for medication reminders" | Stored as a standing instruction and followed from then on. |
| "My doctor is Dr. Rao at Apollo, +91..." | Stored as a fact and used in later conversations. |
| "Book the slot for Friday" (ambiguous) | Asks one precise question, on WhatsApp by default, or by phone if urgent or if you told it to call for such things. |
| You call the Twilio number | It greets you and takes instructions by voice. |

## Setup

### 1. Accounts
- **Anthropic API key**: https://platform.claude.com
- **Twilio** account with:
  - WhatsApp: the free **WhatsApp Sandbox** (Messaging > Try it out > Send a WhatsApp message). Join it from your phone with the `join <code>` message. For production, register a WhatsApp sender.
  - Voice: a voice-capable phone number.

### 2. Configure
```bash
cp .env.example .env     # then fill in the values
```
Key settings (all in `.env`):

| Variable | Meaning |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key. |
| `OWNER_PHONE` | Your mobile in E.164 (`+91...`). The assistant only talks to this number. |
| `OWNER_TIMEZONE` | IANA zone, e.g. `Asia/Kolkata`. All reminder times are interpreted in it. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Twilio credentials. |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+14155238886` for the sandbox, or your approved sender. |
| `TWILIO_VOICE_FROM` | Your Twilio voice number. |
| `PUBLIC_BASE_URL` | Public HTTPS URL of this server (ngrok works for testing). |
| `CLAUDE_MODEL` / `CLAUDE_EFFORT` | Defaults `claude-opus-5-5` / `medium`. |

### 3. Run
```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn assistant.server:create_app --factory --port 8000
# or: docker compose up --build
```
Expose it, e.g. `ngrok http 8000`, and put the HTTPS URL in `PUBLIC_BASE_URL`.

### 4. Let the setup script configure Twilio
```bash
python scripts/setup_twilio.py               # checks the account, gets/configures a voice number,
                                             # verifies your mobile as a caller ID
python scripts/setup_twilio.py --test-call   # rings your phone with a test message
python scripts/setup_twilio.py --test-whatsapp
```
It buys the free trial voice number if you have none (asks first), points its "A call comes in"
webhook at `/webhooks/voice/inbound`, and starts the caller-ID verification call that trial
accounts need. The only manual steps are the two WhatsApp sandbox actions Twilio keeps in its
web console, which the script prints:

| Twilio console | What to do |
|---|---|
| Messaging > Try it out > Send a WhatsApp message | WhatsApp `join <code>` to the sandbox number from your phone |
| Sandbox settings > "When a message comes in" | `https://<PUBLIC_BASE_URL>/webhooks/whatsapp`, POST |

Outbound calls fetch their script from `/webhooks/voice/outbound/{job}` automatically.

## Hosting: where should it run?

Twilio must be able to reach the server, and reminders only fire while it is running, so for
daily use it needs an always-on host. Options, cheapest first:

| Option | Cost | Good for |
|---|---|---|
| Your laptop + `ngrok http 8000` | free | Trying it out. Reminders stop when the laptop sleeps, and the ngrok URL changes on every restart (update `PUBLIC_BASE_URL` and the Twilio webhooks). |
| An old laptop / Raspberry Pi at home + Cloudflare Tunnel | free | Always on at home; a named tunnel gives a stable URL. |
| Fly.io (`fly.toml` included) | a few dollars a month | Always on, stable URL, persistent disk. See the comments in `fly.toml`. |
| Any small VPS + `docker compose up -d` | ~$5 a month | Same, with full control. Put a reverse proxy with HTTPS (Caddy) in front. |

Free web hosts that "sleep" when idle (Render free tier, etc.) are not suitable: the scheduler
is asleep when your reminder is due.

### Try it without Twilio
```bash
python -m assistant.cli
```
Outbound WhatsApp messages and calls are printed to the terminal; `/voice <text>` simulates
answering on a call; reminders fire from a background thread.

## How it works

- **Agent** (`assistant/agent.py`): manual tool-use loop on the Claude Messages API with
  `claude-opus-5-5`, adaptive thinking, `effort` from config, a cached system prompt, strict
  tool schemas and server-side refusal fallbacks enabled. The conversation history is stored
  append-only in SQLite and replayed unchanged, as the API requires for thinking blocks;
  when a conversation gets long or stale a fresh one starts, while instructions, facts and
  reminders persist and are injected into the system prompt.
- **Tools** (`assistant/tools.py`): `create_reminder`, `list_reminders`, `reschedule_reminder`,
  `cancel_reminder`, `add_instruction`, `list_instructions`, `remove_instruction`, `remember`,
  `forget`, `call_user`, `send_whatsapp`, `hang_up`.
- **WhatsApp** (`/webhooks/whatsapp`): Twilio signature is verified, only the owner's number is
  accepted, the webhook returns immediately and the reply is sent through the Twilio REST API
  once Claude finishes (so long turns never hit Twilio's 15 s webhook timeout).
- **Voice**: outbound calls speak the message and, when a reply is expected, use `<Gather
  input="speech">`. Each spoken turn is posted to `/webhooks/voice/turn`, the agent runs on a
  background thread, and the call stays alive through short `<Pause>`/`<Redirect>` loops on
  `/webhooks/voice/poll/{job}` until the answer is ready. If it takes longer than
  `VOICE_POLL_TIMEOUT_SECONDS` the call ends politely and the answer is sent on WhatsApp.
- **Scheduler** (`assistant/scheduler.py`): polls every `REMINDER_POLL_SECONDS`, claims each due
  reminder atomically, delivers it over WhatsApp, call, or both, retries up to 3 times on
  failure, and records the delivery in the conversation so the agent knows what you were told.

## Tests
```bash
pip install -r requirements-dev.txt
pytest
```
The suite uses a scripted fake Claude client and console channels, so it runs offline.

## Known limits
- WhatsApp Business rules: the assistant can message you freely for 24 h after your last message;
  outside that window Twilio requires an approved template. The sandbox number is fine for
  personal testing. Reminders delivered by **call** are not affected.
- One owner per deployment by design; messages and calls from any other number are ignored.
- The assistant cannot see images or documents you send on WhatsApp (it is told they arrived).
