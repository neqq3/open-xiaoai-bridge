"""Hermes Agent backend built on the OpenAI-compatible transport."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from core.hermes_progress import HermesToolProgress
from core.openai import OpenAIManager
from core.utils.logger import logger


class HermesManager(OpenAIManager):
    """Hermes-specific session and structured-event adapter."""

    CONFIG_PREFIX = "hermes"
    ENV_ENABLE = "HERMES_ENABLE"
    LOG_NAME = "Hermes"
    DEFAULT_BASE_URL = "http://127.0.0.1:8642/v1"
    DEFAULT_MODEL = "hermes-agent"
    DEFAULT_SESSION_KEY = "agent:default:open-xiaoai-bridge"
    DEFAULT_SESSION_HEADER = "X-Hermes-Session-Key"

    # Runtime state must not be inherited from a simultaneously enabled
    # OpenAIManager instance.
    _initialized = False
    _reload_listener_registered = False
    _enabled = False
    _base_url = DEFAULT_BASE_URL
    _api_key = ""
    _model = DEFAULT_MODEL
    _session_key = DEFAULT_SESSION_KEY
    _session_header = DEFAULT_SESSION_HEADER
    _system_prompt = ""
    _temperature = None
    _max_tokens = None
    _timeout = 120
    _history_max_messages = 20
    _extra_body: dict[str, Any] = {}
    _tts_speaker = None
    _session_tts_speakers: dict[str, str] = {}
    _tts_speed = 1.0
    _rule_prompt = ""
    _rule_prompt_for_skill = ""
    _sessions: dict[str, list[dict[str, str]]] = {}
    _response_events: dict[str, Any] = {}
    _response_texts: dict[str, str] = {}
    _response_tts_speakers: dict[str, str | None] = {}
    last_error: str | None = None

    @classmethod
    async def request_streaming_chat_completion(
        cls,
        text: str,
        *,
        on_delta: Callable[[str], Awaitable[None] | None],
        on_tool_progress: (
            Callable[[HermesToolProgress], Awaitable[None] | None] | None
        ) = None,
    ) -> str | None:
        """Stream standard answer deltas plus Hermes tool lifecycle events."""

        async def on_event(event):
            if event.event != "hermes.tool.progress":
                logger.debug(
                    f"[Hermes] Ignoring unsupported SSE event: {event.event}"
                )
                return
            try:
                progress = HermesToolProgress.from_json(event.data)
            except Exception as exc:
                logger.debug(
                    f"[Hermes] Ignoring malformed tool progress event: {exc}"
                )
                return
            if not progress:
                return
            # Preserve technical lifecycle detail for diagnostics. The spoken
            # narrator receives the typed event and never reads label directly.
            logger.debug(
                f"[Hermes] Tool progress event: {progress.technical_log()}"
            )
            if on_tool_progress:
                await cls._invoke_callback(on_tool_progress, progress)

        return await super().request_streaming_chat_completion(
            text,
            on_delta=on_delta,
            on_event=on_event,
        )
