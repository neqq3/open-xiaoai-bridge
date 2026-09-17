"""设备选择不能被手动 profile 绕过；未验证的 LX06 不自动启用。"""
import asyncio
import types
import unittest
from unittest.mock import AsyncMock

from core.services.native_visual import NativeVisualService, NativeVisualSession, NativeVisualUnavailable
from core.services.native_visual_profiles import OH2P, LX06, select_profile
from core.services.visual_audio import VisualAudioRelay


def identity(model, version):
    return f'{model}\nROM Type:release / Ver:{version}\n'


class ProfileTests(unittest.TestCase):
    def test_auto_keeps_verified_oh2p(self):
        self.assertEqual(select_profile(identity('OH2P', '1.62.2')), OH2P)

    def test_lx06_requires_explicit_profile_and_exact_version(self):
        with self.assertRaises(ValueError):
            select_profile(identity('LX06', '1.94.13'))
        self.assertEqual(select_profile(identity('LX06', '1.94.13'), LX06.name), LX06)
        for model, version in [('OH2P', '1.94.13'), ('LX06', '1.88.221'), ('LX06', '1.94.130')]:
            with self.subTest(model=model, version=version), self.assertRaises(ValueError):
                select_profile(identity(model, version), LX06.name)

    def test_unknown_profile_or_identity_is_rejected(self):
        for text, profile in [('', 'auto'), ('LX06\n1.94.13\n', LX06.name),
                              (identity('OH2P', '1.62.2'), 'force'),
                              (identity('OH2P', '1.62.20'), 'auto')]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                select_profile(text, profile)


class ProfileSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_lx06_never_reaches_prerequisites_or_server(self):
        service = NativeVisualService(VisualAudioRelay())
        speaker = types.SimpleNamespace(run_shell=AsyncMock(return_value=types.SimpleNamespace(
            exit_code=0, stdout=identity('LX06', '1.94.13'))))
        with self.assertRaises(NativeVisualUnavailable):
            await service.open(speaker, {'enabled': True})
        self.assertEqual(speaker.run_shell.await_count, 1)
        self.assertIsNone(service.runner)

    async def test_lx06_listening_uses_control_without_microphone(self):
        service = NativeVisualService(VisualAudioRelay())
        commands = []
        async def remote(command, timeout):
            commands.append(command)
            lease = service.relay._lease
            self.assertTrue(lease.heartbeat)
            self.assertTrue(lease.control)
            self.assertFalse(service.relay.push(b'\xff\x7f' * 320))
            service.relay._claim(lease.token)
            while not lease.closed:
                await asyncio.sleep(0.001)
            return types.SimpleNamespace(exit_code=0, stdout='visual_result=0 native_takeover=0')
        speaker = types.SimpleNamespace(run_shell=remote)
        session = NativeVisualSession(service, speaker, 'http://127.0.0.1:9093', profile=LX06)
        await session.phase('listening', 1)
        await session.clear()
        self.assertIn('LX06 1.94.13', commands[0])
        self.assertNotIn('/tmp/mic_audio.fifo', commands[0])

    async def test_preempt_preserves_lx06_state(self):
        service = NativeVisualService(VisualAudioRelay())
        session = service.session = NativeVisualSession(service, None, '', profile=LX06)
        lease = session.lease = service.relay.begin(1, heartbeat=True)
        lease.control = True
        service.preempt()
        self.assertTrue(lease.closed)
        self.assertTrue(lease.preserve_on_close)
        self.assertTrue(session.preempted.is_set())

    async def test_lx06_speech_guard_never_uses_oh2p_fifo(self):
        service = NativeVisualService(VisualAudioRelay())
        speaker = types.SimpleNamespace(run_shell=AsyncMock(return_value=types.SimpleNamespace(exit_code=20, stdout='')))
        session = NativeVisualSession(service, speaker, '', profile=LX06)
        session._status = AsyncMock(return_value=0)
        with self.assertRaises(NativeVisualUnavailable):
            await session.speak('测试')
        command = speaker.run_shell.call_args.args[0]
        self.assertIn('LX06 1.94.13', command)
        self.assertNotIn('/tmp/mic_audio.fifo', command)
        self.assertEqual(speaker.run_shell.await_count, 1)
