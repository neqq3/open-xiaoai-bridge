import asyncio
import importlib
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


class HermesStreamingTest(unittest.TestCase):
    def setUp(self):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        server = sys.modules.get("open_xiaoai_server")
        if server is None:
            server = types.SimpleNamespace()
            sys.modules["open_xiaoai_server"] = server
        if not hasattr(server, "decode_audio"):
            server.decode_audio = lambda *_args, **_kwargs: b""
        if not hasattr(server, "begin_playback_session"):
            server.begin_playback_session = lambda: 1

        module = importlib.import_module("core.hermes_conversation")
        self.module = module
        self.controller = module.HermesConversationController.__new__(
            module.HermesConversationController
        )
        self.controller.config = _Config(
            {
                "hermes": {
                    "streaming": {
                        "enabled": True,
                        "sentence_min_chars": 4,
                        "sentence_max_chars": 80,
                    },
                    "progress": {"enabled": False},
                    "input_mode": "xiaoai_asr",
                }
            }
        )
        self.controller._playback_token = None
        self.controller._stop_recording = mock.AsyncMock()
        self.controller._play_send_sound = mock.AsyncMock()

    def test_final_sentence_starts_before_stream_finishes(self):
        async def scenario():
            first_spoken = asyncio.Event()
            release_first = asyncio.Event()
            stream_completed = False
            played = []

            async def play(text):
                played.append(text)
                if len(played) == 1:
                    first_spoken.set()
                    await release_first.wait()

            async def request(
                _text,
                *,
                on_delta,
                on_tool_progress,
            ):
                nonlocal stream_completed
                del on_tool_progress
                await on_delta("第一句已经好了。")
                await first_spoken.wait()
                self.assertFalse(stream_completed)
                release_first.set()
                await on_delta("第二句也好了。")
                stream_completed = True
                return "第一句已经好了。第二句也好了。"

            self.controller._play_tts = play
            self.controller.backend = types.SimpleNamespace(
                _rule_prompt="",
                _session_key="agent:hermes:test",
                request_streaming_chat_completion=request,
            )
            result = await self.controller._request_streaming_turn(
                "问题",
                play_send_sound=False,
            )
            return result, played

        result, played = asyncio.run(scenario())

        self.assertEqual(
            ("第一句已经好了。第二句也好了。", True),
            result,
        )
        self.assertEqual(
            ["第一句已经好了。", "第二句也好了。"],
            played,
        )

    def test_repeated_tool_events_create_one_rate_limited_progress(self):
        async def request(
            _text,
            *,
            on_delta,
            on_tool_progress,
        ):
            progress_module = importlib.import_module(
                "core.hermes_progress"
            )
            for call_id in ("call-1", "call-2", "call-3"):
                await on_tool_progress(
                    progress_module.HermesToolProgress(
                        tool="web_search",
                        status="running",
                        tool_call_id=call_id,
                        label="curl https://internal.example",
                    )
                )
            await asyncio.sleep(1.05)
            await on_delta("这是最终答案。")
            return "这是最终答案。"

        self.controller.config.values["hermes"]["progress"] = {
            "enabled": True,
            "initial_delay": 1,
            "min_interval": 20,
            "max_messages": 2,
        }
        played = []
        self.controller._play_tts = mock.AsyncMock(
            side_effect=lambda text: played.append(text)
        )
        self.controller.backend = types.SimpleNamespace(
            _rule_prompt="",
            _session_key="agent:hermes:test",
            request_streaming_chat_completion=request,
        )

        asyncio.run(
            self.controller._request_streaming_turn(
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

    def test_failure_before_spoken_text_uses_non_streaming_fallback(self):
        async def request(
            _text,
            *,
            on_delta,
            on_tool_progress,
        ):
            del on_delta, on_tool_progress
            raise RuntimeError("stream unavailable")

        played = []
        self.controller._play_tts = lambda text: _append_async(
            played,
            text,
        )
        fallback = mock.AsyncMock(return_value="安全回退。")
        self.controller.backend = types.SimpleNamespace(
            _rule_prompt="",
            _session_key="agent:hermes:test",
            request_streaming_chat_completion=request,
            _request_chat_completion=fallback,
        )

        result = asyncio.run(
            self.controller._request_streaming_turn(
                "问题",
                play_send_sound=False,
            )
        )

        self.assertEqual(("安全回退。", True), result)
        self.assertEqual(["安全回退。"], played)
        fallback.assert_awaited_once_with("问题")

    def test_failure_after_spoken_text_does_not_replay_answer(self):
        async def request(
            _text,
            *,
            on_delta,
            on_tool_progress,
        ):
            del on_tool_progress
            await on_delta("已经播出的第一句。")
            raise RuntimeError("connection lost")

        played = []
        self.controller._play_tts = lambda text: _append_async(
            played,
            text,
        )
        fallback = mock.AsyncMock(return_value="不应重复的完整回答。")
        self.controller.backend = types.SimpleNamespace(
            _rule_prompt="",
            _session_key="agent:hermes:test",
            request_streaming_chat_completion=request,
            _request_chat_completion=fallback,
        )

        result = asyncio.run(
            self.controller._request_streaming_turn(
                "问题",
                play_send_sound=False,
            )
        )

        self.assertEqual(("已经播出的第一句。", True), result)
        self.assertEqual(
            ["已经播出的第一句。", "回答传输中断了，请再问一次"],
            played,
        )
        fallback.assert_not_awaited()

    def test_mode_like_phrases_are_forwarded_unchanged(self):
        self.controller.config.values["hermes"]["streaming"] = {
            "enabled": False
        }
        send = mock.AsyncMock(return_value="回答")
        self.controller.backend = types.SimpleNamespace(
            _rule_prompt="",
            send=send,
        )

        result = asyncio.run(
            self.controller._request_backend_turn(
                "详细查一下，不要简单说",
                play_send_sound=False,
            )
        )

        self.assertEqual(("回答", False), result)
        send.assert_awaited_once_with(
            "详细查一下，不要简单说",
            wait_response=True,
        )

    def test_failed_verified_tts_uses_blocking_native_fallback(self):
        backend = types.SimpleNamespace(
            _play_response_with_tts=mock.AsyncMock(
                return_value=False
            ),
            get_tts_speaker_for_session_key=mock.Mock(
                return_value="xiaoai"
            ),
        )
        speaker = mock.AsyncMock()
        speaker.play.return_value = True
        self.controller.backend = backend

        async def scenario():
            with mock.patch.object(
                self.module,
                "get_speaker",
                return_value=speaker,
            ):
                await self.controller._play_tts(
                    "兜底也必须完整播放"
                )

        asyncio.run(scenario())

        speaker.play.assert_awaited_once_with(
            text="兜底也必须完整播放",
            blocking=True,
        )
        self.assertIsNone(self.controller._playback_token)


async def _append_async(items, value):
    items.append(value)


if __name__ == "__main__":
    unittest.main()
