import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class NativeTTSTest(unittest.TestCase):
    def setUp(self):
        sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        sys.modules.pop("core.services.speaker", None)
        module = importlib.import_module("core.services.speaker")
        self.command_result = module.CommandResult
        self.speaker = module.SpeakerManager()

    def test_completed_script_is_success(self):
        stdout = "\n".join(
            [
                "2026-07-26 22:34:08[/usr/sbin/tts_play.sh] - Starting play flow",
                "2026-07-26 22:34:10[/usr/sbin/tts_play.sh] - Audio playback completed successfully",
                "2026-07-26 22:34:10[/usr/sbin/tts_play.sh] - Script interrupted, performing cleanup...",
            ]
        )
        self.speaker.run_shell = mock.AsyncMock(
            return_value=self.command_result(stdout, "", 0)
        )

        result = asyncio.run(self.speaker.play(text="正常播放", blocking=True))

        self.assertTrue(result)

    def test_exit_zero_without_completion_marker_is_failure(self):
        stdout = "\n".join(
            [
                "2026-07-26 22:34:08[/usr/sbin/tts_play.sh] - Starting play flow",
                "2026-07-26 22:34:08[/usr/sbin/tts_play.sh] - Text to speech: 被提前终止",
                "2026-07-26 22:34:09[/usr/sbin/tts_play.sh] - Script interrupted, performing cleanup...",
            ]
        )
        self.speaker.run_shell = mock.AsyncMock(
            return_value=self.command_result(stdout, "", 0)
        )

        with mock.patch("core.services.speaker.logger.warning") as warning:
            result = asyncio.run(self.speaker.play(text="被提前终止", blocking=True))

        self.assertFalse(result)
        diagnostic = warning.call_args.args[0]
        self.assertIn("exit_code=0", diagnostic)
        self.assertIn("[TTS text omitted]", diagnostic)
        self.assertNotIn("被提前终止", diagnostic)
        self.assertIn("Script interrupted", diagnostic)

    def test_diagnostic_output_is_compact_and_bounded(self):
        output = "first\n\nsecond " + ("x" * 1300)

        diagnostic = self.speaker._diagnostic_output(output)

        self.assertTrue(diagnostic.startswith("first second "))
        self.assertTrue(diagnostic.endswith("…"))
        self.assertLessEqual(
            len(diagnostic),
            self.speaker._TTS_DIAGNOSTIC_LIMIT + 1,
        )

    def test_native_tts_normalizes_message_breaking_characters(self):
        self.speaker.run_shell = mock.AsyncMock(
            return_value=self.command_result("", "", 0)
        )

        result = asyncio.run(
            self.speaker.play(
                text='史称"六朝古都"。\n路径 C:\\temp\t完成',
                blocking=True,
            )
        )

        self.assertTrue(result)
        command = self.speaker.run_shell.await_args.args[0]
        self.assertIn("史称“六朝古都“。", command)
        self.assertIn("路径 C:／temp 完成", command)
        self.assertNotIn('"', command)
        self.assertNotIn("\n", command)

    def test_long_native_tts_is_split_and_played_sequentially(self):
        completed = "\n".join(
            [
                "2026-07-26 22:34:08[/usr/sbin/tts_play.sh] - Starting play flow",
                "2026-07-26 22:34:10[/usr/sbin/tts_play.sh] - Audio playback completed successfully",
            ]
        )
        self.speaker.run_shell = mock.AsyncMock(
            return_value=self.command_result(completed, "", 0)
        )
        text = (
            "第一段介绍南京的历史文化和城市发展，内容较为丰富。"
            "第二段介绍南京的产业、交通和教育资源，继续补充信息。"
        ) * 5

        result = asyncio.run(self.speaker.play(text=text, blocking=True))

        self.assertTrue(result)
        chunks = self.speaker._split_native_tts_text(
            self.speaker._normalize_native_tts_text(text)
        )
        self.assertGreater(len(chunks), 1)
        self.assertTrue(
            all(
                len(chunk) <= self.speaker._NATIVE_TTS_MAX_CHARS
                for chunk in chunks
            )
        )
        self.assertEqual(len(chunks), self.speaker.run_shell.await_count)

    def test_legacy_script_keeps_exit_code_compatibility(self):
        self.speaker.run_shell = mock.AsyncMock(
            return_value=self.command_result("", "", 0)
        )

        result = asyncio.run(self.speaker.play(text="旧版脚本", blocking=True))

        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
