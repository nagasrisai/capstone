from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

log = logging.getLogger(__name__)


class Channels(Protocol):
    """Everything the assistant can do to reach its owner."""

    def send_whatsapp(self, text: str) -> str:
        """Send a WhatsApp message to the owner. Returns a provider message id."""
        ...

    def start_call(self, job_id: str) -> str:
        """Place an outbound call to the owner; Twilio will fetch TwiML for `job_id`. Returns a call id."""
        ...


@dataclass
class ConsoleChannels:
    """Prints instead of sending. Used by the CLI and by tests."""

    whatsapp_sent: list[str] = field(default_factory=list)
    calls_started: list[str] = field(default_factory=list)

    def send_whatsapp(self, text: str) -> str:
        self.whatsapp_sent.append(text)
        print(f"\n[WhatsApp -> owner]\n{text}\n")
        return f"console-wa-{len(self.whatsapp_sent)}"

    def start_call(self, job_id: str) -> str:
        self.calls_started.append(job_id)
        print(f"\n[Phone call -> owner] (voice job {job_id})\n")
        return f"console-call-{len(self.calls_started)}"
