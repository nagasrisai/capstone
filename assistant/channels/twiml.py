"""TwiML builders for the voice conversation loop."""

from __future__ import annotations

from twilio.twiml.voice_response import Gather, VoiceResponse

from ..config import Settings


def _say(resp: VoiceResponse | Gather, text: str, settings: Settings) -> None:
    resp.say(text, voice=settings.voice_name, language=settings.voice_language)


def say_and_listen(text: str, action_url: str, settings: Settings, *, no_input_text: str | None = None) -> str:
    """Speak `text`, then capture the owner's spoken reply and POST it to `action_url`."""
    resp = VoiceResponse()
    gather = Gather(
        input="speech",
        action=action_url,
        method="POST",
        speech_timeout="auto",
        language=settings.voice_language,
        action_on_empty_result=False,
    )
    _say(gather, text, settings)
    resp.append(gather)
    # Reached only if nothing was said.
    _say(resp, no_input_text or "I didn't catch anything. I'll follow up on WhatsApp. Goodbye.", settings)
    resp.hangup()
    return str(resp)


def say_and_hangup(text: str, settings: Settings) -> str:
    resp = VoiceResponse()
    _say(resp, text, settings)
    resp.hangup()
    return str(resp)


def wait_then_redirect(redirect_url: str, settings: Settings, *, text: str | None = None, pause: int = 3) -> str:
    """Keep the call alive while the assistant thinks, then re-check at `redirect_url`."""
    resp = VoiceResponse()
    if text:
        _say(resp, text, settings)
    resp.pause(length=pause)
    resp.redirect(redirect_url, method="POST")
    return str(resp)


def empty_response() -> str:
    return str(VoiceResponse())
