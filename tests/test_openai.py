import asyncio
import ast
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


class OpenAIRequestCompatibilityTest(unittest.TestCase):
    def setUp(self):
        sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        sys.modules.pop("core.openai", None)
        self.module = importlib.import_module("core.openai")
        self.manager = self.module.OpenAIManager
        self.manager._initialized = True
        self.manager._enabled = True
        self.manager._api_key = ""
        self.manager._session_header = ""
        self.manager._session_key = "plain-session"
        self.manager._system_prompt = ""
        self.manager._temperature = None
        self.manager._max_tokens = None
        self.manager._timeout = 10
        self.manager._history_max_messages = 20
        self.manager._extra_body = {}
        self.manager._sessions.clear()

    def _request(self, response_body, *, chunks=None):
        captured = {}

        class FakeContent:
            async def iter_any(self):
                for chunk in chunks or []:
                    yield chunk

        class FakeResponse:
            status = 200
            headers = {
                "Content-Type": (
                    "text/event-stream" if chunks is not None else "application/json"
                )
            }
            content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def json(self, **_kwargs):
                return response_body

        class FakeSession:
            def __init__(self, **kwargs):
                captured["session_kwargs"] = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, url, **kwargs):
                captured["url"] = url
                captured.update(kwargs)
                return FakeResponse()

        return captured, FakeSession

    def test_plain_openai_non_streaming_request_preserves_original_shape(self):
        self.manager._base_url = "https://api.example.test/v1"
        self.manager._api_key = "secret"
        self.manager._model = "gpt-compatible"
        self.manager._system_prompt = "system"
        self.manager._temperature = 0.25
        self.manager._max_tokens = 256
        captured, fake_session = self._request(
            {"choices": [{"message": {"content": "回答"}}]}
        )

        async def scenario():
            with (
                mock.patch.object(
                    self.module.aiohttp,
                    "ClientSession",
                    fake_session,
                ),
                mock.patch.object(
                    self.module.aiohttp,
                    "ClientTimeout",
                    lambda **kwargs: kwargs,
                ),
            ):
                return await self.manager._request_chat_completion("问题")

        self.assertEqual("回答", asyncio.run(scenario()))
        self.assertEqual(
            "https://api.example.test/v1/chat/completions",
            captured["url"],
        )
        self.assertEqual(
            {
                "model": "gpt-compatible",
                "messages": [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "问题"},
                ],
                "stream": False,
                "temperature": 0.25,
                "max_tokens": 256,
            },
            captured["json"],
        )
        self.assertEqual(
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer secret",
            },
            captured["headers"],
        )

    def test_ollama_style_request_needs_no_auth_or_config_change(self):
        self.manager._base_url = "http://127.0.0.1:11434/v1"
        self.manager._model = "qwen2.5:7b"
        captured, fake_session = self._request(
            {"choices": [{"message": {"content": "本地回答"}}]}
        )

        async def scenario():
            with (
                mock.patch.object(
                    self.module.aiohttp,
                    "ClientSession",
                    fake_session,
                ),
                mock.patch.object(
                    self.module.aiohttp,
                    "ClientTimeout",
                    lambda **kwargs: kwargs,
                ),
            ):
                return await self.manager._request_chat_completion("本地问题")

        self.assertEqual("本地回答", asyncio.run(scenario()))
        self.assertEqual(
            "http://127.0.0.1:11434/v1/chat/completions",
            captured["url"],
        )
        self.assertEqual("qwen2.5:7b", captured["json"]["model"])
        self.assertFalse(captured["json"]["stream"])
        self.assertEqual(
            {"Content-Type": "application/json"},
            captured["headers"],
        )

    def test_standard_openai_sse_is_parsed_without_vendor_semantics(self):
        self.manager._base_url = "http://127.0.0.1:1234/v1"
        self.manager._model = "local-model"
        payload = (
            'data: {"choices":[{"delta":{"content":"第一句。"},'
            '"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{"content":"第二句。"},'
            '"finish_reason":"stop"}]}\n\n'
            "data: [DONE]\n\n"
        ).encode()
        captured, fake_session = self._request(
            None,
            chunks=[payload[:31], payload[31:87], payload[87:]],
        )
        deltas = []

        async def scenario():
            with (
                mock.patch.object(
                    self.module.aiohttp,
                    "ClientSession",
                    fake_session,
                ),
                mock.patch.object(
                    self.module.aiohttp,
                    "ClientTimeout",
                    lambda **kwargs: kwargs,
                ),
                mock.patch.object(self.module.logger, "ai_response"),
            ):
                return await self.manager.request_streaming_chat_completion(
                    "问题",
                    on_delta=lambda value: deltas.append(value),
                )

        self.assertEqual("第一句。第二句。", asyncio.run(scenario()))
        self.assertEqual(["第一句。", "第二句。"], deltas)
        self.assertTrue(captured["json"]["stream"])
        self.assertEqual(
            {"Content-Type": "application/json"},
            captured["headers"],
        )


class MainAppCompatibilityTest(unittest.TestCase):
    def test_hermes_flag_is_appended_after_existing_positional_arguments(self):
        tree = ast.parse((ROOT / "core" / "app.py").read_text(encoding="utf-8"))
        main_app = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MainApp"
        )
        expected = [
            "enable_xiaozhi",
            "enable_openclaw",
            "enable_openai",
            "enable_qwenpaw",
            "enable_hermes",
        ]
        for method_name in ("instance", "__init__"):
            method = next(
                node
                for node in main_app.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == method_name
            )
            argument_names = [argument.arg for argument in method.args.args]
            self.assertEqual(expected, argument_names[-len(expected) :])


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
