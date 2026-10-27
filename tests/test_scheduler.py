from datetime import timedelta

from assistant.scheduler import ReminderScheduler
from assistant.store import utcnow


def test_due_reminders_delivered_on_each_channel(settings, store, channels):
    past = utcnow() - timedelta(seconds=5)
    future = utcnow() + timedelta(hours=1)
    wa = store.create_reminder("Drink water", past, "whatsapp")
    call = store.create_reminder("Leave for airport", past, "call", notes="Terminal 2")
    both = store.create_reminder("Standup", past, "both")
    store.create_reminder("Later", future, "whatsapp")

    scheduler = ReminderScheduler(store, channels, settings)
    assert scheduler.deliver_due() == 3
    assert scheduler.deliver_due() == 0  # nothing left

    assert any("Drink water" in m for m in channels.whatsapp_sent)
    assert any("Standup" in m for m in channels.whatsapp_sent)
    assert len(channels.calls_started) == 2
    job = store.get_voice_job(channels.calls_started[0])
    assert job.kind == "reminder" and "Leave for airport" in job.payload["message"] and "Terminal 2" in job.payload["message"]
    assert job.payload["reminder_id"] == call.id and job.payload["wait_for_reply"] is True
    for r in (wa, call, both):
        assert store.get_reminder(r.id).status == "delivered"


def test_delivery_failure_is_retried(settings, store, channels):
    r = store.create_reminder("x", utcnow() - timedelta(seconds=1), "whatsapp")

    class Broken:
        def send_whatsapp(self, text):
            raise RuntimeError("twilio down")

        def start_call(self, job_id):
            raise RuntimeError("twilio down")

    scheduler = ReminderScheduler(store, Broken(), settings)
    assert scheduler.deliver_due() == 0
    assert store.get_reminder(r.id).status == "pending" and store.get_reminder(r.id).fail_count == 1
