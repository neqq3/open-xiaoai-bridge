"""Hermes Agent continuous conversation controller."""

from core.hermes import HermesManager
from core.hermes_progress import HermesProgressNarrator
from core.streaming_conversation import StreamingConversationController


class HermesConversationController(StreamingConversationController):
    """Hermes session with structured progress and streamed final speech."""

    CONFIG_PREFIX = "hermes"
    BACKEND_NAME = "Hermes"
    LOG_MODULE = "Hermes Conv"
    WAKEUP_SOURCE = "hermes"
    MANAGER = HermesManager
    STREAMING_DEFAULT = True

    def _progress_config(self):
        value = self._cfg("progress", {})
        return value if isinstance(value, dict) else {}

    def _create_progress_narrator(self, text: str):
        return HermesProgressNarrator(text)

    async def _request_stream(
        self,
        prompt: str,
        *,
        on_delta,
        on_progress,
    ) -> str | None:
        return await self.backend.request_streaming_chat_completion(
            prompt,
            on_delta=on_delta,
            on_tool_progress=on_progress,
        )
