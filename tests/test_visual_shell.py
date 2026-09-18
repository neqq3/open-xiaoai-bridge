"""用真实固件的两种不同 LED 状态格式回归 shell 阶段检查，防止误判接管。"""

from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipIf(sys.platform == "win32", "需要 POSIX shell；在 Linux 候选容器中运行")
class VisualShellTests(unittest.TestCase):
    def test_snapshot_accepts_paused_music_but_rejects_playing_and_unknown(self):
        source = (Path(__file__).parents[1] / "core/services/oh2p_visual_phase.sh").read_text()
        snapshot = "snapshot()" + source.split("snapshot()", 1)[1].split("cleanup()", 1)[0]
        # 执行真实 shell 检查，仅模拟 ubus/jshn 读数，不访问任何设备。
        fixtures = '''
ubus() { return 0; }
json_load() { return 0; }
json_get_var() {
    case "$1" in
        code) code=0;; info) info=unused;; media) media=$fixture_status;;
        leds) leds='stored led ids: ; current id 0';;
    esac
}
'''
        for status, expected in (("0", 0), ("2", 0), ("3", 0), ("1", 1), ("4", 1), ("99", 1), ("", 1)):
            with self.subTest(status=status):
                result = subprocess.run(
                    ["/bin/sh", "-c", 'fixture_status=$1\n' + fixtures + snapshot + '\nsnapshot', "sh", status],
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)

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
