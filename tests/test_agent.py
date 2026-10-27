import json
from datetime import datetime, timedelta

from assistant.agent import FALLBACK_BETA, Assistant
from assistant.tools import TOOL_DEFINITIONS

from .conftest import FakeClaude, FakeResponse, text_block, thinking_block, tool_use_block


def make_assistant(settings, store, channels, responses):
    client = FakeClaude(responses)
    return Assistant(settings, store, channels, client=client), client


def test_plain_reply_is_stored_append_only(settings, store, channels):
    assistant, client = make_assistant(settings, store, channels, [FakeResponse([thinking_block(), text_block("Hi Sai!")], "end_turn")])
    reply = assistant.handle("hello", "whatsapp")
    assert reply.text == "Hi Sai!" and reply.hang_up is False

    req = client.requests[0]
    assert req["model"] == settings.claude_model
    assert req["fallbacks"] == "default" and FALLBACK_BETA in req["betas"]
    assert req["tools"] == TOOL_DEFINITIONS
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Sai" in req["system"][0]["text"]
    assert req["messages"][-1]["role"] == "user" and "via WhatsApp" in req["messages"][-1]["content"]

    conv = store.current_conversation(100, 12)
    msgs = store.conversation_messages(conv)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["content"][0] == thinking_block()  # thinking replayed unchanged


def test_tool_call_creates_reminder_and_continues(settings, store, channels):
    due = (datetime.now(settings.tz) + timedelta(hours=1)).replace(second=0, microsecond=0)
    responses = [
        FakeResponse(
            [text_block("Setting that up."), tool_use_block("create_reminder", {"title": "Call mom", "due_at": due.strftime("%Y-%m-%dT%H:%M"), "channel": "call", "notes": None})],
            "tool_use",
        ),
        FakeResponse([text_block("Done, I'll call you at that time.")], "end_turn"),
    ]
    assistant, client = make_assistant(settings, store, channels, responses)
    reply = assistant.handle("remind me to call mom in an hour, call me", "whatsapp")
    assert reply.text == "Done, I'll call you at that time."

    reminders = store.list_reminders()
    assert len(reminders) == 1 and reminders[0].title == "Call mom" and reminders[0].channel == "call"
    assert reminders[0].due_at == due

    # second request carried the tool result back
    second = client.requests[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["content"][1]["type"] == "tool_use"
    result = second[-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_1" and result["is_error"] is False
    assert json.loads(result["content"])["created"]["id"] == reminders[0].id
    # the dynamic system block now lists the reminder
    assert "Call mom" in client.requests[1]["system"][1]["text"]


def test_tool_error_is_reported_not_raised(settings, store, channels):
    responses = [
        FakeResponse([tool_use_block("create_reminder", {"title": "x", "due_at": "2000-01-01T00:00", "channel": "whatsapp", "notes": None})], "tool_use"),
        FakeResponse([text_block("That time is in the past; when should I remind you?")], "end_turn"),
    ]
    assistant, client = make_assistant(settings, store, channels, responses)
    reply = assistant.handle("remind me", "whatsapp")
    assert "past" in reply.text
    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "in the past" in result["content"]
    assert store.list_reminders() == []


def test_clarification_by_phone_call(settings, store, channels):
    responses = [
        FakeResponse([tool_use_block("call_user", {"message": "Did you mean this Friday or next?", "wait_for_reply": True})], "tool_use"),
        FakeResponse([text_block("I'm calling you to confirm which Friday.")], "end_turn"),
    ]
    assistant, _ = make_assistant(settings, store, channels, responses)
    reply = assistant.handle("book the thing for friday", "whatsapp")
    assert "calling" in reply.text
    assert len(channels.calls_started) == 1
    job = store.get_voice_job(channels.calls_started[0])
    assert job.kind == "outbound" and job.payload["wait_for_reply"] is True
    assert job.payload["message"].startswith("Did you mean")


def test_hang_up_on_voice_channel(settings, store, channels):
    responses = [
        FakeResponse([tool_use_block("hang_up", {})], "tool_use"),
        FakeResponse([text_block("Great, talk later. Bye!")], "end_turn"),
    ]
    assistant, _ = make_assistant(settings, store, channels, responses)
    reply = assistant.handle("that's all, thanks", "voice")
    assert reply.hang_up is True and reply.text.startswith("Great")


def test_hang_up_rejected_outside_calls(settings, store, channels):
    responses = [
        FakeResponse([tool_use_block("hang_up", {})], "tool_use"),
        FakeResponse([text_block("ok")], "end_turn"),
    ]
    assistant, client = make_assistant(settings, store, channels, responses)
    reply = assistant.handle("bye", "whatsapp")
    assert reply.hang_up is False
    assert client.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_refusal_returns_graceful_text(settings, store, channels):
    assistant, _ = make_assistant(settings, store, channels, [FakeResponse([], "refusal")])
    reply = assistant.handle("...", "whatsapp")
    assert reply.stop_reason == "refusal" and reply.text


def test_fallback_boundary_drops_internal_blocks_before_it(settings, store, channels):
    blocks = [
        thinking_block(),
        tool_use_block("list_reminders", {"include_done": False}, "toolu_dead"),
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
        text_block("Here is your list."),
    ]
    assistant, client = make_assistant(settings, store, channels, [FakeResponse(blocks, "end_turn")])
    reply = assistant.handle("list", "whatsapp")
    assert reply.text == "Here is your list."
    stored = store.conversation_messages(store.current_conversation(100, 12))[-1]["content"]
    assert [b["type"] for b in stored] == ["fallback", "text"]


def test_record_event_appends_user_note(settings, store, channels):
    assistant, _ = make_assistant(settings, store, channels, [])
    assistant.record_event("Reminder #1 delivered via whatsapp.")
    msgs = store.conversation_messages(store.current_conversation(100, 12))
    assert msgs[-1]["role"] == "user" and "[event] Reminder #1" in msgs[-1]["content"]
