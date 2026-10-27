"""Tool definitions (JSON schema, strict) and their implementations."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .channels.base import Channels
from .config import Settings
from .store import Store

log = logging.getLogger(__name__)


def _schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "create_reminder",
        "description": (
            "Schedule a reminder for the owner. Call this whenever the owner asks to be reminded of "
            "something, or asks you to call or message them at a given time. The reminder is delivered "
            "automatically at due_at over the chosen channel."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "title": {"type": "string", "description": "What to remind about, phrased as it will be read/spoken to the owner."},
                "due_at": {
                    "type": "string",
                    "description": "ISO 8601 date-time, e.g. 2026-10-28T09:00 (interpreted in the owner's timezone when no offset is given).",
                },
                "channel": {"type": "string", "enum": ["whatsapp", "call", "both"], "description": "How to deliver it."},
                "notes": {"type": ["string", "null"], "description": "Optional extra details to include."},
            }
        ),
    },
    {
        "name": "list_reminders",
        "description": "List reminders. Use it when the owner asks what is scheduled, or before rescheduling/cancelling if you do not know the id.",
        "strict": True,
        "input_schema": _schema({"include_done": {"type": "boolean", "description": "Also include delivered/cancelled reminders."}}),
    },
    {
        "name": "reschedule_reminder",
        "description": "Move an existing reminder to a new time (e.g. snooze). Re-activates it if it was already delivered.",
        "strict": True,
        "input_schema": _schema(
            {
                "reminder_id": {"type": "integer"},
                "due_at": {"type": "string", "description": "New ISO 8601 date-time in the owner's timezone."},
            }
        ),
    },
    {
        "name": "cancel_reminder",
        "description": "Cancel a pending reminder.",
        "strict": True,
        "input_schema": _schema({"reminder_id": {"type": "integer"}}),
    },
    {
        "name": "add_instruction",
        "description": (
            "Save a standing instruction that applies from now on (e.g. 'always call me for medication "
            "reminders', 'never message me after 10pm'). Call it when the owner states a rule or preference about how you should work."
        ),
        "strict": True,
        "input_schema": _schema({"text": {"type": "string", "description": "The rule, in one clear sentence."}}),
    },
    {
        "name": "list_instructions",
        "description": "List the active standing instructions with their ids.",
        "strict": True,
        "input_schema": _schema({}),
    },
    {
        "name": "remove_instruction",
        "description": "Deactivate a standing instruction by id when the owner cancels or changes a rule.",
        "strict": True,
        "input_schema": _schema({"instruction_id": {"type": "integer"}}),
    },
    {
        "name": "remember",
        "description": "Store a durable fact about the owner (e.g. key 'doctor', value 'Dr. Rao, Apollo clinic, +91...'). Overwrites an existing key.",
        "strict": True,
        "input_schema": _schema({"key": {"type": "string"}, "value": {"type": "string"}}),
    },
    {
        "name": "forget",
        "description": "Delete a remembered fact by key.",
        "strict": True,
        "input_schema": _schema({"key": {"type": "string"}}),
    },
    {
        "name": "call_user",
        "description": (
            "Place a phone call to the owner right now and speak `message`. Use it for urgent matters, "
            "for clarification questions that cannot wait, or when the owner asked to be called. "
            "With wait_for_reply=true the call listens for the owner's answer, which arrives later as a new "
            "message tagged 'via voice call'. Do not use this while already on a voice call."
        ),
        "strict": True,
        "input_schema": _schema(
            {
                "message": {"type": "string", "description": "What to say, in natural spoken language. One to three sentences."},
                "wait_for_reply": {"type": "boolean", "description": "True when you are asking a question and need the answer."},
            }
        ),
    },
    {
        "name": "send_whatsapp",
        "description": (
            "Send a WhatsApp message to the owner outside the normal reply flow. Use it during a voice call "
            "to send details that are easier to read than to hear (addresses, lists, links). Your normal "
            "reply to a WhatsApp message is delivered automatically, so do not use this for ordinary replies."
        ),
        "strict": True,
        "input_schema": _schema({"message": {"type": "string"}}),
    },
    {
        "name": "hang_up",
        "description": "End the current phone call after your final spoken sentence. Only valid during a voice call.",
        "strict": True,
        "input_schema": _schema({}),
    },
]


class ToolError(Exception):
    """Raised by a tool implementation; reported back to Claude as an error tool_result."""


@dataclass
class RunContext:
    """Per-request state the tools can read or set."""

    channel: str  # whatsapp | voice | console
    hang_up_requested: bool = False
    side_effects: list[str] = field(default_factory=list)


class ToolExecutor:
    def __init__(self, store: Store, channels: Channels, settings: Settings):
        self.store = store
        self.channels = channels
        self.settings = settings
        self._handlers: dict[str, Callable[[dict[str, Any], RunContext], str]] = {
            "create_reminder": self.create_reminder,
            "list_reminders": self.list_reminders,
            "reschedule_reminder": self.reschedule_reminder,
            "cancel_reminder": self.cancel_reminder,
            "add_instruction": self.add_instruction,
            "list_instructions": self.list_instructions,
            "remove_instruction": self.remove_instruction,
            "remember": self.remember,
            "forget": self.forget,
            "call_user": self.call_user,
            "send_whatsapp": self.send_whatsapp,
            "hang_up": self.hang_up,
        }

    def execute(self, name: str, tool_input: dict[str, Any], ctx: RunContext) -> tuple[str, bool]:
        """Run a tool. Returns (result_text, is_error)."""
        handler = self._handlers.get(name)
        if handler is None:
            return f"Unknown tool: {name}", True
        try:
            return handler(tool_input, ctx), False
        except ToolError as e:
            return str(e), True
        except Exception as e:  # noqa: BLE001 - report any failure to the model rather than crash the turn
            log.exception("Tool %s failed", name)
            return f"{type(e).__name__}: {e}", True

    # ------------------------------------------------------------------ helpers
    def _parse_due(self, raw: str) -> datetime:
        try:
            dt = datetime.fromisoformat(raw.strip())
        except ValueError as e:
            raise ToolError(f"due_at must be ISO 8601 (got {raw!r}): {e}") from e
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.settings.tz)
        return dt

    def _fmt(self, dt: datetime) -> str:
        return dt.astimezone(self.settings.tz).strftime("%a %d %b %Y %H:%M %Z")

    def _reminder_json(self, r) -> dict[str, Any]:
        return {
            "id": r.id,
            "title": r.title,
            "notes": r.notes,
            "due_at_local": self._fmt(r.due_at),
            "channel": r.channel,
            "status": r.status,
        }

    # ------------------------------------------------------------------ reminders
    def create_reminder(self, args: dict[str, Any], ctx: RunContext) -> str:
        due = self._parse_due(args["due_at"])
        now = datetime.now(self.settings.tz)
        if due < now:
            raise ToolError(f"due_at {self._fmt(due)} is in the past (now is {self._fmt(now)}). Ask the owner or pick a future time.")
        r = self.store.create_reminder(args["title"].strip(), due, args["channel"], args.get("notes") or None)
        ctx.side_effects.append(f"reminder #{r.id} created")
        return json.dumps({"created": self._reminder_json(r)})

    def list_reminders(self, args: dict[str, Any], ctx: RunContext) -> str:
        rs = self.store.list_reminders(include_done=bool(args.get("include_done")))
        return json.dumps({"reminders": [self._reminder_json(r) for r in rs]})

    def reschedule_reminder(self, args: dict[str, Any], ctx: RunContext) -> str:
        due = self._parse_due(args["due_at"])
        if not self.store.get_reminder(int(args["reminder_id"])):
            raise ToolError(f"No reminder with id {args['reminder_id']}")
        r = self.store.reschedule_reminder(int(args["reminder_id"]), due)
        return json.dumps({"rescheduled": self._reminder_json(r)})

    def cancel_reminder(self, args: dict[str, Any], ctx: RunContext) -> str:
        if not self.store.cancel_reminder(int(args["reminder_id"])):
            raise ToolError(f"Reminder {args['reminder_id']} was not pending (already delivered, cancelled, or missing).")
        return json.dumps({"cancelled": int(args["reminder_id"])})

    # ------------------------------------------------------------------ instructions & memory
    def add_instruction(self, args: dict[str, Any], ctx: RunContext) -> str:
        i = self.store.add_instruction(args["text"].strip())
        return json.dumps({"added": {"id": i.id, "text": i.text}})

    def list_instructions(self, args: dict[str, Any], ctx: RunContext) -> str:
        return json.dumps({"instructions": [{"id": i.id, "text": i.text} for i in self.store.list_instructions()]})

    def remove_instruction(self, args: dict[str, Any], ctx: RunContext) -> str:
        if not self.store.remove_instruction(int(args["instruction_id"])):
            raise ToolError(f"No active instruction with id {args['instruction_id']}")
        return json.dumps({"removed": int(args["instruction_id"])})

    def remember(self, args: dict[str, Any], ctx: RunContext) -> str:
        self.store.remember(args["key"], args["value"])
        return json.dumps({"remembered": {args["key"].strip().lower(): args["value"]}})

    def forget(self, args: dict[str, Any], ctx: RunContext) -> str:
        if not self.store.forget(args["key"]):
            raise ToolError(f"Nothing remembered under {args['key']!r}")
        return json.dumps({"forgot": args["key"]})

    # ------------------------------------------------------------------ channels
    def call_user(self, args: dict[str, Any], ctx: RunContext) -> str:
        if ctx.channel == "voice":
            raise ToolError("Already on a voice call with the owner; just speak instead.")
        job = self.store.create_voice_job(
            "outbound", {"message": args["message"], "wait_for_reply": bool(args["wait_for_reply"])}
        )
        call_id = self.channels.start_call(job.id)
        ctx.side_effects.append(f"call placed ({call_id})")
        note = (
            "The owner's spoken answer will arrive as a new message tagged 'via voice call'. Do not ask the question again on WhatsApp."
            if args["wait_for_reply"]
            else "The message will be spoken and the call ends."
        )
        return json.dumps({"call_placed": True, "call_id": call_id, "note": note})

    def send_whatsapp(self, args: dict[str, Any], ctx: RunContext) -> str:
        msg_id = self.channels.send_whatsapp(args["message"])
        ctx.side_effects.append("whatsapp sent")
        return json.dumps({"sent": True, "message_id": msg_id})

    def hang_up(self, args: dict[str, Any], ctx: RunContext) -> str:
        if ctx.channel != "voice":
            raise ToolError("Not on a voice call.")
        ctx.hang_up_requested = True
        return json.dumps({"ok": True, "note": "The call will end after your final sentence."})
