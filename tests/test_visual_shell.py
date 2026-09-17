"""用真实固件的两种不同 LED 状态格式回归 shell 阶段检查，防止误判接管。"""

from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipIf(sys.platform == "win32", "需要 POSIX shell；在 Linux 候选容器中运行")
class VisualShellTests(unittest.TestCase):
    def test_stock_led_status_fixtures(self):
        source = (Path(__file__).parents[1] / "core/services/oh2p_visual_phase.sh").read_text()
        # 只执行无副作用的参数和状态格式部分；不加载设备 jshn 或运行任何设备命令。
        prefix = source.split("native_idle()", 1)[0]
        prefix = prefix.replace(". /usr/share/libubox/jshn.sh || exit 10", ":")
        fixtures = {
            "listening": "stored led ids: 41(0) <- ; current id 41",
            "thinking": "stored led ids: ; current id 2",
        }
        for mode, captured_status in fixtures.items():
            with self.subTest(mode=mode):
                result = subprocess.run(
                    ["/bin/sh", "-c", prefix + '\nprintf "%s" "$expected_leds"',
                     "sh", mode, "http://127.0.0.1", "10"],
                    capture_output=True, text=True, check=True,
                )
                self.assertEqual(result.stdout, captured_status)


if __name__ == "__main__":
    unittest.main()
