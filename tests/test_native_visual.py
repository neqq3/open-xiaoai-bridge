"""验证阶段归属、禁用兼容性及原厂 TTS 的保守失败处理，不连接真实设备。"""

import asyncio
import json
import shlex
import types
import unittest
from unittest.mock import AsyncMock

from core.services.native_visual import (
    NativeVisualService, NativeVisualSession, NativeVisualUnavailable,
    parse_reply, ubus_command,
)
from core.services.visual_audio import VisualAudioRelay


def reply(data, code=0):
    return types.SimpleNamespace(stdout=json.dumps(data), exit_code=code)


class NativeVisualTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = NativeVisualService(VisualAudioRelay())
        self.speaker = types.SimpleNamespace(run_shell=AsyncMock())
        self.session = NativeVisualSession(self.service, self.speaker, "http://192.0.2.1:9093")
        self.service.session = self.session

    async def test_disabled_does_not_probe_or_open_port(self):
        self.assertIsNone(await self.service.open(self.speaker, {}))
        self.speaker.run_shell.assert_not_called()
        self.assertIsNone(self.service.runner)

    async def test_unknown_device_does_not_start_server(self):
        self.service.session = None
        self.speaker.run_shell.return_value = types.SimpleNamespace(exit_code=0, stdout="LX06\n1.62.2\n")
        with self.assertRaises(NativeVisualUnavailable):
            await self.service.open(self.speaker, {"enabled": True})
        self.assertIsNone(self.service.runner)

    async def test_preempt_revokes_audio_and_prevents_new_commands(self):
        self.session.lease = self.service.relay.begin(10)
        self.service.preempt()
        self.assertTrue(self.session.lease.closed)
        with self.assertRaises(NativeVisualUnavailable):
            await self.session.phase("thinking")
        self.speaker.run_shell.assert_not_called()

    async def test_clear_waits_for_remote_exit_before_next_phase(self):
        self.session.lease = self.service.relay.begin(10)
        lease = self.session.lease
        released = asyncio.Event()

        async def remote():
            await released.wait()
            return reply({})

        self.session.task = asyncio.create_task(remote())
        clear = asyncio.create_task(self.session.clear())
        await asyncio.sleep(0)
        self.assertTrue(lease.closed)
        self.assertFalse(clear.done())
        released.set()
        await clear

    async def test_remote_failure_prevents_new_phase(self):
        self.session.task = asyncio.create_task(asyncio.sleep(0, result=reply({}, 26)))
        with self.assertRaises(NativeVisualUnavailable):
            await self.session.clear()
        with self.assertRaises(NativeVisualUnavailable):
            await self.session.phase("thinking")

    async def test_cancelled_cleanup_quarantines_unconfirmed_remote_job(self):
        remote_done = asyncio.Event()

        async def remote():
            await remote_done.wait()
            return reply({})

        task = self.session.task = asyncio.create_task(remote())
        cleanup = asyncio.create_task(self.session.clear())
        await asyncio.sleep(0)
        cleanup.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cleanup
        self.assertTrue(self.service.quarantined)
        self.assertFalse(task.cancelled())
        remote_done.set()
        await task

    async def test_old_close_cannot_release_new_session(self):
        new = NativeVisualSession(self.service, self.speaker, "http://192.0.2.1:9093")
        self.service.session = new
        await self.session.close()
        self.assertIs(self.service.session, new)

    async def test_busy_player_never_receives_tts(self):
        self.session._status = AsyncMock(return_value=1)
        with self.assertRaises(NativeVisualUnavailable):
            await self.session.speak("你好")
        self.speaker.run_shell.assert_not_called()

    async def test_uncertain_tts_request_is_not_retried(self):
        self.session._status = AsyncMock(return_value=0)
        self.speaker.run_shell.side_effect = [reply({}), reply({}, 1)]
        with self.assertRaises(NativeVisualUnavailable):
            await self.session.speak("你好")
        self.assertEqual(self.speaker.run_shell.await_count, 2)

    async def test_tts_waits_for_observed_playback_then_idle(self):
        self.session._status = AsyncMock(side_effect=[0, 1, 0, 0, 0, 0, 0])
        self.speaker.run_shell.return_value = reply({"code": 0})
        start = asyncio.get_running_loop().time()
        await self.session.speak("你好")
        self.assertGreaterEqual(asyncio.get_running_loop().time() - start, 0.3)
        self.assertEqual(self.speaker.run_shell.await_count, 2)

    async def test_idle_without_start_is_not_completion(self):
        self.session._status = AsyncMock(return_value=0)
        self.speaker.run_shell.return_value = reply({"code": 0})
        with self.assertRaises(NativeVisualUnavailable):
            await self.session.speak("你好", timeout=0.2)
        self.assertEqual(self.speaker.run_shell.await_count, 2)

    async def test_unknown_player_schema_aborts(self):
        for status in ("0", None, True, 99):
            self.speaker.run_shell.return_value = reply({"code": 0, "info": json.dumps({"status": status})})
            with self.assertRaises(NativeVisualUnavailable):
                await self.session._status()

    async def test_text_is_single_json_shell_argument(self):
        text = "引号'\"; $(touch /tmp/should-not-exist)\n换行"
        args = shlex.split(ubus_command("mibrain", "text_to_speech", {"text": text}))
        self.assertEqual(args[:6], ["ubus", "-t", "2", "call", "mibrain", "text_to_speech"])
        self.assertEqual(json.loads(args[6]), {"text": text})
        self.assertEqual(len(args), 7)

    async def test_malformed_reply_rejected(self):
        for result in (None, reply([], 0), reply({}), reply({"code": False}), reply({"code": 5}), types.SimpleNamespace(exit_code=0, stdout="not-json")):
            with self.assertRaises(NativeVisualUnavailable):
                parse_reply(result)


if __name__ == "__main__":
    unittest.main()
