"""Runtime configuration, loaded from environment variables or a .env file."""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Claude -----------------------------------------------------------
    anthropic_api_key: str | None = None
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"  # low | medium | high | xhigh | max
    claude_max_tokens: int = 8000

    # --- Owner (the single person this assistant works for) ----------------
    owner_name: str = "Boss"
    owner_phone: str = ""  # E.164, e.g. +919876543210
    owner_whatsapp: str | None = None  # defaults to "whatsapp:" + owner_phone
    owner_timezone: str = "UTC"

    # --- Twilio -----------------------------------------------------------
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_whatsapp_from: str | None = None  # e.g. whatsapp:+14155238886 (sandbox)
    twilio_voice_from: str | None = None  # e.g. +15017122661
    validate_twilio_signature: bool = True
    voice_language: str = "en-US"
    voice_name: str = "Polly.Joanna"

    # --- Server -----------------------------------------------------------
    public_base_url: str = "http://localhost:8000"  # public HTTPS URL Twilio can reach
    database_path: str = "data/assistant.db"
    reminder_poll_seconds: int = 15
    voice_poll_timeout_seconds: int = 75

    # --- Behaviour --------------------------------------------------------
    default_clarification_channel: str = "whatsapp"  # whatsapp | call
    max_conversation_messages: int = 80
    conversation_idle_hours: int = 12

    @field_validator("claude_effort")
    @classmethod
    def _check_effort(cls, v: str) -> str:
        v = (v or "").strip().lower() or "medium"
        if v not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError("claude_effort must be one of low, medium, high, xhigh, max")
        return v

    @field_validator("default_clarification_channel")
    @classmethod
    def _check_channel(cls, v: str) -> str:
        v = (v or "").strip().lower() or "whatsapp"
        if v not in {"whatsapp", "call"}:
            raise ValueError("default_clarification_channel must be 'whatsapp' or 'call'")
        return v

    @property
    def owner_whatsapp_address(self) -> str:
        if self.owner_whatsapp:
            return self.owner_whatsapp if self.owner_whatsapp.startswith("whatsapp:") else f"whatsapp:{self.owner_whatsapp}"
        return f"whatsapp:{self.owner_phone}"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.owner_timezone)

    @property
    def twilio_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    def public_url(self, path: str) -> str:
        return self.public_base_url.rstrip("/") + "/" + path.lstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
