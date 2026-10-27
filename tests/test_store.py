from datetime import timedelta

from assistant.store import utcnow


def test_reminder_lifecycle(store):
    due = utcnow() - timedelta(seconds=1)
    r = store.create_reminder("Take medicine", due, "call", notes="two tablets")
    assert r.status == "pending" and r.channel == "call"
    assert [x.id for x in store.due_reminders()] == [r.id]
    assert store.claim_reminder(r.id) is True
    assert store.claim_reminder(r.id) is False  # already claimed
    store.mark_reminder_delivered(r.id)
    assert store.get_reminder(r.id).status == "delivered"
    assert store.due_reminders() == []
    later = utcnow() + timedelta(minutes=10)
    store.reschedule_reminder(r.id, later)
    assert store.get_reminder(r.id).status == "pending"
    assert store.cancel_reminder(r.id) is True
    assert store.cancel_reminder(r.id) is False


def test_failed_reminder_retries_then_gives_up(store):
    r = store.create_reminder("x", utcnow(), "whatsapp")
    store.claim_reminder(r.id)
    store.mark_reminder_failed(r.id)
    assert store.get_reminder(r.id).status == "pending"
    store.claim_reminder(r.id)
    store.mark_reminder_failed(r.id)
    store.claim_reminder(r.id)
    store.mark_reminder_failed(r.id)
    assert store.get_reminder(r.id).status == "failed"


def test_instructions_and_facts(store):
    i = store.add_instruction("Always call me for medication reminders")
    assert [x.text for x in store.list_instructions()] == [i.text]
    store.remember("Doctor", "Dr. Rao")
    store.remember("doctor", "Dr. Rao, Apollo")
    assert store.facts() == {"doctor": "Dr. Rao, Apollo"}
    assert store.forget("doctor") is True
    assert store.remove_instruction(i.id) is True
    assert store.list_instructions() == []


def test_conversation_rolls_over_when_too_long(store):
    c1 = store.current_conversation(max_messages=2, idle_hours=12)
    store.append_message(c1, "user", "hi")
    assert store.current_conversation(max_messages=2, idle_hours=12) == c1
    store.append_message(c1, "assistant", [{"type": "text", "text": "hello"}])
    c2 = store.current_conversation(max_messages=2, idle_hours=12)
    assert c2 != c1
    assert store.conversation_messages(c1) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
    ]


def test_voice_jobs(store):
    job = store.create_voice_job("outbound", {"message": "hello"})
    store.update_voice_job(job.id, status="done", result="ok", payload={"message": "hello", "hang_up": True})
    j = store.get_voice_job(job.id)
    assert j.status == "done" and j.result == "ok" and j.payload["hang_up"] is True
