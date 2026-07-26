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
                "2026-07-26 22:34:09[/usr/sbin/tts_play.sh] - Script interrupted, performing cleanup...",
            ]
        )
        self.speaker.run_shell = mock.AsyncMock(
            return_value=self.command_result(stdout, "", 0)
        )

        result = asyncio.run(self.speaker.play(text="被提前终止", blocking=True))

        self.assertFalse(result)

    def test_legacy_script_keeps_exit_code_compatibility(self):
        self.speaker.run_shell = mock.AsyncMock(
            return_value=self.command_result("", "", 0)
        )

        result = asyncio.run(self.speaker.play(text="旧版脚本", blocking=True))

        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
