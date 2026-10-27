"""Delivers reminders when they fall due: WhatsApp message, phone call, or both."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime

from .agent import Assistant
from .channels.base import Channels
from .config import Settings
from .store import Reminder, Store

log = logging.getLogger(__name__)


def whatsapp_reminder_text(r: Reminder) -> str:
    text = f"⏰ Reminder: {r.title}"
    if r.notes:
        text += f"\n{r.notes}"
    return text


def spoken_reminder_text(r: Reminder, settings: Settings) -> str:
    text = f"Hi {settings.owner_name}, this is your assistant with a reminder. {r.title}."
    if r.notes:
        text += f" {r.notes}."
    text += " Say done, or tell me when to remind you again."
    return text


class ReminderScheduler:
    def __init__(self, store: Store, channels: Channels, settings: Settings, assistant: Assistant | None = None):
        self.store = store
        self.channels = channels
        self.settings = settings
        self.assistant = assistant

    def deliver_due(self, now: datetime | None = None) -> int:
        """Deliver every due reminder once. Returns how many were delivered."""
        delivered = 0
        for r in self.store.due_reminders(now):
            if not self.store.claim_reminder(r.id):
                continue
            try:
                self._deliver(r)
                self.store.mark_reminder_delivered(r.id)
                delivered += 1
                if self.assistant:
                    self.assistant.record_event(f"Reminder #{r.id} '{r.title}' was delivered via {r.channel}.")
            except Exception:  # noqa: BLE001 - keep the loop alive; retry later
                log.exception("Failed to deliver reminder #%s", r.id)
                self.store.mark_reminder_failed(r.id)
        return delivered

    def _deliver(self, r: Reminder) -> None:
        if r.channel in ("whatsapp", "both"):
            self.channels.send_whatsapp(whatsapp_reminder_text(r))
        if r.channel in ("call", "both"):
            job = self.store.create_voice_job(
                "reminder",
                {"message": spoken_reminder_text(r, self.settings), "wait_for_reply": True, "reminder_id": r.id},
            )
            self.channels.start_call(job.id)

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("Reminder scheduler started (every %ss)", self.settings.reminder_poll_seconds)
        while not stop.is_set():
            try:
                await asyncio.to_thread(self.deliver_due)
            except Exception:  # noqa: BLE001
                log.exception("Scheduler tick failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.reminder_poll_seconds)
            except asyncio.TimeoutError:
                pass

    def run_in_thread(self, stop: threading.Event) -> threading.Thread:
        """Blocking variant for the CLI."""

        def loop() -> None:
            while not stop.is_set():
                try:
                    self.deliver_due()
                except Exception:  # noqa: BLE001
                    log.exception("Scheduler tick failed")
                stop.wait(self.settings.reminder_poll_seconds)

        t = threading.Thread(target=loop, name="reminder-scheduler", daemon=True)
        t.start()
        return t
