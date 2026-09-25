"""验证上游 PyO3 文件接口可调用；不连接音箱，不生成或播放音频。"""

import tempfile
import unittest
from pathlib import Path

import open_xiaoai_server as native


class NativeTTSBindingTest(unittest.IsolatedAsyncioTestCase):
    async def test_file_binding_keeps_upstream_arguments_and_reports_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "not-created.wav")
            previous_token = native.begin_playback_session()
            # 缺失文件在设备 I/O 之前失败；不依赖 fork 新增的 token 参数或查询。
            with self.assertRaisesRegex(RuntimeError, "read file failed"):
                await native.play_audio_file(missing, sample_rate=24000)
            # 上游文件路径独立分配一次 token；不把它误称为复用 controller token。
            self.assertEqual(previous_token + 2, native.begin_playback_session())


if __name__ == "__main__":
    unittest.main(verbosity=2)
