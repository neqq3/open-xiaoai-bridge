"""xiaomi-sound-spectrum 两种效果的主机端移植（eastwoodnet，MIT）。

算法来源：neqq3/xiaomi-sound-spectrum@33d5d233e5edc711d88ab9e1240c3269ca52a6f3
led_music.c 与 oh2p_output.h。保留 Q14 FFT、整数舍入、双声道 AGC 和 OH2P 面积映射。
设备进程、原厂状态和网络传输仍由 Bridge 独立管理。
"""

import math
import numpy as np


BANDS = ((1, 2), (3, 4), (5, 9), (10, 20), (21, 46), (47, 95), (96, 180), (181, 340))
PALETTE = (0x0000FF, 0x0045FF, 0x00B5FF, 0x00FF30, 0xFFFF00, 0xFF7500, 0xFF0050, 0xFF40FF)
BASE, WHITE = 0x100002, 0xFFFFFF
WINDOW = np.rint(np.hanning(1024) * 16384).astype(np.int64)
COS = np.rint(np.cos(2 * np.pi * np.arange(512) / 1024) * 16384).astype(np.int64)
SIN = np.rint(np.sin(2 * np.pi * np.arange(512) / 1024) * 16384).astype(np.int64)
REVERSE = np.array([int(f'{i:010b}'[::-1], 2) for i in range(1024)])


def fixed_fft(stereo):
    """并行处理两声道；十层蝶形运算的每一步都与原 C 的整数移位一致。"""
    real = ((stereo.astype(np.int64).T * WINDOW) >> 14)[:, REVERSE].copy()
    imag = np.zeros_like(real)
    for power in range(1, 11):
        step = 1 << power
        half = step // 2
        r, i = real.reshape(2, -1, step), imag.reshape(2, -1, step)
        wr, wi = COS[::1024 // step], SIN[::1024 // step]
        tr = (r[:, :, half:] * wr + i[:, :, half:] * wi) >> 14
        ti = (i[:, :, half:] * wr - r[:, :, half:] * wi) >> 14
        pr, pi = r[:, :, :half].copy(), i[:, :, :half].copy()
        r[:, :, :half], r[:, :, half:] = pr + tr, pr - tr
        i[:, :, :half], i[:, :, half:] = pi + ti, pi - ti
    r, i = np.abs(real[:, :512]), np.abs(imag[:, :512])
    return np.maximum(r, i) + (np.minimum(r, i) >> 1)


def scale(color, level):
    if level <= 0:
        return 0
    if level >= 100:
        return color
    level = (level // 2) * 2
    return sum((((color >> shift) & 255) * level // 100) << shift for shift in (0, 8, 16))


def blend(first, second, amount):
    amount = min(100, max(0, amount))
    return sum(((((first >> shift) & 255) * (100 - amount)
                 + ((second >> shift) & 255) * amount) // 100) << shift for shift in (0, 8, 16))


def map_oh2p(colors, brightness):
    """将虚拟环在高频端切开；0 号低音映射到前排中间，物理编号右→左。"""
    result = []
    for physical in range(12):
        start = 114 + physical * 18
        end = start + 18
        channels = [0, 0, 0]
        for virtual in range(start // 12, (end - 1) // 12 + 1):
            weight = min(end, (virtual + 1) * 12) - max(start, virtual * 12)
            for channel, shift in enumerate((0, 8, 16)):
                channels[channel] += ((colors[virtual % 18] >> shift) & 255) * weight
        result.append(sum((value * brightness // 1800) << shift
                          for value, shift in zip(channels, (0, 8, 16))))
    return result


class SpectrumRenderer:
    def __init__(self, brightness=50, mode='auto'):
        self.mode = None
        self.configure(brightness, mode)
        self.high = np.full((2, 8), 8000, dtype=np.int64)
        self.low = np.full((2, 8), 500, dtype=np.int64)
        self.levels = np.zeros((2, 8), dtype=np.int64)
        self.spread, self.peak, self.hold = [0, 0], [0, 0], [0, 0]
        self.state = self.last_state = 'stopped'
        self.silent_frames = 0
        self.logical = [0] * 18
        self.output = [0] * 12

    def configure(self, brightness, mode):
        if type(brightness) is not int or not 1 <= brightness <= 100:
            raise ValueError('music.brightness must be an integer between 1 and 100')
        if type(mode) is int and mode in (1, 2):
            mode = str(mode)
        if not isinstance(mode, str) or mode not in ('1', '2', 'auto'):
            raise ValueError('music.mode must be 1, 2 or auto')
        if mode != self.mode:
            self.current_mode = 1 if mode == 'auto' else int(mode)
            self.mode_frames = 0
        self.mode, self.brightness = mode, brightness

    def render(self, pcm):
        stereo = np.frombuffer(pcm, dtype='<i2').reshape(1024, 2)
        rms = math.isqrt(int(np.sum(stereo.astype(np.int64) ** 2)) // 2048)
        if rms < 16:
            self.silent_frames += 1
            if self.silent_frames > 90:
                self.state = 'stopped' if self.silent_frames > 1400 else 'paused'
        else:
            self.silent_frames, self.state = 0, 'playing'
        if self.state == 'stopped':
            if self.last_state != 'stopped':
                self.logical = [0] * 18
                self.levels.fill(0)
                self.spread = [0, 0]
            self.last_state = 'stopped'
            return self._output(force=True)
        if self.state == 'paused':
            self.logical = [BASE] * 18
            self.levels = self.levels * 70 // 100
            self.spread = [max(0, value - 1) for value in self.spread]
            self.last_state = 'paused'
            return self._output(force=True)
        self.last_state = 'playing'
        magnitudes = fixed_fft(stereo)
        energy = np.stack([magnitudes[:, a:b + 1].max(axis=1) for a, b in BANDS], axis=1)
        self.high = np.where(energy > self.high, energy, (self.high * 199 + energy) // 200)
        self.low = np.where(energy < self.low, energy, (self.low * 199 + energy) // 200)
        raw = np.clip((energy - self.low) * 100 // np.maximum(3000, self.high - self.low), 0, 100)
        self.levels = np.where(raw >= self.levels, raw, self.levels * 88 // 100)
        if self.mode == 'auto':
            self.mode_frames += 1
            if self.mode_frames >= 2800:
                self.mode_frames = 0
                self.current_mode = 3 - self.current_mode
        if self.current_mode == 1:
            self._equalizer()
        else:
            self._bass_wings()
        return self._output()

    def _equalizer(self):
        for channel in range(2):
            for band, palette in enumerate(PALETTE):
                level = int(self.levels[channel, band])
                color = blend(palette, WHITE, (level - 85) * 6) if level > 85 else palette
                self.logical[1 + band if channel == 0 else 17 - band] = scale(color, level)
        bass = int(self.levels[:, 0].sum()) // 2
        self.logical[0] = scale(blend(0x0020FF, WHITE, (bass - 50) * 2) if bass > 50 else 0x0000FF, bass)
        treble = int(self.levels[:, 6:8].sum()) // 4
        self.logical[9] = scale(WHITE if treble > 60 else 0xFF40FF, treble)

    def _bass_wings(self):
        for channel in range(2):
            bass = int(self.levels[channel, 0] * 2 + self.levels[channel, 1]) // 3
            target = bass * 8 // 100
            if bass > 5 and target == 0:
                target = 1
            if target > self.spread[channel]:
                self.spread[channel] = target
            elif target < self.spread[channel]:
                self.spread[channel] -= 1
            if self.spread[channel] > self.peak[channel]:
                self.peak[channel], self.hold[channel] = self.spread[channel], 10
            elif self.hold[channel] > 0:
                self.hold[channel] -= 1
            elif self.peak[channel] > 0:
                self.peak[channel] -= 1
        bass = int(self.levels[:, :2].sum())
        treble = int(self.levels[:, 5:].sum())
        total = int(self.levels.sum())
        if total:
            br, tr = bass * 100 // total, treble * 100 // total
            if br > 50:
                color = blend(0x0000FF, 0x0080FF, (100 - br) * 2)
            elif tr > 35:
                color = blend(0xFFFF00, 0xFF0080, tr * 2)
            else:
                color = blend(0x00FF40, 0x00B5FF, 50)
        else:
            color = 0x0055FF
        for channel in range(2):
            for step in range(1, 9):
                pixel = step if channel == 0 else 18 - step
                if step <= self.spread[channel]:
                    self.logical[pixel] = color
                elif step == self.peak[channel] and self.peak[channel] > self.spread[channel]:
                    self.logical[pixel] = WHITE
                else:
                    self.logical[pixel] = BASE
        self.logical[0] = color
        self.logical[9] = WHITE if min(self.spread) >= 8 else BASE

    def _output(self, force=False):
        mapped = map_oh2p(self.logical, self.brightness)
        for index, color in enumerate(mapped):
            diff = max(abs(((color >> shift) & 255) - ((self.output[index] >> shift) & 255))
                       for shift in (0, 8, 16))
            if force or diff >= 2:
                self.output[index] = color
        return self.output.copy()
