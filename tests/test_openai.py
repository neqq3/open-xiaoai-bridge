import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class OpenAIHeadersTest(unittest.TestCase):
    def setUp(self):
        sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())
        sys.modules.setdefault(
            "aiohttp",
            types.SimpleNamespace(ClientSession=object, ClientTimeout=object),
        )
        sys.modules.setdefault("requests", types.SimpleNamespace(post=None, get=None))
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        sys.modules.pop("core.openai", None)
        self.manager = importlib.import_module("core.openai").OpenAIManager
        self.manager._api_key = ""
        self.manager._session_key = "agent:default:open-xiaoai-bridge"

    def test_default_does_not_send_vendor_session_header(self):
        self.manager._session_header = ""
        self.assertEqual(
            {"Content-Type": "application/json"},
            self.manager._headers(),
        )

    def test_empty_session_header_disables_it(self):
        """Setting session_header empty keeps requests header-free (plain OpenAI)."""
        self.manager._session_header = ""
        self.assertEqual({"Content-Type": "application/json"}, self.manager._headers())

    def test_vendor_session_header_sent_only_when_configured(self):
        self.manager._session_header = "X-Vendor-Session"
        headers = self.manager._headers()
        self.assertEqual(
            "agent:default:open-xiaoai-bridge",
            headers["X-Vendor-Session"],
        )

    def test_session_header_omitted_when_session_key_empty(self):
        self.manager._session_header = "X-Vendor-Session"
        self.manager._session_key = ""
        self.assertNotIn("X-Vendor-Session", self.manager._headers())

    def test_bearer_auth_and_session_header_coexist(self):
        self.manager._api_key = "secret"
        self.manager._session_header = "X-Vendor-Session"
        headers = self.manager._headers()
        self.assertEqual("Bearer secret", headers["Authorization"])
        self.assertEqual(
            "agent:default:open-xiaoai-bridge",
            headers["X-Vendor-Session"],
        )


class _Config:
    def __init__(self, values):
        self.values = values

    def get_app_config(self, path, default=None):
        value = self.values
        for key in path.split("."):
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value


class OpenAIConversationCompatibilityTest(unittest.TestCase):
    def setUp(self):
        sys.modules["open_xiaoai_server"] = types.SimpleNamespace(
            begin_playback_session=lambda: 1,
            stop_tts_playback=lambda _token: None,
            decode_audio=lambda *_args, **_kwargs: b"",
        )
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        for name in (
            "core.external_conversation",
            "core.openai",
            "core.streaming_conversation",
            "core.openai_conversation",
        ):
            sys.modules.pop(name, None)
        self.controller_class = importlib.import_module(
            "core.openai_conversation"
        ).OpenAIConversationController

    def test_default_path_remains_non_streaming_and_prompt_unchanged(self):
        backend = types.SimpleNamespace(
            _session_key="plain",
            _rule_prompt="原有规则",
            send=mock.AsyncMock(return_value="原有回答"),
            request_streaming_chat_completion=mock.AsyncMock(),
        )
        controller = self.controller_class.__new__(self.controller_class)
        controller.config = _Config(
            {
                "openai": {
                    "input_mode": "xiaoai_asr",
                    "streaming": {"enabled": False},
                },
                "wakeup": {"timeout": 20},
            }
        )
        controller.backend = backend
        controller._playback_token = None

        result = asyncio.run(
            controller._request_backend_turn(
                "简单说一下这个问题",
                play_send_sound=False,
            )
        )

        self.assertEqual(("原有回答", False), result)
        backend.send.assert_awaited_once_with(
            "简单说一下这个问题\n原有规则",
            wait_response=True,
        )
        backend.request_streaming_chat_completion.assert_not_awaited()

    def test_optional_standard_stream_uses_common_ordered_delivery(self):
        async def stream(_prompt, *, on_delta):
            await on_delta("第一句。")
            await on_delta("第二句。")
            return "第一句。第二句。"

        backend = types.SimpleNamespace(
            _session_key="plain",
            _rule_prompt="",
            send=mock.AsyncMock(),
            request_streaming_chat_completion=stream,
            _request_chat_completion=mock.AsyncMock(),
        )
        controller = self.controller_class.__new__(self.controller_class)
        controller.config = _Config(
            {
                "openai": {
                    "input_mode": "xiaoai_asr",
                    "streaming": {
                        "enabled": True,
                        "sentence_min_chars": 2,
                    },
                },
                "wakeup": {"timeout": 20},
            }
        )
        controller.backend = backend
        controller._playback_token = None
        controller._stop_recording = mock.AsyncMock()
        controller._play_send_sound = mock.AsyncMock()
        controller._play_tts = mock.AsyncMock()

        with mock.patch(
            "core.streaming_conversation.logger.user_speech"
        ):
            response, already_played = asyncio.run(
                controller._request_backend_turn(
                    "原始问题",
                    play_send_sound=False,
                )
            )

        self.assertEqual("第一句。第二句。", response)
        self.assertTrue(already_played)
        self.assertEqual(
            ["第一句。", "第二句。"],
            [
                call.args[0]
                for call in controller._play_tts.await_args_list
            ],
        )

if __name__ == "__main__":
    unittest.main()
