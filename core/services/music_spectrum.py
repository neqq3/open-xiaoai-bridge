"""主机端频谱与有界 HTTP 租约；音频只留在内存，不保存录音。"""

import asyncio
import colorsys
import secrets
import time

import numpy as np
from aiohttp import web


class SpectrumRenderer:
    """OH2P 前排 12 灯的对称六频段布局，中心为低频，两端为高频。"""

    def __init__(self, brightness=50):
        if type(brightness) is not int or not 1 <= brightness <= 100:
            raise ValueError("music.brightness must be an integer between 1 and 100")
        self.brightness = brightness / 100
        self.peaks = np.full(6, 2000.0)
        self.levels = np.zeros(6)
        self.window = np.hanning(1024)
        # 采用设备请求的名义采样率；尚不作为校准过的频率测量工具。
        frequencies = np.fft.rfftfreq(1024, 1 / 48000)
        bounds = (40, 160, 400, 1000, 2500, 6000, 16000)
        self.bands = [(frequencies >= a) & (frequencies < b)
                      for a, b in zip(bounds, bounds[1:])]

    def render(self, pcm):
        stereo = np.frombuffer(pcm, dtype='<i2').reshape(1024, 2)
        mono = stereo.astype(np.float64).mean(axis=1)
        rms = float(np.sqrt(np.mean(mono * mono)))
        if rms < 20:
            self.levels *= .65
        else:
            magnitude = np.abs(np.fft.rfft(mono * self.window))
            energy = np.array([np.max(magnitude[band]) for band in self.bands])
            self.peaks = np.maximum(2000, np.maximum(energy, self.peaks * .98))
            self.levels = np.maximum(np.clip(energy / self.peaks, 0, 1), self.levels * .78)
        pixels = []
        for physical in range(12):
            band = int(abs(physical - 5.5))
            value = float(self.levels[band]) * self.brightness
            if rms < 20 and value < .01:
                value = 0
            r, g, b = (round(c * 255) for c in colorsys.hsv_to_rgb(band * .15, 1, value))
            pixels.append(r | (g << 8) | (b << 16))
        return pixels


class SpectrumLease:
    """每次最多 120 秒，上传与下载各允许一次，输出仅保留最新帧。"""

    def __init__(self, brightness=50, seconds=120):
        self.renderer = SpectrumRenderer(brightness)
        self.token = secrets.token_hex(16)
        self.deadline = time.monotonic() + seconds
        self.seconds = seconds
        self.started = time.monotonic()
        self.last_audio = self.started
        self.closed = asyncio.Event()
        self.reason = None
        self.preserve = False
        self.claims = set()
        self.latest = None
        self.generation = 0
        self.bytes = 0

    def close(self, reason, preserve=False):
        self.preserve |= preserve
        if not self.closed.is_set():
            self.reason = reason
            self.closed.set()

    def authorize(self, request, channel):
        if request.match_info.get('token') != self.token:
            raise web.HTTPNotFound()
        if self.closed.is_set() or time.monotonic() >= self.deadline:
            raise web.HTTPGone()
        if channel in self.claims:
            raise web.HTTPConflict()
        self.claims.add(channel)

    async def audio(self, request):
        self.authorize(request, 'audio')
        pending = bytearray()
        try:
            async with asyncio.timeout(max(.01, self.deadline - time.monotonic())):
                while not self.closed.is_set():
                    async with asyncio.timeout(2):
                        chunk = await request.content.read(4096)
                    if not chunk:
                        break
                    self.bytes += len(chunk)
                    if self.bytes > self.seconds * 192000:
                        self.close('byte_limit')
                        break
                    pending.extend(chunk)
                    if len(pending) >= 4096:
                        self.latest = self.renderer.render(bytes(pending[:4096]))
                        del pending[:4096]
                        self.generation += 1
                        self.last_audio = time.monotonic()
                    # 即使发送端已填满 socket 缓冲，也让路给对话和输出任务。
                    await asyncio.sleep(0)
        except (TimeoutError, ConnectionError):
            self.close('audio_disconnected')
        finally:
            self.close('audio_ended')
        return web.Response(status=204)

    async def frames(self, request):
        self.authorize(request, 'frames')
        response = web.StreamResponse(headers={'Content-Type': 'text/plain', 'Cache-Control': 'no-store'})
        await response.prepare(request)
        sent = -1
        try:
            while not self.closed.is_set():
                now = time.monotonic()
                if now >= self.deadline:
                    self.close('expired')
                    break
                if now - self.started > 3 and now - self.last_audio > 1:
                    self.close('audio_stale')
                    break
                if self.latest is not None and self.generation != sent:
                    sent = self.generation
                    line = str(sent) + ' ' + ' '.join(f'0x{p:06X}' for p in self.latest) + '\n'
                    async with asyncio.timeout(.4):
                        await response.write(line.encode())
                try:
                    await asyncio.wait_for(self.closed.wait(), timeout=.05)
                except TimeoutError:
                    pass
            # 原厂接管时不熄灯，普通停止则由设备检查空闲后清理自身画面。
            async with asyncio.timeout(.4):
                await response.write(b'YIELD\n' if self.preserve else b'STOP\n')
        except (TimeoutError, ConnectionError):
            self.close('output_disconnected')
        return response
