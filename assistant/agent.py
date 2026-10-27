"""The Claude-powered agent loop.

One owner, one running conversation. Every turn:
  1. append the tagged user message to the stored conversation,
  2. call Claude with the tools,
  3. execute requested tools and feed results back until Claude stops,
  4. return the final text (and whether a hang-up was requested on a call).

History is append-only: assistant content blocks (thinking included) are stored verbatim and
replayed unchanged, as the API requires.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

import anthropic

from .channels.base import Channels
from .config import Settings
from .prompts import dynamic_context, stable_system_prompt, tag_user_message
from .store import Store
from .tools import TOOL_DEFINITIONS, RunContext, ToolExecutor

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOOL_ITERATIONS = 15


@dataclass
class AgentReply:
    text: str
    hang_up: bool = False
    stop_reason: str | None = None


def _block_to_param(block: Any) -> dict[str, Any]:
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    if isinstance(block, dict):
        return block
    raise TypeError(f"Cannot serialize content block of type {type(block)!r}")


def _split_at_last_fallback(blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (blocks_before_last_fallback, blocks_after). Both empty-prefix when no fallback block exists."""
    idx = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1)
    if idx < 0:
        return [], blocks
    return blocks[: idx + 1], blocks[idx + 1 :]


def _sanitize_for_replay(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """After a mid-output refusal fallback, model-internal blocks before the boundary must not be echoed back."""
    before, after = _split_at_last_fallback(blocks)
    if not before:
        return blocks
    keep = [b for b in before if b.get("type") not in {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}]
    return keep + after


def _text_of(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


class Assistant:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        channels: Channels,
        client: anthropic.Anthropic | None = None,
    ):
        self.settings = settings
        self.store = store
        self.channels = channels
        self.client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.tools = ToolExecutor(store, channels, settings)
        self._lock = threading.Lock()
        self._stable_prompt = stable_system_prompt(settings)

    # ------------------------------------------------------------------ public API
    def handle(self, text: str, channel: str) -> AgentReply:
        """Process one owner message arriving over `channel` ('whatsapp' | 'voice' | 'console')."""
        with self._lock:
            conv_id = self._conversation_id()
            self.store.append_message(conv_id, "user", tag_user_message(text, channel, self.settings))
            return self._run(conv_id, RunContext(channel=channel))

    def record_event(self, text: str) -> None:
        """Append a system-originated note (e.g. 'reminder delivered') so the next turn knows about it."""
        with self._lock:
            conv_id = self._conversation_id()
            self.store.append_message(conv_id, "user", tag_user_message(f"[event] {text}", "system", self.settings))

    # ------------------------------------------------------------------ internals
    def _conversation_id(self) -> str:
        return self.store.current_conversation(
            self.settings.max_conversation_messages, self.settings.conversation_idle_hours
        )

    def _system(self) -> list[dict[str, Any]]:
        dyn = dynamic_context(
            self.settings,
            self.store.list_instructions(),
            self.store.facts(),
            self.store.list_reminders(include_done=False, limit=20),
        )
        return [
            {"type": "text", "text": self._stable_prompt, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": dyn},
        ]

    def _create(self, messages: list[dict[str, Any]]):
        return self.client.beta.messages.create(
            model=self.settings.claude_model,
            max_tokens=self.settings.claude_max_tokens,
            system=self._system(),
            messages=messages,
            tools=TOOL_DEFINITIONS,
            output_config={"effort": self.settings.claude_effort},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )

    def _run(self, conv_id: str, ctx: RunContext) -> AgentReply:
        messages = self.store.conversation_messages(conv_id)
        last_text = ""
        for iteration in range(MAX_TOOL_ITERATIONS):
            try:
                response = self._create(messages)
            except anthropic.RateLimitError:
                log.warning("Rate limited by the Claude API")
                return AgentReply("I'm being rate-limited right now. Please try again in a minute.", stop_reason="error")
            except anthropic.APIStatusError as e:
                log.error("Claude API error %s: %s", e.status_code, e.message)
                return AgentReply("I hit a problem talking to my brain. Please try again shortly.", stop_reason="error")
            except anthropic.APIConnectionError:
                log.error("Could not reach the Claude API")
                return AgentReply("I can't reach my brain right now. Please try again shortly.", stop_reason="error")

            blocks = [_block_to_param(b) for b in response.content]
            stop = response.stop_reason
            log.info("turn=%d stop_reason=%s model=%s", iteration, stop, getattr(response, "model", "?"))

            if stop == "refusal":
                if blocks:
                    self.store.append_message(conv_id, "assistant", _sanitize_for_replay(blocks))
                text = _text_of(blocks) or "I can't help with that one."
                return AgentReply(text, hang_up=ctx.hang_up_requested, stop_reason=stop)

            replay = _sanitize_for_replay(blocks)
            self.store.append_message(conv_id, "assistant", replay)
            messages.append({"role": "assistant", "content": replay})
            last_text = _text_of(blocks) or last_text

            if stop == "pause_turn":
                continue

            if stop == "max_tokens":
                log.warning("Response truncated at max_tokens")
                return AgentReply(last_text or "My answer got cut off; could you ask again?", hang_up=ctx.hang_up_requested, stop_reason=stop)

            if stop != "tool_use":
                return AgentReply(last_text, hang_up=ctx.hang_up_requested, stop_reason=stop)

            # Only tool calls after a fallback boundary are live; earlier ones were discarded by the API.
            _, live = _split_at_last_fallback(blocks)
            tool_uses = [b for b in live if b.get("type") == "tool_use"]
            if not tool_uses:
                return AgentReply(last_text, hang_up=ctx.hang_up_requested, stop_reason=stop)

            results = []
            for tu in tool_uses:
                tool_input = tu.get("input") or {}
                if not isinstance(tool_input, dict):
                    out, is_err = f"Invalid tool input: {tool_input!r}", True
                else:
                    out, is_err = self.tools.execute(tu["name"], tool_input, ctx)
                log.info("tool=%s error=%s", tu["name"], is_err)
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": out, "is_error": is_err})
            self.store.append_message(conv_id, "user", results)
            messages.append({"role": "user", "content": results})

        log.warning("Tool iteration limit reached")
        return AgentReply(
            last_text or "I got stuck in a loop working on that. Could you rephrase?",
            hang_up=ctx.hang_up_requested,
            stop_reason="iteration_limit",
        )
