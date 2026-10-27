"""Local terminal chat with the assistant: no Twilio needed.

Outbound WhatsApp messages and phone calls are printed instead of sent, and reminders are
delivered by a background scheduler thread so you can test the whole flow.

    python -m assistant.cli

Commands:  /voice <text>   simulate the owner answering on a phone call
           /reminders      list pending reminders
           /quit
"""

from __future__ import annotations

import logging
import threading

from .agent import Assistant
from .channels import ConsoleChannels
from .config import get_settings
from .scheduler import ReminderScheduler
from .store import Store


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    settings = get_settings()
    store = Store(settings.database_path)
    channels = ConsoleChannels()
    assistant = Assistant(settings, store, channels)
    scheduler = ReminderScheduler(store, channels, settings, assistant)
    stop = threading.Event()
    scheduler.run_in_thread(stop)

    print(f"Assistant for {settings.owner_name} ready (model {settings.claude_model}). Type /quit to exit.\n")
    try:
        while True:
            try:
                text = input("you> ").strip()
            except EOFError:
                break
            if not text:
                continue
            if text in ("/quit", "/exit"):
                break
            if text == "/reminders":
                for r in store.list_reminders():
                    print(f"  #{r.id} {r.due_at.astimezone(settings.tz):%a %d %b %H:%M} via {r.channel}: {r.title}")
                continue
            channel = "console"
            if text.startswith("/voice "):
                channel, text = "voice", text[len("/voice ") :]
            reply = assistant.handle(text, channel)
            print(f"\nassistant> {reply.text}\n")
            if reply.hang_up:
                print("[call ended]\n")
    finally:
        stop.set()
        store.close()


if __name__ == "__main__":
    main()
