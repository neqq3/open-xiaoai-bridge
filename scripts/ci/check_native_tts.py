"""验证实际编译的 PyO3 token 接口；不连接音箱，不生成或播放音频。"""

import tempfile
import unittest
from pathlib import Path

import open_xiaoai_server as native


class NativeTTSBindingTest(unittest.IsolatedAsyncioTestCase):
    async def test_file_binding_reuses_token_and_ignores_stale_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "not-created.wav")
            old_token = native.begin_playback_session()
            current_token = native.begin_playback_session()
            self.assertFalse(native.is_playback_session_active(old_token))
            self.assertTrue(native.is_playback_session_active(current_token))

            # 失效 token 不应读取文件、分配新 token 或启动设备播放。
            await native.play_audio_file(missing, playback_token=old_token)
            native.stop_tts_playback(old_token)
            self.assertTrue(native.is_playback_session_active(current_token))

            # 有效 token 应到达正常文件读取路径，但不能创建替代 token。
            with self.assertRaisesRegex(RuntimeError, "read file failed"):
                await native.play_audio_file(missing, playback_token=current_token)
            self.assertTrue(native.is_playback_session_active(current_token))

            # 不传 token 的原有调用仍会创建自己的会话。
            with self.assertRaisesRegex(RuntimeError, "read file failed"):
                await native.play_audio_file(missing)
            self.assertFalse(native.is_playback_session_active(current_token))


if __name__ == "__main__":
    unittest.main(verbosity=2)
