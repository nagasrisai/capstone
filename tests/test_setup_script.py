"""Smoke test for scripts/setup_twilio.py against a mocked Twilio client."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("setup_twilio", ROOT / "scripts" / "setup_twilio.py")
setup_twilio = importlib.util.module_from_spec(spec)
sys.modules["setup_twilio"] = setup_twilio
spec.loader.exec_module(setup_twilio)


def fake_client(owned=None, verified=False):
    c = MagicMock()
    c.api.accounts.return_value.fetch.return_value = SimpleNamespace(type="Trial", friendly_name="My First Twilio Account")
    c.balance.fetch.return_value = SimpleNamespace(balance="14.50", currency="USD")
    c.incoming_phone_numbers.list.return_value = owned or []
    c.available_phone_numbers.return_value.local.list.return_value = [SimpleNamespace(phone_number="+15550009999")]
    bought = SimpleNamespace(sid="PN1", phone_number="+15550009999", voice_url=None, voice_method=None, capabilities={"voice": True})
    c.incoming_phone_numbers.create.return_value = bought
    c.outgoing_caller_ids.list.return_value = [SimpleNamespace()] if verified else []
    c.validation_requests.create.return_value = SimpleNamespace(validation_code="123456")
    return c


def test_full_setup_on_fresh_trial_account(settings, tmp_path, monkeypatch, capsys):
    env = tmp_path / ".env"
    env.write_text("TWILIO_VOICE_FROM=\n")
    monkeypatch.setattr(setup_twilio, "ENV_PATH", env)
    settings.public_base_url = "https://example.ngrok.app"
    settings.twilio_voice_from = None
    settings.anthropic_api_key = "sk-ant-test"
    client = fake_client()

    assert setup_twilio.check_account(client, settings) is True
    number = setup_twilio.ensure_voice_number(client, settings, assume_yes=True)
    assert number == "+15550009999"
    client.incoming_phone_numbers.create.assert_called_once_with(phone_number="+15550009999", friendly_name="Personal assistant")
    client.incoming_phone_numbers.return_value.update.assert_called_once_with(
        voice_url="https://example.ngrok.app/webhooks/voice/inbound", voice_method="POST"
    )
    assert "TWILIO_VOICE_FROM=+15550009999" in env.read_text()

    setup_twilio.ensure_verified_caller_id(client, settings)
    client.validation_requests.create.assert_called_once()
    out = capsys.readouterr().out
    assert "123456" in out and "Trial" in out


def test_existing_number_and_verified_owner_need_no_changes(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(setup_twilio, "ENV_PATH", tmp_path / ".env")
    settings.public_base_url = "https://example.ngrok.app"
    settings.twilio_voice_from = "+15550002222"
    owned = [SimpleNamespace(sid="PN9", phone_number="+15550002222", voice_url="https://example.ngrok.app/webhooks/voice/inbound", voice_method="POST", capabilities={"voice": True})]
    client = fake_client(owned=owned, verified=True)
    assert setup_twilio.ensure_voice_number(client, settings, assume_yes=False) == "+15550002222"
    client.incoming_phone_numbers.create.assert_not_called()
    client.incoming_phone_numbers.return_value.update.assert_not_called()
    setup_twilio.ensure_verified_caller_id(client, settings)
    client.validation_requests.create.assert_not_called()


def test_write_env_replaces_or_appends(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("A=1\nTWILIO_VOICE_FROM=old\n")
    monkeypatch.setattr(setup_twilio, "ENV_PATH", env)
    setup_twilio.write_env("TWILIO_VOICE_FROM", "+1")
    setup_twilio.write_env("NEW_KEY", "x")
    assert env.read_text() == "A=1\nTWILIO_VOICE_FROM=+1\nNEW_KEY=x\n"
