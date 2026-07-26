import asyncio
import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class OpenAIVoiceHelpersTest(unittest.TestCase):
    def setUp(self):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        self.voice = importlib.import_module("core.openai_voice")

    def test_persistent_and_one_shot_modes_are_distinct(self):
        switch = self.voice.interpret_voice_mode("切换快速模式。", "standard")
        self.assertTrue(switch.is_switch_command)
        self.assertEqual("fast", switch.mode)
        self.assertIsNone(switch.query)

        one_shot = self.voice.interpret_voice_mode(
            "详细查一下明天南京要不要带伞",
            "fast",
        )
        self.assertFalse(one_shot.persistent)
        self.assertEqual("deep", one_shot.mode)
        self.assertEqual("明天南京要不要带伞", one_shot.query)

        next_turn = self.voice.interpret_voice_mode("再说一个笑话", "fast")
        self.assertEqual("fast", next_turn.mode)

    def test_sentence_chunker_preserves_order_and_waits_for_boundaries(self):
        chunker = self.voice.SentenceChunker(min_chars=4, max_chars=20)

        self.assertEqual([], chunker.feed("第一句话还"))
        self.assertEqual(["第一句话还没结束。"], chunker.feed("没结束。第二"))
        self.assertEqual(["第二句话完成！"], chunker.feed("句话完成！"))
        self.assertEqual(["最后半句"], chunker.feed("最后半句", final=True))

    def test_sse_decoder_handles_split_blocks_and_custom_events(self):
        decoder = self.voice.SSEDecoder()
        first = decoder.feed(
            'event: hermes.tool.progress\ndata: {"tool":"web_search",'
        )
        self.assertEqual([], first)

        events = decoder.feed(
            '"status":"running"}\n\n'
            'data: {"choices":[{"delta":{"content":"答案。"},'
            '"finish_reason":null}]}\n\n'
        )
        self.assertEqual(2, len(events))
        self.assertEqual("hermes.tool.progress", events[0].event)
        self.assertEqual("web_search", json.loads(events[0].data)["tool"])
        delta, finish_reason = self.voice.extract_openai_delta(events[1].data)
        self.assertEqual("答案。", delta)
        self.assertIsNone(finish_reason)

    def test_tool_progress_uses_safe_categories_only(self):
        self.assertEqual("research", self.voice.safe_tool_category("web_extract"))
        self.assertEqual("weather", self.voice.safe_tool_category("get_weather"))
        self.assertEqual("working", self.voice.safe_tool_category("private_tool"))

    def test_tool_transport_drops_preview_arguments_and_urls(self):
        sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())
        sys.modules.setdefault(
            "aiohttp",
            types.SimpleNamespace(ClientSession=object, ClientTimeout=object),
        )
        manager = importlib.import_module("core.openai").OpenAIManager

        event = manager._decode_tool_progress(
            json.dumps(
                {
                    "tool": "web_search",
                    "status": "running",
                    "toolCallId": "call-1",
                    "preview": "https://private.example/path",
                    "args": {"query": "sensitive raw query"},
                }
            )
        )

        self.assertEqual(
            {
                "tool": "web_search",
                "status": "running",
                "tool_call_id": "call-1",
            },
            event,
        )


class _Config:
    def __init__(self, openai_config):
        self.openai_config = openai_config

    def get_app_config(self, path, default=None):
        if path == "openai":
            return self.openai_config
        if path.startswith("openai."):
            value = self.openai_config
            for key in path.split(".")[1:]:
                if not isinstance(value, dict) or key not in value:
                    return default
                value = value[key]
            return value
        if path == "wakeup.timeout":
            return 20
        return default


class OpenAIVoiceControllerTest(unittest.TestCase):
    def setUp(self):
        sys.modules["open_xiaoai_server"] = types.SimpleNamespace(
            begin_playback_session=lambda: 1,
            stop_tts_playback=lambda _token: None,
            decode_audio=lambda *_args, **_kwargs: b"",
        )
        sys.modules.setdefault(
            "aiohttp",
            types.SimpleNamespace(ClientSession=object, ClientTimeout=object),
        )
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        for name in (
            "core.external_conversation",
            "core.openai",
            "core.openai_conversation",
        ):
            sys.modules.pop(name, None)
        module = importlib.import_module("core.openai_conversation")
        self.controller_class = module.OpenAIConversationController
        user_speech_patch = mock.patch.object(module.logger, "user_speech")
        user_speech_patch.start()
        self.addCleanup(user_speech_patch.stop)

    def _controller(self, voice_config, backend):
        controller = self.controller_class.__new__(self.controller_class)
        controller.config = _Config(
            {
                "voice": voice_config,
                "input_mode": "xiaoai_asr",
            }
        )
        controller.backend = backend
        controller.active = True
        controller._session_voice_modes = {}
        controller._playback_token = None
        controller._stop_recording = mock.AsyncMock()
        controller._play_send_sound = mock.AsyncMock()
        controller._play_tts = mock.AsyncMock()
        return controller

    def test_persistent_switch_is_local_and_does_not_call_backend(self):
        backend = types.SimpleNamespace(
            _session_key="agent:default:speaker",
        )
        controller = self._controller(
            {"enabled": True, "default_mode": "standard"},
            backend,
        )

        result = asyncio.run(
            controller._request_backend_turn(
                "切换快速模式",
                play_send_sound=False,
            )
        )

        self.assertEqual(("已切换到快速模式", False), result)
        self.assertEqual("fast", controller._current_voice_mode())

    def test_disabled_voice_feature_preserves_original_prompt(self):
        backend = types.SimpleNamespace(
            _session_key="agent:default:speaker",
            _rule_prompt="原有规则",
            send=mock.AsyncMock(return_value="原有回答"),
        )
        controller = self._controller(
            {"enabled": False},
            backend,
        )

        result = asyncio.run(
            controller._request_backend_turn(
                "原始问题",
                play_send_sound=False,
            )
        )

        self.assertEqual(("原有回答", False), result)
        backend.send.assert_awaited_once_with(
            "原始问题\n原有规则",
            wait_response=True,
        )

    def test_plain_openai_keeps_non_streaming_delivery(self):
        backend = types.SimpleNamespace(
            _session_key="agent:default:speaker",
            _send_and_track=mock.AsyncMock(return_value="run-1"),
            _wait_response=mock.AsyncMock(return_value="简短答案"),
        )
        controller = self._controller(
            {
                "enabled": True,
                "default_mode": "fast",
                "hermes": {"enabled": False},
            },
            backend,
        )

        result = asyncio.run(
            controller._request_backend_turn(
                "今天几号",
                play_send_sound=True,
            )
        )

        self.assertEqual(("简短答案", False), result)
        sent_prompt = backend._send_and_track.await_args.args[0]
        self.assertIn("语音快速模式", sent_prompt)
        controller._play_send_sound.assert_awaited_once()

    def test_stream_starts_tts_before_full_response_and_keeps_order(self):
        first_played = asyncio.Event()
        stream_finished = False

        async def stream_request(_prompt, *, on_delta, on_tool_progress):
            nonlocal stream_finished
            await on_tool_progress(
                {"tool": "web_search", "status": "running"}
            )
            await on_delta("第一句已经形成。")
            await asyncio.wait_for(first_played.wait(), timeout=1)
            self.assertFalse(stream_finished)
            await on_delta("第二句随后形成。")
            stream_finished = True
            return "第一句已经形成。第二句随后形成。"

        backend = types.SimpleNamespace(
            _session_key="agent:default:speaker",
            request_streaming_chat_completion=stream_request,
            _request_chat_completion=mock.AsyncMock(),
        )
        controller = self._controller(
            {
                "enabled": True,
                "default_mode": "standard",
                "hermes": {
                    "enabled": True,
                    "streaming": True,
                    "sentence_min_chars": 4,
                    "progress": {"enabled": False},
                },
            },
            backend,
        )
        played = []

        async def play(text):
            played.append(text)
            if len(played) == 1:
                first_played.set()

        controller._play_tts.side_effect = play

        response, already_played = asyncio.run(
            controller._request_backend_turn(
                "请回答",
                play_send_sound=False,
            )
        )

        self.assertTrue(already_played)
        self.assertEqual("第一句已经形成。第二句随后形成。", response)
        self.assertEqual(
            ["第一句已经形成。", "第二句随后形成。"],
            played,
        )

    def test_stream_failure_before_answer_falls_back_without_duplication(self):
        async def broken_stream(_prompt, *, on_delta, on_tool_progress):
            await on_tool_progress(
                {"tool": "web_search", "status": "running"}
            )
            raise RuntimeError("stream unsupported")

        backend = types.SimpleNamespace(
            _session_key="agent:default:speaker",
            request_streaming_chat_completion=broken_stream,
            _request_chat_completion=mock.AsyncMock(
                return_value="回退后的完整回答。"
            ),
        )
        controller = self._controller(
            {
                "enabled": True,
                "default_mode": "standard",
                "hermes": {
                    "enabled": True,
                    "streaming": True,
                    "sentence_min_chars": 4,
                    "progress": {"enabled": False},
                },
            },
            backend,
        )

        response, already_played = asyncio.run(
            controller._request_backend_turn(
                "请回答",
                play_send_sound=False,
            )
        )

        self.assertEqual("回退后的完整回答。", response)
        self.assertTrue(already_played)
        controller._play_tts.assert_awaited_once_with("回退后的完整回答。")

    def test_repeated_tool_events_produce_one_rate_limited_status(self):
        async def slow_stream(_prompt, *, on_delta, on_tool_progress):
            for _ in range(3):
                await on_tool_progress(
                    {"tool": "web_search", "status": "running"}
                )
            await asyncio.sleep(1.1)
            await on_delta("这是最终答案。")
            return "这是最终答案。"

        backend = types.SimpleNamespace(
            _session_key="agent:default:speaker",
            request_streaming_chat_completion=slow_stream,
            _request_chat_completion=mock.AsyncMock(),
        )
        controller = self._controller(
            {
                "enabled": True,
                "default_mode": "deep",
                "hermes": {
                    "enabled": True,
                    "streaming": True,
                    "sentence_min_chars": 4,
                    "progress": {
                        "enabled": True,
                        "initial_delay": 1,
                        "min_interval": 20,
                        "max_messages": 2,
                    },
                },
            },
            backend,
        )
        played = []
        controller._play_tts.side_effect = lambda text: played.append(text)

        asyncio.run(
            controller._request_backend_turn(
                "查一下实时行情",
                play_send_sound=False,
            )
        )

        self.assertEqual(
            ["我正在查询并核对相关信息", "这是最终答案。"],
            played,
        )


class OpenAIStreamingTransportTest(unittest.TestCase):
    def setUp(self):
        sys.modules["open_xiaoai_server"] = types.SimpleNamespace()
        sys.modules.setdefault(
            "aiohttp",
            types.SimpleNamespace(ClientSession=object, ClientTimeout=object),
        )
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        sys.modules.pop("core.openai", None)
        self.module = importlib.import_module("core.openai")
        self.manager = self.module.OpenAIManager
        self.manager._initialized = True
        self.manager._enabled = True
        self.manager._base_url = "http://example.test/v1"
        self.manager._api_key = ""
        self.manager._model = "hermes-agent"
        self.manager._session_key = "agent:default:test"
        self.manager._session_header = "X-Hermes-Session-Key"
        self.manager._system_prompt = ""
        self.manager._temperature = None
        self.manager._max_tokens = None
        self.manager._timeout = 10
        self.manager._history_max_messages = 20
        self.manager._extra_body = {}
        self.manager._sessions.clear()

    def test_transport_parses_split_text_and_sanitized_tool_events(self):
        payload = (
            'event: hermes.tool.progress\n'
            'data: {"tool":"web_search","status":"running",'
            '"toolCallId":"call-1","preview":"https://secret.example",'
            '"args":{"query":"raw"}}\n\n'
            'data: {"choices":[{"delta":{"role":"assistant"},'
            '"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{"content":"第一句。"},'
            '"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{"content":"第二句。"},'
            '"finish_reason":null}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            'data: [DONE]\n\n'
        ).encode()
        chunks = [payload[:71], payload[71:143], payload[143:]]

        class FakeContent:
            async def iter_any(self):
                for chunk in chunks:
                    yield chunk

        class FakeResponse:
            status = 200
            headers = {"Content-Type": "text/event-stream"}
            content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class FakeSession:
            def __init__(self, **_kwargs):
                self.request_payload = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, _url, *, json, headers):
                self.request_payload = (json, headers)
                return FakeResponse()

        deltas = []
        tool_events = []

        async def scenario():
            with (
                mock.patch.object(
                    self.module.aiohttp,
                    "ClientSession",
                    FakeSession,
                ),
                mock.patch.object(
                    self.module.aiohttp,
                    "ClientTimeout",
                    lambda **_kwargs: object(),
                ),
                mock.patch.object(self.module.logger, "ai_response"),
            ):
                return await self.manager.request_streaming_chat_completion(
                    "测试流式",
                    on_delta=lambda delta: deltas.append(delta),
                    on_tool_progress=lambda event: tool_events.append(event),
                )

        response = asyncio.run(scenario())

        self.assertEqual("第一句。第二句。", response)
        self.assertEqual(["第一句。", "第二句。"], deltas)
        self.assertEqual(
            [
                {
                    "tool": "web_search",
                    "status": "running",
                    "tool_call_id": "call-1",
                }
            ],
            tool_events,
        )
        self.assertNotIn("preview", tool_events[0])
        self.assertEqual(
            [
                {"role": "user", "content": "测试流式"},
                {"role": "assistant", "content": "第一句。第二句。"},
            ],
            self.manager._sessions["agent:default:test"],
        )


if __name__ == "__main__":
    unittest.main()
