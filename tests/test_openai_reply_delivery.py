import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class OpenAIReplyDeliveryTest(unittest.TestCase):
    def setUp(self):
        sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())
        sys.modules.setdefault(
            "aiohttp",
            types.SimpleNamespace(ClientSession=object, ClientTimeout=object),
        )
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        sys.modules.pop("core.openai", None)
        self.manager = importlib.import_module("core.openai").OpenAIManager
        self.manager._response_events.clear()
        self.manager._response_texts.clear()
        self.manager._response_tts_speakers.clear()

    def test_extracts_standard_openai_final_message(self):
        body = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "最终天气回答",
                    }
                }
            ]
        }

        self.assertEqual(
            "最终天气回答",
            self.manager._extract_response_text(body),
        )

    def test_extracts_segmented_final_message(self):
        body = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "最终"},
                            {"type": "tool_result", "text": "不应混入"},
                            {"type": "text", "text": "回答"},
                        ],
                    }
                }
            ]
        }

        self.assertEqual("最终回答", self.manager._extract_response_text(body))

    def test_delayed_final_response_is_not_dropped(self):
        async def scenario():
            run_id = "slow-openai-run"
            loop = asyncio.get_running_loop()
            self.manager._timeout = 1
            self.manager._response_events[run_id] = loop.create_future()
            self.manager._response_texts[run_id] = ""

            async def finish_later():
                await asyncio.sleep(0.05)
                self.manager._response_texts[run_id] = "延迟后的最终回答"
                self.manager._response_events[run_id].set_result(None)

            asyncio.create_task(finish_later())
            return await self.manager._wait_response(run_id)

        self.assertEqual("延迟后的最终回答", asyncio.run(scenario()))

    def test_native_tts_retries_once_after_verified_failure(self):
        speaker = mock.AsyncMock()
        speaker.play.side_effect = [False, True]
        self.manager._tts_speaker = self.manager.XIAOAI_TTS_SPEAKER

        async def scenario():
            with (
                mock.patch("core.ref.get_speaker", return_value=speaker),
                mock.patch("core.openai.asyncio.sleep", new=mock.AsyncMock()),
            ):
                return await self.manager._play_response_with_tts(
                    "这条回复必须播出来",
                    tts_speaker=self.manager.XIAOAI_TTS_SPEAKER,
                )

        self.assertTrue(asyncio.run(scenario()))
        self.assertEqual(2, speaker.play.await_count)


if __name__ == "__main__":
    unittest.main()
