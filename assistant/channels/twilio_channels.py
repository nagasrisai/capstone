from __future__ import annotations

import logging

from twilio.rest import Client

from ..config import Settings

log = logging.getLogger(__name__)

WHATSAPP_MAX_CHARS = 1500  # Twilio/WhatsApp cap is 1600; leave headroom.


def chunk_text(text: str, limit: int = WHATSAPP_MAX_CHARS) -> list[str]:
    """Split long replies on paragraph/line boundaries so each chunk fits a WhatsApp message."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for para in text.split("\n"):
        candidate = para if not current else f"{current}\n{para}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(para) > limit:  # a single huge line: hard-split
            chunks.append(para[:limit])
            para = para[limit:]
        current = para
    if current:
        chunks.append(current)
    return chunks


class TwilioChannels:
    def __init__(self, settings: Settings, client: Client | None = None):
        if not settings.twilio_configured:
            raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set to use Twilio channels")
        self.settings = settings
        self.client = client or Client(settings.twilio_account_sid, settings.twilio_auth_token)

    def send_whatsapp(self, text: str) -> str:
        if not self.settings.twilio_whatsapp_from:
            raise RuntimeError("TWILIO_WHATSAPP_FROM is not configured")
        last_sid = ""
        for chunk in chunk_text(text):
            msg = self.client.messages.create(
                from_=self.settings.twilio_whatsapp_from,
                to=self.settings.owner_whatsapp_address,
                body=chunk,
            )
            last_sid = msg.sid
            log.info("WhatsApp sent sid=%s chars=%d", msg.sid, len(chunk))
        return last_sid

    def start_call(self, job_id: str) -> str:
        if not self.settings.twilio_voice_from:
            raise RuntimeError("TWILIO_VOICE_FROM is not configured")
        if not self.settings.owner_phone:
            raise RuntimeError("OWNER_PHONE is not configured")
        call = self.client.calls.create(
            to=self.settings.owner_phone,
            from_=self.settings.twilio_voice_from,
            url=self.settings.public_url(f"/webhooks/voice/outbound/{job_id}"),
            method="POST",
            timeout=40,
        )
        log.info("Call placed sid=%s job=%s", call.sid, job_id)
        return call.sid
