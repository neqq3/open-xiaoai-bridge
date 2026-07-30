"""Hermes Agent 连续对话控制器。"""

import open_xiaoai_server

from core.hermes import HermesManager
from core.hermes_progress import HermesProgressNarrator
from core.ref import get_speaker
from core.streaming_conversation import StreamingConversationController
from core.utils.logger import logger


class HermesConversationController(StreamingConversationController):
    """支持结构化进度和流式最终语音的 Hermes 会话。"""

    CONFIG_PREFIX = "hermes"
    BACKEND_NAME = "Hermes"
    LOG_MODULE = "Hermes Conv"
    WAKEUP_SOURCE = "hermes"
    MANAGER = HermesManager
    STREAMING_DEFAULT = True

    async def start(self):
        if not self.active:
            self.backend.begin_conversation()
        await super().start()

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

    async def _play_tts(self, text: str):
        """播放一条排队中的 Hermes 语音，失败时不静默吞掉异常。"""

        self._playback_token = open_xiaoai_server.begin_playback_session()
        try:
            played = await self.backend._play_response_with_tts(
                text,
                tts_speaker=self.backend.get_tts_speaker_for_session_key(),
                playback_token=self._playback_token,
            )
            if played:
                return

            speaker = get_speaker()
            if not speaker or not await speaker.play(
                text=text,
                blocking=True,
            ):
                raise RuntimeError("Hermes TTS playback did not complete")
        except Exception as exc:
            logger.error(
                f"TTS playback error: {type(exc).__name__}: {exc}",
                module=self.LOG_MODULE,
            )
            raise
        finally:
            self._playback_token = None
