import asyncio
import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


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


class HermesProgressTest(unittest.TestCase):
    def setUp(self):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        self.progress = importlib.import_module("core.hermes_progress")

    def test_tool_label_and_internal_name_never_enter_spoken_message(self):
        event = self.progress.HermesToolProgress.from_json(
            json.dumps(
                {
                    "tool": "web_search",
                    "status": "running",
                    "toolCallId": "call-1",
                    "label": "curl https://private.example?q=secret",
                    "emoji": "🔍",
                }
            )
        )
        narrator = self.progress.HermesProgressNarrator("查一下今天的新闻")
        narrator.observe(event)
        message, key = narrator.next_message()

        self.assertEqual("research", key)
        self.assertEqual("正在查最新资料", message)
        self.assertNotIn("web_search", message)
        self.assertNotIn("http", message)
        self.assertNotIn("curl", message)
        technical = event.technical_log()
        self.assertNotIn("private.example", technical["label"])

    def test_repeated_category_is_deduplicated_then_reports_new_phase(self):
        narrator = self.progress.HermesProgressNarrator("查一下资料")
        first = self.progress.HermesToolProgress(
            "web_search", "running", "call-1"
        )
        second = self.progress.HermesToolProgress(
            "web_extract", "running", "call-2"
        )
        narrator.observe(first)
        narrator.observe(second)
        self.assertEqual(
            ("正在查最新资料", "research"),
            narrator.next_message(),
        )
        self.assertIsNone(narrator.next_message())

        narrator.observe(
            self.progress.HermesToolProgress(
                "web_search", "completed", "call-1"
            )
        )
        self.assertEqual(
            ("已经找到一些信息，正在整理", "organizing"),
            narrator.next_message(),
        )
        self.assertIsNone(narrator.next_message())

    def test_task_context_can_classify_unknown_tool_safely(self):
        narrator = self.progress.HermesProgressNarrator(
            "看看家里的设备状态"
        )
        narrator.observe(
            self.progress.HermesToolProgress(
                "private_mcp_42", "running", "call-1"
            )
        )
        self.assertEqual(
            ("正在查看家里的设备状态", "home"),
            narrator.next_message(),
        )


class HermesManagerTest(unittest.TestCase):
    def setUp(self):
        sys.modules["open_xiaoai_server"] = types.SimpleNamespace()
        sys.modules.setdefault(
            "aiohttp",
            types.SimpleNamespace(ClientSession=object, ClientTimeout=object),
        )
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        for name in ("core.openai", "core.hermes"):
            sys.modules.pop(name, None)
        self.openai_module = importlib.import_module("core.openai")
        self.openai = self.openai_module.OpenAIManager
        self.module = importlib.import_module("core.hermes")
        self.hermes = self.module.HermesManager

    def test_openai_and_hermes_session_state_are_isolated(self):
        self.openai._session_header = ""
        self.openai._session_key = "plain-session"
        self.hermes._session_header = "X-Hermes-Session-Key"
        self.hermes._session_key = "agent:default:speaker"
        self.openai._sessions.clear()
        self.hermes._sessions.clear()
        self.openai._sessions["plain-session"] = [{"role": "user", "content": "A"}]

        self.assertNotIn("X-Hermes-Session-Key", self.openai._headers())
        self.assertEqual(
            "agent:default:speaker",
            self.hermes._headers()["X-Hermes-Session-Key"],
        )
        self.assertEqual({}, self.hermes._sessions)

    def test_transport_parses_full_hermes_tool_lifecycle(self):
        payload = (
            'event: hermes.tool.progress\n'
            'data: {"tool":"web_search","status":"running",'
            '"toolCallId":"call-1","label":"Search https://example.test",'
            '"emoji":"x"}\n\n'
            'data: {"choices":[{"delta":{"content":"最终答案。"},'
            '"finish_reason":null}]}\n\n'
            'event: hermes.tool.progress\n'
            'data: {"tool":"web_search","status":"completed",'
            '"toolCallId":"call-1"}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            'data: [DONE]\n\n'
        ).encode()
        chunks = [payload[:80], payload[80:190], payload[190:]]

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
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def post(self, *_args, **_kwargs):
                return FakeResponse()

        self.hermes._initialized = True
        self.hermes._enabled = True
        self.hermes._base_url = "http://example.test/v1"
        self.hermes._api_key = ""
        self.hermes._model = "hermes-agent"
        self.hermes._session_key = "agent:default:test"
        self.hermes._session_header = "X-Hermes-Session-Key"
        self.hermes._system_prompt = ""
        self.hermes._temperature = None
        self.hermes._max_tokens = None
        self.hermes._timeout = 10
        self.hermes._history_max_messages = 20
        self.hermes._extra_body = {}
        self.hermes._sessions.clear()
        deltas = []
        events = []

        async def scenario():
            with (
                mock.patch.object(
                    self.openai_module.aiohttp,
                    "ClientSession",
                    FakeSession,
                ),
                mock.patch.object(
                    self.openai_module.aiohttp,
                    "ClientTimeout",
                    lambda **_kwargs: object(),
                ),
                mock.patch.object(self.module.logger, "ai_response"),
            ):
                return await self.hermes.request_streaming_chat_completion(
                    "测试",
                    on_delta=lambda value: deltas.append(value),
                    on_tool_progress=lambda event: events.append(event),
                )

        response = asyncio.run(scenario())
        self.assertEqual("最终答案。", response)
        self.assertEqual(["最终答案。"], deltas)
        self.assertEqual(["running", "completed"], [event.status for event in events])
        self.assertEqual("Search https://example.test", events[0].label)


class HermesConversationTest(unittest.TestCase):
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
            "core.hermes",
            "core.streaming_conversation",
            "core.hermes_conversation",
        ):
            sys.modules.pop(name, None)
        module = importlib.import_module("core.hermes_conversation")
        self.controller_class = module.HermesConversationController
        stream_module = importlib.import_module("core.streaming_conversation")
        user_speech_patch = mock.patch.object(stream_module.logger, "user_speech")
        user_speech_patch.start()
        self.addCleanup(user_speech_patch.stop)

    def _controller(self, hermes_config, backend):
        controller = self.controller_class.__new__(self.controller_class)
        controller.config = _Config(
            {
                "hermes": {
                    "input_mode": "xiaoai_asr",
                    **hermes_config,
                },
                "wakeup": {"timeout": 20},
            }
        )
        controller.backend = backend
        controller.active = True
        controller._playback_token = None
        controller._stop_recording = mock.AsyncMock()
        controller._play_send_sound = mock.AsyncMock()
        controller._play_tts = mock.AsyncMock()
        return controller

    def test_natural_simple_or_detailed_phrasing_reaches_hermes_unchanged(self):
        backend = types.SimpleNamespace(
            _session_key="agent:default:speaker",
            _rule_prompt="只返回纯文字",
            send=mock.AsyncMock(return_value="回答"),
        )
        controller = self._controller(
            {"streaming": {"enabled": False}},
            backend,
        )

        result = asyncio.run(
            controller._request_backend_turn(
                "详细查一下明天的天气",
                play_send_sound=False,
            )
        )

        self.assertEqual(("回答", False), result)
        backend.send.assert_awaited_once_with(
            "详细查一下明天的天气\n只返回纯文字",
            wait_response=True,
        )

    def test_final_sentences_start_before_stream_completion_in_order(self):
        first_played = asyncio.Event()
        stream_finished = False

        async def stream_request(_prompt, *, on_delta, on_tool_progress):
            nonlocal stream_finished
            await on_delta("第一句已经形成。")
            await asyncio.wait_for(first_played.wait(), timeout=1)
            self.assertFalse(stream_finished)
            await on_delta("第二句随后形成。")
            stream_finished = True
            return "第一句已经形成。第二句随后形成。"

        backend = types.SimpleNamespace(
            _session_key="agent:default:speaker",
            _rule_prompt="",
            request_streaming_chat_completion=stream_request,
            _request_chat_completion=mock.AsyncMock(),
        )
        controller = self._controller(
            {
                "streaming": {
                    "enabled": True,
                    "sentence_min_chars": 4,
                },
                "progress": {"enabled": False},
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

    def test_failure_before_spoken_answer_falls_back_once(self):
        async def broken_stream(_prompt, *, on_delta, on_tool_progress):
            await on_delta("未形成完整句")
            raise RuntimeError("stream unsupported")

        backend = types.SimpleNamespace(
            _session_key="agent:default:speaker",
            _rule_prompt="",
            request_streaming_chat_completion=broken_stream,
            _request_chat_completion=mock.AsyncMock(
                return_value="回退后的完整回答。"
            ),
        )
        controller = self._controller(
            {
                "streaming": {
                    "enabled": True,
                    "sentence_min_chars": 20,
                },
                "progress": {"enabled": False},
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

    def test_repeated_tool_events_create_one_natural_rate_limited_progress(self):
        async def slow_stream(_prompt, *, on_delta, on_tool_progress):
            for call_id in ("call-1", "call-2", "call-3"):
                await on_tool_progress(
                    importlib.import_module(
                        "core.hermes_progress"
                    ).HermesToolProgress(
                        "web_search",
                        "running",
                        call_id,
                        label="curl https://internal.example",
                    )
                )
            await asyncio.sleep(1.05)
            await on_delta("这是最终答案。")
            return "这是最终答案。"

        backend = types.SimpleNamespace(
            _session_key="agent:default:speaker",
            _rule_prompt="",
            request_streaming_chat_completion=slow_stream,
            _request_chat_completion=mock.AsyncMock(),
        )
        controller = self._controller(
            {
                "streaming": {
                    "enabled": True,
                    "sentence_min_chars": 4,
                },
                "progress": {
                    "enabled": True,
                    "initial_delay": 1,
                    "min_interval": 20,
                    "max_messages": 2,
                },
            },
            backend,
        )
        played = []
        controller._play_tts.side_effect = lambda text: played.append(text)

        asyncio.run(
            controller._request_backend_turn(
                "查一下最新资料",
                play_send_sound=False,
            )
        )

        self.assertEqual(
            ["正在查最新资料", "这是最终答案。"],
            played,
        )
        self.assertNotIn("web_search", "".join(played))
        self.assertNotIn("http", "".join(played))

    def test_mid_stream_failure_does_not_replay_already_spoken_answer(self):
        async def broken_stream(_prompt, *, on_delta, on_tool_progress):
            await on_delta("第一句已经播出。")
            raise RuntimeError("connection lost")

        backend = types.SimpleNamespace(
            _session_key="agent:default:speaker",
            _rule_prompt="",
            request_streaming_chat_completion=broken_stream,
            _request_chat_completion=mock.AsyncMock(
                return_value="不同措辞的完整回答。"
            ),
        )
        controller = self._controller(
            {
                "streaming": {
                    "enabled": True,
                    "sentence_min_chars": 4,
                },
                "progress": {"enabled": False},
            },
            backend,
        )
        played = []
        controller._play_tts.side_effect = lambda text: played.append(text)

        response, already_played = asyncio.run(
            controller._request_backend_turn(
                "请回答",
                play_send_sound=False,
            )
        )

        self.assertEqual("第一句已经播出。", response)
        self.assertTrue(already_played)
        backend._request_chat_completion.assert_not_awaited()
        self.assertEqual(
            ["第一句已经播出。", "回答传输中断了，请再问一次"],
            played,
        )


class HermesWakeupRouteTest(unittest.TestCase):
    def setUp(self):
        sys.modules["open_xiaoai_server"] = types.SimpleNamespace()
        sys.modules.setdefault(
            "aiohttp",
            types.SimpleNamespace(ClientSession=object, ClientTimeout=object),
        )
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        sys.modules.pop("core.wakeup_session", None)
        self.module = importlib.import_module("core.wakeup_session")

    def test_hermes_has_explicit_wakeup_route(self):
        async def before_wakeup(*_args):
            return "hermes"

        manager = self.module.WakeupSessionManager()
        manager.config = _Config(
            {
                "wakeup": {"before_wakeup": before_wakeup},
                "openclaw": {},
                "openai": {},
                "hermes": {"session_key": "agent:default:test"},
                "qwenpaw": {},
            }
        )
        manager.reset_all_sessions = mock.AsyncMock()
        manager._start_hermes_conversation = mock.AsyncMock()

        async def scenario():
            with (
                mock.patch.object(self.module, "get_kws", return_value=None),
                mock.patch.object(self.module, "get_speaker", return_value=object()),
                mock.patch.object(self.module, "get_app", return_value=object()),
            ):
                await manager.wakeup("超人迪迦", "kws")

        asyncio.run(scenario())
        manager.reset_all_sessions.assert_awaited_once()
        manager._start_hermes_conversation.assert_awaited_once()

    def test_default_config_routes_diga_to_hermes_without_changing_openai(self):
        config_module = importlib.import_module("config")
        speaker = mock.AsyncMock()
        app = types.SimpleNamespace()

        diga = asyncio.run(
            config_module.before_wakeup(
                speaker,
                "超人迪迦",
                "kws",
                app,
            )
        )
        openai = asyncio.run(
            config_module.before_wakeup(
                speaker,
                "你好小黑",
                "kws",
                app,
            )
        )

        self.assertEqual("hermes", diga)
        self.assertEqual("openai", openai)


if __name__ == "__main__":
    unittest.main()
