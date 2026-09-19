"""频谱网络边界、嵌套对话优先级和设备端状态门控回归。"""

import asyncio
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

import numpy as np
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from core.services.music_spectrum import SpectrumLease, SpectrumRenderer
from core.services.native_visual import NativeVisualService, NativeVisualUnavailable


class RenderTests(unittest.TestCase):
    def test_silence_and_decay_to_black(self):
        renderer = SpectrumRenderer()
        self.assertEqual(renderer.render(bytes(4096)), [0] * 12)
        renderer.levels[:] = 1
        for _ in range(30):
            colors = renderer.render(bytes(4096))
        self.assertEqual(colors, [0] * 12)

    def test_bass_is_centered_symmetric_and_brightness_bounded(self):
        wave = (np.sin(np.arange(1024) * 2 * np.pi * 93.75 / 48000) * 4000).astype('<i2')
        colors = SpectrumRenderer(50).render(np.column_stack((wave, wave)).tobytes())
        self.assertEqual(colors, colors[::-1])
        self.assertGreater(colors[5] & 255, colors[0] & 255)
        for color in colors:
            self.assertTrue(all(((color >> shift) & 255) <= 128 for shift in (0, 8, 16)))

    def test_invalid_brightness_rejected(self):
        for value in (0, 101, True, '50', .5):
            with self.assertRaises(ValueError):
                SpectrumRenderer(value)


class StreamTests(AioHTTPTestCase):
    async def get_application(self):
        self.lease = SpectrumLease(seconds=5)
        app = web.Application()
        app.router.add_put('/audio/{token}', self.lease.audio)
        app.router.add_get('/frames/{token}', self.lease.frames)
        return app

    async def test_tokens_duplicates_and_revocation(self):
        response = await self.client.get('/frames/wrong')
        self.assertEqual(response.status, 404)
        response = await self.client.get('/frames/' + self.lease.token)
        duplicate = await self.client.get('/frames/' + self.lease.token)
        self.assertEqual(duplicate.status, 409)
        self.lease.close('speech')
        self.assertEqual(await response.content.readline(), b'STOP\n')
        revoked = await self.client.put('/audio/' + self.lease.token, data=b'')
        self.assertEqual(revoked.status, 410)

    async def test_native_takeover_preserves_original_lights(self):
        response = await self.client.get('/frames/' + self.lease.token)
        self.lease.close('native_takeover', preserve=True)
        self.lease.close('cleanup')
        self.assertEqual(await response.content.readline(), b'YIELD\n')

    async def test_fragmented_pcm_delivered_without_buffering_history(self):
        sent = asyncio.Event()
        finish = asyncio.Event()

        async def fragments():
            for size in (3, 4000, 93):
                yield bytes(size)
            sent.set()
            await finish.wait()

        upload = asyncio.create_task(self.client.put('/audio/' + self.lease.token, data=fragments()))
        await sent.wait()
        response = await self.client.get('/frames/' + self.lease.token)
        line = await asyncio.wait_for(response.content.readline(), 2)
        self.assertEqual(line.decode().split(), ['1'] + ['0x000000'] * 12)
        duplicate = await self.client.put('/audio/' + self.lease.token, data=b'')
        self.assertEqual(duplicate.status, 409)
        finish.set()
        await upload
        self.assertEqual(await response.content.readline(), b'STOP\n')
        self.assertEqual(self.lease.reason, 'audio_ended')

    async def test_missing_audio_expires_without_hanging(self):
        self.lease.deadline = asyncio.get_running_loop().time() + .1
        response = await self.client.get('/frames/' + self.lease.token)
        self.assertEqual(await asyncio.wait_for(response.content.readline(), 1), b'STOP\n')
        self.assertEqual(self.lease.reason, 'expired')

    async def test_excess_upload_is_bounded(self):
        self.lease.seconds = .01
        response = await self.client.put('/audio/' + self.lease.token, data=bytes(4096))
        self.assertEqual(response.status, 204)
        self.assertEqual(self.lease.reason, 'byte_limit')
        self.assertIsNone(self.lease.latest)


class OwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.visual = NativeVisualService()
        self.music = self.visual.music

    async def test_disabled_does_not_start_worker_probe_or_port(self):
        with patch('core.services.music_visual.get_speaker') as speaker:
            await self.music.start({})
            speaker.assert_not_called()
        self.assertIsNone(self.music.task)
        self.assertIsNone(self.visual.runner)

    async def test_mode_reload_from_watcher_thread_preserves_live_transport(self):
        self.music.loop = asyncio.get_running_loop()
        self.music.renderer = SpectrumRenderer(50, 1)
        self.music.lease = SpectrumLease(renderer=self.music.renderer)
        token = self.music.lease.token
        await asyncio.to_thread(self.music.config_changed, {}, {
            'native_visual': {'music': {'brightness': 100, 'mode': 2}}})
        await asyncio.sleep(0)
        self.assertEqual(self.music.renderer.current_mode, 2)
        self.assertEqual(self.music.renderer.brightness, 100)
        self.assertEqual(self.music.lease.token, token)
        self.assertFalse(self.music.lease.closed.is_set())

    async def test_speech_waits_for_remote_cleanup_and_holds_until_exit(self):
        self.music.lease = SpectrumLease()
        released = asyncio.Event()
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def remote():
            await released.wait()
            return types.SimpleNamespace(stdout='music_result=0', exit_code=0)

        self.music.job = asyncio.create_task(remote())

        async def speech():
            async with self.music.speech():
                entered.set()
                await finish.wait()

        task = asyncio.create_task(speech())
        await asyncio.sleep(0)
        self.assertTrue(self.music.lease.closed.is_set())
        self.assertFalse(entered.is_set())
        self.assertTrue(self.music._busy())
        released.set()
        await entered.wait()
        self.assertTrue(self.music._busy())
        finish.set()
        await task
        self.assertFalse(self.music.holders)

    async def test_nested_and_cancelled_holders_release_only_themselves(self):
        async with self.music.speech():
            async def inner():
                async with self.music.speech():
                    await asyncio.Future()
            task = asyncio.create_task(inner())
            await asyncio.sleep(0)
            self.assertEqual(len(self.music.holders), 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(len(self.music.holders), 1)
        self.assertFalse(self.music.holders)

    async def test_unconfirmed_remote_exit_disables_lights_but_allows_voice(self):
        self.music.job = asyncio.create_task(asyncio.sleep(0, result=None))
        async with self.music.speech():
            self.assertTrue(self.visual.quarantined)
        speaker = types.SimpleNamespace(run_shell=AsyncMock())
        with self.assertRaises(NativeVisualUnavailable):
            await self.visual.open(speaker, {'enabled': True})
        speaker.run_shell.assert_not_called()

    async def test_unknown_device_never_starts_http_or_capture(self):
        speaker = types.SimpleNamespace(run_shell=AsyncMock(return_value=types.SimpleNamespace(
            exit_code=0, stdout='LX06\nVer:1.94.13\n')))
        with patch('core.services.music_visual.get_speaker', return_value=speaker):
            await self.music.start({'music': {'enabled': True}, 'public_url': 'http://192.0.2.1:9093'})
            await asyncio.sleep(.05)
            await self.music.shutdown()
        speaker.run_shell.assert_awaited_once()
        self.assertIsNone(self.visual.runner)

    async def test_one_server_for_speech_and_music(self):
        settings = {'public_url': 'http://192.0.2.1:9093', 'bind_host': '127.0.0.1', 'port': 0}
        await asyncio.gather(self.visual.ensure_server(settings), self.visual.ensure_server(settings))
        self.assertEqual(len(self.visual.runner.sites), 1)
        await self.visual.shutdown()


@unittest.skipIf(sys.platform == 'win32', '需要 POSIX shell')
class DeviceGuardTests(unittest.TestCase):
    def test_led_status_has_no_code_and_app_switch_must_be_off(self):
        source = (Path(__file__).parents[1] / 'core/services/oh2p_music_visual.sh').read_text()
        function = 'safe()' + source.split('safe()', 1)[1].split('playing()', 1)[0]
        fixtures = '''
ubus() { printf '%s' "$5"; }
json_load() { document=$1; }
json_get_var() {
    case "$1" in
        state) state=$fixture_led;; code) code=0;; setting) setting=$fixture_setting;;
    esac
}
'''
        for led, setting, expected in [('stored led ids: ; current id 0', '0', 0),
                                       ('stored led ids: ; current id 0', '1', 1),
                                       ('stored led ids: ; current id 2', '0', 1)]:
            result = subprocess.run(['/bin/sh', '-c', fixtures + function + '\nsafe', 'sh'],
                                    env={'fixture_led': led, 'fixture_setting': setting})
            self.assertEqual(result.returncode, expected)

    def test_only_music_playback_is_eligible(self):
        source = (Path(__file__).parents[1] / 'core/services/oh2p_music_visual.sh').read_text()
        function = 'playing()' + source.split('playing()', 1)[1].split('command -v', 1)[0]
        fixtures = '''
ubus() { return 0; }
json_load() { return 0; }
json_get_var() {
    case "$1" in
        code) code=0;; info) info=unused;; state) state=$fixture_status;; media) media=$fixture_type;;
    esac
}
'''
        for status, media, expected in [('1', '3', 0), ('2', '3', 1), ('0', '3', 1), ('1', '1', 1), ('1', '', 1)]:
            result = subprocess.run(['/bin/sh', '-c', fixtures + function + '\nplaying', 'sh'],
                                    env={'fixture_status': status, 'fixture_type': media})
            self.assertEqual(result.returncode, expected)


if __name__ == '__main__':
    unittest.main()
