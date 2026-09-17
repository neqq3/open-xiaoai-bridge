"""使用真实HTTP客户端验证有限PCM通道；不连接设备或保存语音。"""

import asyncio
import struct
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.services.visual_audio import MAX_INPUT_BYTES, Oh2pVisualEncoder, VisualAudioRelay


def pcm(value, samples=1440):
    return struct.pack("<h", value) * samples


class VisualEncoderTests(unittest.TestCase):
    def test_duration_gain_and_stereo(self):
        out = Oh2pVisualEncoder().encode(pcm(1000, 160), 1)
        self.assertEqual(len(out), 1920)
        self.assertEqual(out, struct.pack("<hh", 250, 250) * 480)

    def test_negative_division_and_chunk_continuity(self):
        encoder = Oh2pVisualEncoder()
        one = encoder.encode(pcm(-1001, 1), 1)
        two = encoder.encode(pcm(1002, 1), 2)
        self.assertEqual(one + two, Oh2pVisualEncoder().encode(pcm(-1001, 1) + pcm(1002, 1), 1))
        self.assertEqual(one, struct.pack("<hh", -250, -250) * 3)

    def test_gap_drops_interpolation_history(self):
        encoder = Oh2pVisualEncoder()
        encoder.encode(pcm(32767, 1), 1)
        self.assertEqual(encoder.encode(pcm(-1000, 1), 3), struct.pack("<hh", -250, -250) * 3)

    def test_invalid_pcm_rejected(self):
        for data in (b"", b"x", bytes(MAX_INPUT_BYTES + 2)):
            with self.assertRaises(ValueError):
                Oh2pVisualEncoder().encode(data, 1)

    def test_gain_calibration_is_independent_and_bounded(self):
        original = pcm(1000, 160)
        self.assertEqual(Oh2pVisualEncoder(0.30).encode(original, 1), struct.pack("<hh", 300, 300) * 480)
        self.assertEqual(original, pcm(1000, 160))
        for gain in (0, -1, 1.1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                Oh2pVisualEncoder(gain)
            with self.assertRaises(ValueError):
                VisualAudioRelay().begin(1, gain=gain)


class VisualLeaseTests(unittest.TestCase):
    def test_no_active_lease_keeps_no_audio(self):
        relay = VisualAudioRelay()
        self.assertFalse(relay.push(pcm(1)))

    def test_latest_only_and_old_cleanup_isolated(self):
        relay = VisualAudioRelay()
        old = relay.begin(5)
        new = relay.begin(5)
        for i in range(1000):
            relay.push(pcm(i, 1))
        relay.close(old)
        self.assertTrue(relay._active(new))
        sequence, _, data = relay._take_latest(new)
        self.assertEqual((sequence, data), (1000, pcm(999, 1)))
        self.assertIsNone(relay._take_latest(new))

    def test_expiry_invalid_duration_and_input_bound(self):
        now = [0.0]
        relay = VisualAudioRelay(clock=lambda: now[0])
        lease = relay.begin(1)
        for data in (b"", b"x", bytes(MAX_INPUT_BYTES + 2)):
            self.assertFalse(relay.push(data))
        now[0] = 1
        self.assertFalse(relay.push(pcm(1)))
        self.assertIsNone(relay._claim(lease.token))
        for seconds in (0, -1, 61, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                relay.begin(seconds)

    def test_capture_does_not_wait_on_consumer_lock(self):
        relay = VisualAudioRelay()
        relay.begin(1)
        with relay._lock:
            self.assertFalse(relay.push(pcm(1)))


class VisualHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.relay = VisualAudioRelay()
        app = web.Application()
        app.router.add_get("/visual/{token}", self.relay.handle_stream, allow_head=False)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        self.relay.close_all()
        await self.client.close()

    async def test_real_http_packets_and_no_duplicate_consumer(self):
        lease = self.relay.begin(2)
        self.relay.push(pcm(1000))
        response = await self.client.get("/visual/" + lease.token)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(await response.content.readexactly(1920), struct.pack("<hh", 250, 250) * 480)
        duplicate = await self.client.get("/visual/" + lease.token)
        self.assertEqual(duplicate.status, 409)
        self.relay.close(lease)
        await asyncio.wait_for(response.read(), 0.5)
        self.assertFalse(self.relay.push(pcm(1)))

    async def test_token_method_and_expired_session(self):
        lease = self.relay.begin(2)
        wrong = await self.client.get("/visual/not-a-token")
        self.assertEqual(wrong.status, 404)
        unicode_token = await self.client.get("/visual/错误令牌")
        self.assertEqual(unicode_token.status, 404)
        head = await self.client.head("/visual/" + lease.token)
        self.assertEqual(head.status, 405)
        self.assertFalse(lease.claimed)
        self.relay.close(lease)
        stale = await self.client.get("/visual/" + lease.token)
        self.assertEqual(stale.status, 404)

    async def test_input_stall_closes_without_waiting_full_lease(self):
        lease = self.relay.begin(10)
        response = await self.client.get("/visual/" + lease.token)
        self.assertEqual(await asyncio.wait_for(response.read(), 1.0), b"")
        self.assertTrue(lease.closed)

    async def test_thinking_lease_never_keeps_or_transmits_microphone(self):
        lease = self.relay.begin(2, heartbeat=True)
        self.assertFalse(self.relay.push(pcm(1000)))
        response = await self.client.get("/visual/" + lease.token)
        self.assertEqual(await response.content.readexactly(2), b"\0\0")
        self.assertIsNone(lease.latest)
        self.relay.close(lease)
        await asyncio.wait_for(response.read(), 0.5)

    async def test_new_lease_closes_old_http_without_closing_new(self):
        old = self.relay.begin(5)
        self.relay.push(pcm(1000))
        response = await self.client.get("/visual/" + old.token)
        await response.content.readexactly(1920)
        new = self.relay.begin(5)
        await asyncio.wait_for(response.read(), 0.5)
        self.assertTrue(self.relay._active(new))

    async def test_frame_is_not_burst_delivered(self):
        lease = self.relay.begin(2)
        self.relay.push(pcm(1000))
        response = await self.client.get("/visual/" + lease.token)
        loop = asyncio.get_running_loop()
        started = loop.time()
        data = await response.content.readexactly(1440 * 12)
        self.assertGreaterEqual(loop.time() - started, 0.065)
        self.assertEqual(len(data), 17280)
        self.relay.close(lease)


if __name__ == "__main__":
    unittest.main()
