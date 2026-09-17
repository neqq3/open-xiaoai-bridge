"""验证共享会话接线，尤其是原厂 TTS 结果不明时不能进入旧的重复播报兜底。"""

import importlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from core.services.native_visual import NativeVisualUnavailable


class VisualConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = types.SimpleNamespace(
            decode_audio=lambda *a, **k: b"",
            begin_playback_session=Mock(return_value=42),
        )
        with patch.dict(sys.modules, {"open_xiaoai_server": self.server}):
            self.module = importlib.import_module("core.external_conversation")
        self.controller = object.__new__(self.module.ExternalConversationController)
        self.controller._visual = types.SimpleNamespace(clear=AsyncMock(), speak=AsyncMock())
        self.controller.backend = types.SimpleNamespace(
            get_tts_speaker_for_session_key=lambda: "xiaoai", _play_response_with_tts=AsyncMock()
        )
        self.controller._playback_token = None

    async def test_native_tts_failure_does_not_replay_via_backend_or_speaker(self):
        self.controller._visual.speak.side_effect = NativeVisualUnavailable("unknown completion")
        speaker = types.SimpleNamespace(play=AsyncMock())
        with patch.object(self.module, "get_speaker", return_value=speaker):
            with self.assertRaises(NativeVisualUnavailable):
                await self.controller._play_tts("只播一次")
        self.controller.backend._play_response_with_tts.assert_not_awaited()
        speaker.play.assert_not_awaited()

    async def test_enabled_native_tts_waits_then_returns(self):
        await self.controller._play_tts("测试")
        self.controller._visual.speak.assert_awaited_once_with("测试")
        self.controller.backend._play_response_with_tts.assert_not_awaited()

    async def test_disabled_visual_preserves_existing_tts(self):
        self.controller._visual = None
        with patch.object(self.module, "open_xiaoai_server", self.server):
            await self.controller._play_tts("测试")
        self.controller.backend._play_response_with_tts.assert_awaited_once_with(
            "测试", tts_speaker="xiaoai", playback_token=42
        )
        self.assertIsNone(self.controller._playback_token)


if __name__ == "__main__":
    unittest.main()
