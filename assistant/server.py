"""FastAPI app exposing the Twilio webhooks for WhatsApp and voice.

Twilio expects an answer within ~15 seconds, while a Claude turn can take longer, so every
inbound message is answered immediately and the agent runs in a background task:
  - WhatsApp: reply is sent afterwards via the Twilio REST API.
  - Voice: the call is kept alive with short pauses that redirect to a poll endpoint until the
    agent's answer is ready, then the answer is spoken and the call listens again.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from twilio.request_validator import RequestValidator

from .agent import Assistant
from .channels import ConsoleChannels, TwilioChannels, twiml
from .channels.base import Channels
from .config import Settings, get_settings
from .scheduler import ReminderScheduler
from .store import Store, utcnow

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- app wiring
def build_app(
    settings: Settings | None = None,
    *,
    store: Store | None = None,
    channels: Channels | None = None,
    assistant: Assistant | None = None,
    run_scheduler: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    store = store or Store(settings.database_path)
    if channels is None:
        channels = TwilioChannels(settings) if settings.twilio_configured else ConsoleChannels()
        if not settings.twilio_configured:
            log.warning("Twilio is not configured; outbound messages/calls will be printed to the console")
    assistant = assistant or Assistant(settings, store, channels)
    scheduler = ReminderScheduler(store, channels, settings, assistant)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler.run_forever(stop)) if run_scheduler else None
        try:
            yield
        finally:
            stop.set()
            if task:
                await task

    app = FastAPI(title="Personal assistant", lifespan=lifespan)
    app.state.settings = settings
    app.state.store = store
    app.state.channels = channels
    app.state.assistant = assistant
    app.state.scheduler = scheduler
    _register_routes(app)
    return app


def _xml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


def _spawn(fn, *args) -> threading.Thread:
    """Run an agent turn on its own thread so the webhook can answer Twilio immediately."""
    t = threading.Thread(target=fn, args=args, name=f"turn-{fn.__name__}", daemon=True)
    t.start()
    return t


async def _verify_twilio(request: Request, form: dict[str, Any]) -> None:
    settings: Settings = request.app.state.settings
    if not settings.validate_twilio_signature:
        return
    if not settings.twilio_auth_token:
        raise HTTPException(status_code=503, detail="Twilio auth token not configured")
    signature = request.headers.get("X-Twilio-Signature", "")
    url = settings.public_url(request.url.path)
    if request.url.query:
        url += "?" + request.url.query
    if not RequestValidator(settings.twilio_auth_token).validate(url, form, signature):
        log.warning("Rejected request with invalid Twilio signature for %s", url)
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


def _register_routes(app: FastAPI) -> None:
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "time": utcnow().isoformat()}

    # ----------------------------------------------------------------- WhatsApp
    @app.post("/webhooks/whatsapp")
    async def whatsapp_inbound(request: Request) -> Response:
        form = dict(await request.form())
        await _verify_twilio(request, form)
        settings: Settings = request.app.state.settings
        sender = str(form.get("From", ""))
        if sender != settings.owner_whatsapp_address:
            log.warning("Ignoring WhatsApp message from non-owner %s", sender)
            return _xml("<Response/>")
        body = str(form.get("Body", "")).strip()
        try:
            n_media = int(form.get("NumMedia", 0) or 0)
        except ValueError:
            n_media = 0
        if n_media:
            body += f"\n[The owner attached {n_media} media file(s) which you cannot view; ask for the content as text if it matters.]"
        if not body.strip():
            return _xml("<Response/>")
        _spawn(_whatsapp_turn, request.app, body)
        return _xml("<Response/>")

    # ----------------------------------------------------------------- Voice: outbound call script
    @app.post("/webhooks/voice/outbound/{job_id}")
    async def voice_outbound(job_id: str, request: Request) -> Response:
        form = dict(await request.form())
        await _verify_twilio(request, form)
        settings: Settings = request.app.state.settings
        store: Store = request.app.state.store
        job = store.get_voice_job(job_id)
        if job is None:
            return _xml(twiml.say_and_hangup("Sorry, I lost track of why I called. Goodbye.", settings))
        store.update_voice_job(job_id, status="answered")
        message = job.payload.get("message", "")
        if job.payload.get("wait_for_reply"):
            return _xml(twiml.say_and_listen(message, settings.public_url("/webhooks/voice/turn"), settings))
        return _xml(twiml.say_and_hangup(message + " Goodbye.", settings))

    # ----------------------------------------------------------------- Voice: inbound call
    @app.post("/webhooks/voice/inbound")
    async def voice_inbound(request: Request) -> Response:
        form = dict(await request.form())
        await _verify_twilio(request, form)
        settings: Settings = request.app.state.settings
        if str(form.get("From", "")) != settings.owner_phone:
            return _xml(twiml.say_and_hangup("Sorry, this line is private. Goodbye.", settings))
        return _xml(
            twiml.say_and_listen(
                f"Hi {settings.owner_name}, how can I help?", settings.public_url("/webhooks/voice/turn"), settings
            )
        )

    # ----------------------------------------------------------------- Voice: one spoken turn
    @app.post("/webhooks/voice/turn")
    async def voice_turn(request: Request) -> Response:
        form = dict(await request.form())
        await _verify_twilio(request, form)
        settings: Settings = request.app.state.settings
        store: Store = request.app.state.store
        speech = str(form.get("SpeechResult", "")).strip()
        if not speech:
            return _xml(twiml.say_and_hangup("I didn't catch that. I'll follow up on WhatsApp. Goodbye.", settings))
        job = store.create_voice_job("turn", {"text": speech}, status="pending")
        _spawn(_voice_turn, request.app, job.id, speech)
        return _xml(twiml.wait_then_redirect(settings.public_url(f"/webhooks/voice/poll/{job.id}"), settings, pause=2))

    # ----------------------------------------------------------------- Voice: wait for the agent
    @app.post("/webhooks/voice/poll/{job_id}")
    async def voice_poll(job_id: str, request: Request) -> Response:
        form = dict(await request.form())
        await _verify_twilio(request, form)
        settings: Settings = request.app.state.settings
        store: Store = request.app.state.store
        job = store.get_voice_job(job_id)
        if job is None:
            return _xml(twiml.say_and_hangup("Sorry, something went wrong on my side. Goodbye.", settings))
        if job.status == "done":
            text = job.result or "Okay."
            if job.payload.get("hang_up"):
                return _xml(twiml.say_and_hangup(text, settings))
            return _xml(twiml.say_and_listen(text, settings.public_url("/webhooks/voice/turn"), settings))
        if job.status == "error":
            return _xml(twiml.say_and_hangup("Sorry, I ran into a problem. I'll message you on WhatsApp. Goodbye.", settings))
        age = (utcnow() - job.created_at).total_seconds()
        if age > settings.voice_poll_timeout_seconds:
            store.update_voice_job(job_id, status="abandoned")
            return _xml(twiml.say_and_hangup("This is taking longer than expected. I'll send my answer on WhatsApp. Goodbye.", settings))
        text = "Just a moment." if 8 <= age < 12 else None
        return _xml(twiml.wait_then_redirect(settings.public_url(f"/webhooks/voice/poll/{job_id}"), settings, text=text, pause=3))


# --------------------------------------------------------------------------- background work
def _whatsapp_turn(app: FastAPI, text: str) -> None:
    assistant: Assistant = app.state.assistant
    channels: Channels = app.state.channels
    try:
        reply = assistant.handle(text, "whatsapp")
        if reply.text:
            channels.send_whatsapp(reply.text)
    except Exception:  # noqa: BLE001
        log.exception("WhatsApp turn failed")
        try:
            channels.send_whatsapp("Sorry, something went wrong on my side. Please try again.")
        except Exception:  # noqa: BLE001
            log.exception("Could not send the failure notice")


def _voice_turn(app: FastAPI, job_id: str, text: str) -> None:
    assistant: Assistant = app.state.assistant
    store: Store = app.state.store
    channels: Channels = app.state.channels
    try:
        reply = assistant.handle(text, "voice")
        spoken = reply.text or "Okay."
        job = store.get_voice_job(job_id)
        if job is not None and job.status == "abandoned":
            # The call already ended; deliver the answer on WhatsApp instead.
            channels.send_whatsapp(spoken)
            store.update_voice_job(job_id, status="done", result=spoken)
            return
        store.update_voice_job(job_id, status="done", result=spoken, payload={"text": text, "hang_up": reply.hang_up})
    except Exception:  # noqa: BLE001
        log.exception("Voice turn failed")
        store.update_voice_job(job_id, status="error")


def create_app() -> FastAPI:
    """Entry point for `uvicorn assistant.server:create_app --factory`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return build_app()
