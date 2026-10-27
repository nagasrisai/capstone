from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from assistant.channels import ConsoleChannels
from assistant.config import Settings
from assistant.store import Store


class FakeResponse:
    def __init__(self, blocks: list[dict[str, Any]], stop_reason: str, model: str = "fake-model"):
        self.content = blocks
        self.stop_reason = stop_reason
        self.model = model


class FakeClaude:
    """Scripted stand-in for anthropic.Anthropic: returns queued responses and records requests."""

    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> FakeResponse:
        self.requests.append(copy.deepcopy(kwargs))
        if not self._responses:
            raise AssertionError("FakeClaude ran out of scripted responses")
        return self._responses.pop(0)


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def tool_use_block(name: str, inp: dict[str, Any], tool_id: str = "toolu_1") -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def thinking_block() -> dict[str, Any]:
    return {"type": "thinking", "thinking": "", "signature": "sig123"}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        owner_name="Sai",
        owner_phone="+15550001111",
        owner_timezone="Asia/Kolkata",
        database_path=str(tmp_path / "test.db"),
        validate_twilio_signature=False,
        twilio_whatsapp_from="whatsapp:+14155238886",
        twilio_voice_from="+15550002222",
        reminder_poll_seconds=1,
        voice_poll_timeout_seconds=30,
    )


@pytest.fixture
def store(settings) -> Store:
    s = Store(settings.database_path)
    yield s
    s.close()


@pytest.fixture
def channels() -> ConsoleChannels:
    return ConsoleChannels()
