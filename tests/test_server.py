import re
import threading
import time
from datetime import timedelta

from fastapi.testclient import TestClient

from assistant.agent import Assistant
from assistant.server import build_app

from .conftest import FakeClaude, FakeResponse, text_block, tool_use_block


def make_client(settings, store, channels, responses, delay=0.0):
    class SlowClaude(FakeClaude):
        def _create(self, **kwargs):
            if delay:
                time.sleep(delay)
            return super()._create(**kwargs)

    assistant = Assistant(settings, store, channels, client=SlowClaude(responses))
    app = build_app(settings, store=store, channels=channels, assistant=assistant, run_scheduler=False)
    return TestClient(app)


def wait_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_health(settings, store, channels):
    with make_client(settings, store, channels, []) as client:
        assert client.get("/health").json()["ok"] is True


def test_whatsapp_webhook_replies_in_background(settings, store, channels):
    responses = [FakeResponse([text_block("Hello Sai, noted.")], "end_turn")]
    with make_client(settings, store, channels, responses) as client:
        r = client.post("/webhooks/whatsapp", data={"From": settings.owner_whatsapp_address, "Body": "note this", "NumMedia": "0"})
        assert r.status_code == 200 and r.text == "<Response/>"
        assert wait_until(lambda: channels.whatsapp_sent)
    assert channels.whatsapp_sent == ["Hello Sai, noted."]


def test_whatsapp_from_stranger_is_ignored(settings, store, channels):
    with make_client(settings, store, channels, [FakeResponse([text_block("x")], "end_turn")]) as client:
        r = client.post("/webhooks/whatsapp", data={"From": "whatsapp:+10000000000", "Body": "hi"})
        assert r.status_code == 200
        time.sleep(0.2)
    assert channels.whatsapp_sent == []


def test_signature_required_when_enabled(settings, store, channels):
    settings.validate_twilio_signature = True
    settings.twilio_auth_token = "secret"
    with make_client(settings, store, channels, []) as client:
        r = client.post("/webhooks/whatsapp", data={"From": settings.owner_whatsapp_address, "Body": "hi"})
        assert r.status_code == 403


def test_outbound_call_script_for_question(settings, store, channels):
    job = store.create_voice_job("outbound", {"message": "Which Friday did you mean?", "wait_for_reply": True})
    with make_client(settings, store, channels, []) as client:
        r = client.post(f"/webhooks/voice/outbound/{job.id}", data={"CallSid": "CA1"})
    assert r.status_code == 200
    assert "<Gather" in r.text and "Which Friday did you mean?" in r.text
    assert "/webhooks/voice/turn" in r.text
    assert store.get_voice_job(job.id).status == "answered"


def test_outbound_call_script_statement_hangs_up(settings, store, channels):
    job = store.create_voice_job("outbound", {"message": "Your cab is here.", "wait_for_reply": False})
    with make_client(settings, store, channels, []) as client:
        r = client.post(f"/webhooks/voice/outbound/{job.id}", data={})
    assert "<Gather" not in r.text and "<Hangup" in r.text and "Your cab is here." in r.text


def test_voice_turn_then_poll_speaks_answer_and_listens(settings, store, channels):
    responses = [FakeResponse([text_block("Sure, I moved it to next Friday.")], "end_turn")]
    with make_client(settings, store, channels, responses) as client:
        r = client.post("/webhooks/voice/turn", data={"SpeechResult": "next Friday please"})
        assert r.status_code == 200 and "<Redirect" in r.text
        poll_url = re.search(r"<Redirect method=\"POST\">([^<]+)</Redirect>", r.text).group(1)
        job_id = poll_url.rsplit("/", 1)[1]
        assert wait_until(lambda: store.get_voice_job(job_id).status == "done")
        r2 = client.post(f"/webhooks/voice/poll/{job_id}", data={})
    assert "Sure, I moved it to next Friday." in r2.text and "<Gather" in r2.text


def test_voice_turn_hang_up_ends_call(settings, store, channels):
    responses = [
        FakeResponse([tool_use_block("hang_up", {})], "tool_use"),
        FakeResponse([text_block("Bye for now.")], "end_turn"),
    ]
    with make_client(settings, store, channels, responses) as client:
        r = client.post("/webhooks/voice/turn", data={"SpeechResult": "that's all"})
        job_id = re.search(r"/poll/([0-9a-f]+)", r.text).group(1)
        assert wait_until(lambda: store.get_voice_job(job_id).status == "done")
        r2 = client.post(f"/webhooks/voice/poll/{job_id}", data={})
    assert "Bye for now." in r2.text and "<Hangup" in r2.text and "<Gather" not in r2.text


def test_voice_poll_waits_while_agent_thinks(settings, store, channels):
    responses = [FakeResponse([text_block("done")], "end_turn")]
    with make_client(settings, store, channels, responses, delay=0.6) as client:
        r = client.post("/webhooks/voice/turn", data={"SpeechResult": "hello"})
        job_id = re.search(r"/poll/([0-9a-f]+)", r.text).group(1)
        r2 = client.post(f"/webhooks/voice/poll/{job_id}", data={})
        assert "<Redirect" in r2.text and "<Pause" in r2.text
        assert wait_until(lambda: store.get_voice_job(job_id).status == "done")


def test_voice_poll_timeout_falls_back_to_whatsapp(settings, store, channels):
    settings.voice_poll_timeout_seconds = 0
    responses = [FakeResponse([text_block("Here is the long answer.")], "end_turn")]
    with make_client(settings, store, channels, responses, delay=0.5) as client:
        r = client.post("/webhooks/voice/turn", data={"SpeechResult": "hard question"})
        job_id = re.search(r"/poll/([0-9a-f]+)", r.text).group(1)
        time.sleep(0.05)
        r2 = client.post(f"/webhooks/voice/poll/{job_id}", data={})
        assert "<Hangup" in r2.text and store.get_voice_job(job_id).status == "abandoned"
        assert wait_until(lambda: channels.whatsapp_sent)
    assert channels.whatsapp_sent == ["Here is the long answer."]


def test_inbound_call_from_owner_is_greeted(settings, store, channels):
    with make_client(settings, store, channels, []) as client:
        ok = client.post("/webhooks/voice/inbound", data={"From": settings.owner_phone})
        bad = client.post("/webhooks/voice/inbound", data={"From": "+10000000000"})
    assert "<Gather" in ok.text and "Hi Sai" in ok.text
    assert "<Hangup" in bad.text and "<Gather" not in bad.text
