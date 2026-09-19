"""与独立编译的原 C 参考程序产生的逐帧输出摘要对照。"""
import hashlib
import unittest

import numpy as np

from core.services.music_spectrum import SpectrumLease
from core.services.spectrum_effects import SpectrumRenderer, map_oh2p
from spectrum_vectors import blocks, tone


# 基准：xiaomi-sound-spectrum 33d5d233 的纯算法段，GCC 10 / QEMU 执行。
# 包括定点 FFT、立体声、白色高亮、静音状态机、OH2P 映射、亮度与差量阈值。
GOLDEN = (
    ('mixed', '1', 100, 'e9f5b19c9c2c24d522e4131fdbdb6f8f6313b47e74e353367a4fee476714e7df'),
    ('mixed', '2', 100, '1b4a08b2d743f8786dd4cad937be1174eff0c0a180811ca77dc6085603b87720'),
    ('mixed', '1', 37, '8017e92abc50d23e62932348a43ec259e73b7a47974704f3e34edba587db5aed'),
    ('cycle', 'auto', 100, 'd339cbe85659bb58f8e02cac049c36e4d271c1336c76a4c96f81bd2fa63cd191'),
)


class EffectsTests(unittest.TestCase):
    def test_reference_output_for_both_modes_and_auto_cycle(self):
        for case, mode, brightness, expected in GOLDEN:
            with self.subTest(case=case, mode=mode, brightness=brightness):
                renderer = SpectrumRenderer(brightness, mode)
                digest = hashlib.sha256()
                for block in blocks(case):
                    digest.update(np.array(renderer.render(block), dtype='<u4').tobytes())
                self.assertEqual(digest.hexdigest(), expected)

    def test_left_only_audio_is_not_mirrored_to_right_wing(self):
        stereo = np.frombuffer(tone(3), dtype='<i2').reshape(-1, 2).copy()
        stereo[:, 1] = 0
        renderer = SpectrumRenderer(100, 1)
        for _ in range(5):
            renderer.render(stereo.tobytes())
        self.assertTrue(any(renderer.logical[1:9]))
        self.assertEqual(renderer.logical[10:18], [0] * 8)

    def test_center_and_edges_use_the_same_oh2p_area_mapping(self):
        colors = [0] * 18
        colors[0] = 0xFF
        pixels = map_oh2p(colors, 100)
        self.assertEqual([i for i, color in enumerate(pixels) if color], [5, 6])
        colors[0], colors[9] = 0, 0xFF0000
        pixels = map_oh2p(colors, 100)
        self.assertEqual([i for i, color in enumerate(pixels) if color], [0, 11])

    def test_config_change_is_validated_before_modifying_renderer(self):
        renderer = SpectrumRenderer(100, 1)
        for mode in (True, 0, 3, 'random', None):
            with self.assertRaises(ValueError):
                renderer.configure(50, mode)
            self.assertEqual((renderer.mode, renderer.brightness), ('1', 100))
        renderer.configure(70, 2)
        self.assertEqual((renderer.current_mode, renderer.brightness), (2, 70))

    def test_transport_renewal_keeps_auto_cycle_and_envelopes(self):
        renderer = SpectrumRenderer(50, 'auto')
        renderer.mode_frames = 2799
        first = SpectrumLease(renderer=renderer)
        first.renderer.render(tone(1))
        first.close('expired')
        second = SpectrumLease(renderer=renderer)
        self.assertIs(first.renderer, second.renderer)
        self.assertEqual(second.renderer.current_mode, 2)
        self.assertTrue(second.renderer.levels.any())


if __name__ == '__main__':
    unittest.main()
