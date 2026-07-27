"""Hermes Agent backend built on the OpenAI-compatible transport."""

from __future__ import annotations

import asyncio
from typing import Any

from core.openai import OpenAIManager
from core.utils.base import get_env
from core.utils.config import ConfigManager
from core.utils.logger import logger


class HermesManager(OpenAIManager):
    """Hermes-specific backend with isolated configuration and session state."""

    CONFIG_PREFIX = "hermes"
    ENV_ENABLE = "HERMES_ENABLE"
    LOG_NAME = "Hermes"
    DEFAULT_BASE_URL = "http://127.0.0.1:8642/v1"
    DEFAULT_MODEL = "hermes-agent"
    DEFAULT_SESSION_KEY = "agent:default:open-xiaoai-bridge"
    DEFAULT_SESSION_HEADER = "X-Hermes-Session-Key"

    # Hermes and generic OpenAI may be enabled together, so every mutable
    # runtime field must belong to this subclass rather than the parent.
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
    _response_events: dict[str, asyncio.Future] = {}
    _response_texts: dict[str, str] = {}
    _response_tts_speakers: dict[str, str | None] = {}
    last_error: str | None = None

    @classmethod
    def initialize_from_config(cls, enabled: bool | None = None):
        logger.info("[Hermes] Initializing from config...")
        cls.reload_from_config(enabled=enabled)
        cls._initialized = True

    @classmethod
    def reload_from_config(cls, enabled: bool | None = None):
        """Refresh Hermes settings without touching generic OpenAI state."""

        config_manager = ConfigManager.instance()
        if not cls._reload_listener_registered:
            config_manager.add_reload_listener(
                lambda _old, _new: cls.reload_from_config()
            )
            cls._reload_listener_registered = True

        config = config_manager.get_app_config(cls.CONFIG_PREFIX, {})
        if enabled is not None:
            cls._enabled = enabled
        else:
            env_enabled = get_env(cls.ENV_ENABLE)
            cls._enabled = (
                env_enabled.lower() in ("1", "true", "yes")
                if env_enabled is not None
                else False
            )

        cls._base_url = str(
            config.get("base_url", cls.DEFAULT_BASE_URL)
        ).rstrip("/")
        cls._api_key = str(config.get("api_key", "") or "")
        cls._model = str(config.get("model", cls.DEFAULT_MODEL))
        cls._session_key = str(
            config.get("session_key", cls.DEFAULT_SESSION_KEY)
        )
        cls._session_header = str(
            config.get("session_header", cls.DEFAULT_SESSION_HEADER) or ""
        ).strip()
        cls._system_prompt = str(config.get("system_prompt", "") or "")
        cls._timeout = int(config.get("response_timeout", 120))
        cls._history_max_messages = max(
            0,
            int(config.get("history_max_messages", 20)),
        )
        cls._temperature = cls._optional_float(config.get("temperature"))
        cls._max_tokens = cls._optional_int(config.get("max_tokens"))
        cls._extra_body = config.get("extra_body", {})
        if not isinstance(cls._extra_body, dict):
            cls._extra_body = {}
        cls._tts_speaker = config.get("tts_speaker", None)
        cls._session_tts_speakers = (
            {
                str(key): str(value)
                for key, value in config.get(
                    "session_tts_speakers",
                    {},
                ).items()
                if key and value
            }
            if isinstance(config.get("session_tts_speakers", {}), dict)
            else {}
        )
        cls._tts_speed = float(config.get("tts_speed", 1.0))
        cls._rule_prompt = str(config.get("rule_prompt", "") or "")
        cls._rule_prompt_for_skill = str(
            config.get("rule_prompt_for_skill", "") or ""
        )

        if cls._enabled:
            logger.info(
                f"[Hermes] Enabled, base_url={cls._base_url}, "
                f"model={cls._model}"
            )

    @classmethod
    def set_session_key(cls, session_key: str):
        logger.info(
            f"[Hermes] Session key updated: "
            f"{cls._session_key!r} -> {session_key!r}"
        )
        cls._session_key = session_key
