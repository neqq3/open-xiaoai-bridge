"""主机端频谱与有界 HTTP 租约；音频只留在内存，不保存录音。"""

import asyncio
import secrets
import time

from core.services.spectrum_effects import SpectrumRenderer
from aiohttp import web


class SpectrumLease:
    """每次最多 120 秒，上传与下载各允许一次，输出仅保留最新帧。"""

    def __init__(self, brightness=50, seconds=120, renderer=None):
        self.renderer = renderer if renderer is not None else SpectrumRenderer(brightness)
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
