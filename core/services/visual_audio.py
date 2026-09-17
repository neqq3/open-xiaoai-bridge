"""原厂可视化的有限 PCM 回送通道，不录音、不播放、不操作音箱灯光。

输入为现有 Client 提供的16kHz单声道S16_LE，输出为OH2P可视化所需的
48kHz双声道S16_LE。此通道必须由会话层显式创建/撤销，不可作为常驻麦克风API。
"""

import asyncio
import math
import secrets
import struct
import threading
import time
from dataclasses import dataclass, field

from aiohttp import web


MAX_INPUT_BYTES = 8192
PACKET_BYTES = 1920
OUTPUT_BYTES_PER_SECOND = 192000
MAX_FRAME_AGE = 0.2
INPUT_TIMEOUT = 0.5
MAX_LEASE_SECONDS = 60.0


def _trunc_div(value: int, divisor: int) -> int:
    """与设备探针的C有符号整数除法一致，负数向零取整。"""
    return value // divisor if value >= 0 else -((-value) // divisor)


class Oh2pVisualEncoder:
    """连续3倍线性插值及独立可视化增益；不改变原始ASR数据。"""

    def __init__(self, gain=0.25):
        if not math.isfinite(gain) or not 0.05 <= gain <= 1.0:
            raise ValueError("invalid visual gain")
        self.gain = gain
        self.previous: int | None = None
        self.sequence: int | None = None

    def reset(self):
        self.previous = None
        self.sequence = None

    def encode(self, pcm: bytes, sequence: int) -> bytes:
        if not pcm or len(pcm) % 2 or len(pcm) > MAX_INPUT_BYTES:
            self.reset()
            raise ValueError("invalid visual PCM block")
        if self.sequence is None or sequence != self.sequence + 1:
            self.previous = None
        self.sequence = sequence
        out = bytearray(len(pcm) * 6)
        offset = 0
        for (sample,) in struct.iter_unpack("<h", pcm):
            previous = sample if self.previous is None else self.previous
            for k in (1, 2, 3):
                value = previous + _trunc_div((sample - previous) * k, 3)
                value = int(value * self.gain)
                struct.pack_into("<hh", out, offset, value, value)
                offset += 4
            self.previous = sample
        return bytes(out)


@dataclass(eq=False)
class VisualLease:
    """一次有限订阅；令牌不写入日志，旧订阅不能撤销新订阅。"""

    token: str = field(repr=False)
    created_at: float
    deadline: float
    claimed: bool = False
    closed: bool = False
    sequence: int = 0
    heartbeat: bool = False
    control: bool = False
    preserve_on_close: bool = False
    gain: float = 0.25
    latest: tuple[int, float, bytes] | None = field(default=None, repr=False)


class VisualAudioRelay:
    """输入回调只更新最新块，网络慢不能阻塞ASR；所有队列有界。"""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._lease: VisualLease | None = None

    def begin(self, seconds: float, *, heartbeat: bool = False, gain: float = 0.25) -> VisualLease:
        if not math.isfinite(seconds) or not 0 < seconds <= MAX_LEASE_SECONDS:
            raise ValueError("invalid visual lease duration")
        if not math.isfinite(gain) or not 0.05 <= gain <= 1.0:
            raise ValueError("invalid visual gain")
        now = self._clock()
        lease = VisualLease(secrets.token_urlsafe(32), now, now + seconds)
        lease.heartbeat = heartbeat
        lease.gain = gain
        with self._lock:
            if self._lease is not None:
                self._lease.closed = True
                self._lease.latest = None
            self._lease = lease
        return lease

    def close(self, lease: VisualLease):
        with self._lock:
            lease.closed = True
            lease.latest = None
            if self._lease is lease:
                self._lease = None

    def close_all(self):
        with self._lock:
            if self._lease is not None:
                self._lease.closed = True
                self._lease.latest = None
                self._lease = None

    def push(self, pcm: bytes) -> bool:
        """可由原生回调线程调用；不调度协程、不等待锁、不执行网络IO。"""
        if not pcm or len(pcm) % 2 or len(pcm) > MAX_INPUT_BYTES:
            return False
        if not self._lock.acquire(blocking=False):
            return False
        try:
            lease = self._lease
            now = self._clock()
            if lease is None or lease.closed or lease.heartbeat or now >= lease.deadline:
                return False
            lease.sequence += 1
            # 原回调给出不可变bytes时不额外复制；其他buffer类型需转为独立快照。
            lease.latest = (lease.sequence, now, bytes(pcm))
            return True
        finally:
            self._lock.release()

    def _active(self, lease: VisualLease) -> bool:
        with self._lock:
            return self._lease is lease and not lease.closed and self._clock() < lease.deadline

    def _take_latest(self, lease: VisualLease):
        with self._lock:
            if self._lease is not lease or lease.closed:
                return None
            block, lease.latest = lease.latest, None
            return block

    def _claim(self, token: str) -> VisualLease | None:
        if not token.isascii() or len(token) > 128:
            return None
        with self._lock:
            lease = self._lease
            if (lease is None or lease.closed or self._clock() >= lease.deadline
                    or not secrets.compare_digest(token, lease.token)):
                return None
            if lease.claimed:
                raise web.HTTPConflict(text="visual stream already claimed")
            lease.claimed = True
            return lease

    async def handle_stream(self, request: web.Request) -> web.StreamResponse:
        """只允许持有当前短期令牌的一个请求消费；不支持HEAD/重复重连。"""
        if request.method != "GET":
            raise web.HTTPMethodNotAllowed(request.method, ["GET"])
        lease = self._claim(request.match_info.get("token", ""))
        if lease is None:
            raise web.HTTPNotFound()
        response = web.StreamResponse(headers={
            "Content-Type": "application/octet-stream", "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        })
        response.force_close()
        encoder = Oh2pVisualEncoder(lease.gain)
        last_input = self._clock()
        encoded = b""
        encoded_at = 0.0
        offset = 0
        next_due = self._clock()
        aborted = False
        try:
            await asyncio.wait_for(response.prepare(request), timeout=0.2)
            while self._active(lease):
                now = self._clock()
                if lease.heartbeat:
                    # 等待阶段只维持可撤销租约，不订阅或传输麦克风。
                    await asyncio.wait_for(response.write(b"0" if lease.control else b"\0"), timeout=0.1)
                    await asyncio.sleep(0.1)
                    continue
                if now - last_input >= INPUT_TIMEOUT:
                    break
                if now < next_due:
                    await asyncio.sleep(min(next_due - now, 0.01))
                    continue
                if encoded and now - encoded_at > MAX_FRAME_AGE:
                    encoded, offset = b"", 0
                    encoder.reset()
                if offset >= len(encoded):
                    block = self._take_latest(lease)
                    if block is None:
                        await asyncio.sleep(0.005)
                        continue
                    sequence, captured_at, pcm = block
                    if now - captured_at > MAX_FRAME_AGE:
                        encoder.reset()
                        continue
                    last_input = encoded_at = captured_at
                    encoded = encoder.encode(pcm, sequence)
                    offset = 0
                # 写入前再次检查；撤销或期限届满不能启动新的write。
                if not self._active(lease):
                    break
                packet = encoded[offset:offset + PACKET_BYTES]
                offset += len(packet)
                await asyncio.wait_for(response.write(packet), timeout=0.1)
                # 从实际交付时刻安排下一包，慢网络不追赶式突发补写。
                next_due = self._clock() + len(packet) / OUTPUT_BYTES_PER_SECOND
            if lease.control:
                # LX06 无 OH2P 的原厂 FIFO 写端观测。用结束标记区分正常清理和原厂接管。
                await asyncio.wait_for(response.write(b"P" if lease.preserve_on_close else b"C"), timeout=0.1)
            await asyncio.wait_for(response.write_eof(), timeout=0.1)
        except (ConnectionError, asyncio.TimeoutError):
            aborted = True
        except asyncio.CancelledError:
            aborted = True
            raise
        finally:
            self.close(lease)
            if aborted and request.transport is not None:
                request.transport.close()
        return response


visual_audio = VisualAudioRelay()
