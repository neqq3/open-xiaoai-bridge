import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class VerifiedNativeTTSTest(unittest.TestCase):
    def setUp(self):
        sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        sys.modules.pop("core.services.speaker", None)
        module = importlib.import_module("core.services.speaker")
        self.command_result = module.CommandResult
        self.speaker = module.SpeakerManager()

    def test_existing_play_method_keeps_upstream_exit_code_behavior(self):
        stdout = (
            "2026[/usr/sbin/tts_play.sh] - Script interrupted"
        )
        self.speaker.run_shell = mock.AsyncMock(
            return_value=self.command_result(stdout, "", 0)
        )

        result = asyncio.run(
            self.speaker.play(text="普通路径", blocking=True)
        )

        self.assertTrue(result)
        self.speaker.run_shell.assert_awaited_once()

    def test_verified_path_retries_missing_completion_marker(self):
        interrupted = (
            "2026[/usr/sbin/tts_play.sh] - Script interrupted"
        )
        completed = "\n".join(
            [
                "2026[/usr/sbin/tts_play.sh] - Starting play flow",
                "2026[/usr/sbin/tts_play.sh] - "
                "Audio playback completed successfully",
            ]
        )
        self.speaker.run_shell = mock.AsyncMock(
            side_effect=[
                self.command_result(interrupted, "", 0),
                self.command_result(completed, "", 0),
            ]
        )

        with mock.patch(
            "core.services.speaker.asyncio.sleep",
            new=mock.AsyncMock(),
        ):
            result = asyncio.run(
                self.speaker.play_verified_text("需要重试")
            )

        self.assertTrue(result)
        self.assertEqual(2, self.speaker.run_shell.await_count)

    def test_verified_path_returns_false_after_all_attempts(self):
        interrupted = (
            "2026[/usr/sbin/tts_play.sh] - Script interrupted"
        )
        self.speaker.run_shell = mock.AsyncMock(
            return_value=self.command_result(interrupted, "", 0)
        )

        with mock.patch(
            "core.services.speaker.asyncio.sleep",
            new=mock.AsyncMock(),
        ):
            result = asyncio.run(
                self.speaker.play_verified_text("持续失败")
            )

        self.assertFalse(result)
        self.assertEqual(2, self.speaker.run_shell.await_count)

    def test_long_verified_text_is_normalized_and_played_in_order(self):
        completed = "\n".join(
            [
                "2026[/usr/sbin/tts_play.sh] - Starting play flow",
                "2026[/usr/sbin/tts_play.sh] - "
                "Audio playback completed successfully",
            ]
        )
        self.speaker.run_shell = mock.AsyncMock(
            return_value=self.command_result(completed, "", 0)
        )
        text = (
            '第一段介绍"南京"的历史文化和城市发展。\n'
            "第二段介绍产业、交通和教育资源。"
        ) * 8

        result = asyncio.run(
            self.speaker.play_verified_text(text)
        )

        self.assertTrue(result)
        chunks = self.speaker._split_native_tts_text(
            self.speaker._normalize_native_tts_text(text)
        )
        self.assertGreater(len(chunks), 1)
        self.assertEqual(len(chunks), self.speaker.run_shell.await_count)
        commands = [
            call.args[0]
            for call in self.speaker.run_shell.await_args_list
        ]
        self.assertNotIn('"', "".join(commands))
        self.assertNotIn("\n", "".join(commands))

    def test_diagnostics_redact_text_and_are_bounded(self):
        output = (
            "2026 - Text to speech: private words\n"
            + ("x" * 1400)
        )

        diagnostic = self.speaker._diagnostic_output(output)

        self.assertIn("[TTS text omitted]", diagnostic)
        self.assertNotIn("private words", diagnostic)
        self.assertTrue(diagnostic.endswith("…"))
        self.assertLessEqual(
            len(diagnostic),
            self.speaker._TTS_DIAGNOSTIC_LIMIT + 1,
        )


if __name__ == "__main__":
    unittest.main()
