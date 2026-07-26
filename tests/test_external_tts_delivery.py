import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class ExternalTTSDeliveryTest(unittest.TestCase):
    def setUp(self):
        sys.modules["open_xiaoai_server"] = types.SimpleNamespace(
            begin_playback_session=lambda: 7,
        )
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        sys.modules.pop("core.external_conversation", None)
        self.module = importlib.import_module("core.external_conversation")

    def test_failed_backend_uses_blocking_native_fallback(self):
        backend = types.SimpleNamespace(
            _play_response_with_tts=mock.AsyncMock(return_value=False),
            get_tts_speaker_for_session_key=mock.Mock(return_value="xiaoai"),
        )
        speaker = mock.AsyncMock()
        speaker.play.return_value = True
        controller = self.module.ExternalConversationController.__new__(
            self.module.ExternalConversationController
        )
        controller.backend = backend
        controller._playback_token = None

        async def scenario():
            with mock.patch(
                "core.external_conversation.get_speaker",
                return_value=speaker,
            ):
                await controller._play_tts("兜底也必须完整播放")

        asyncio.run(scenario())

        speaker.play.assert_awaited_once_with(
            text="兜底也必须完整播放",
            blocking=True,
        )
        self.assertIsNone(controller._playback_token)


if __name__ == "__main__":
    unittest.main()
