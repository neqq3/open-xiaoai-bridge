import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())

router_module = importlib.import_module("core.services.tts.router")
TTSRouter = router_module.TTSRouter


class TTSRouterProviderTest(unittest.TestCase):
    def test_explicit_provider_is_shared_by_all_backends(self):
        for provider in ("xiaoai", "doubao", "openai", "mlx_audio"):
            self.assertEqual(
                provider,
                TTSRouter.resolve_provider(provider, "xiaoai"),
            )

    def test_missing_provider_preserves_legacy_speaker_selection(self):
        self.assertEqual("xiaoai", TTSRouter.resolve_provider(None, "xiaoai"))
        self.assertEqual("doubao", TTSRouter.resolve_provider(None, None))
        self.assertEqual("doubao", TTSRouter.resolve_provider(None, "alloy"))


class BackendTTSProviderConfigTest(unittest.TestCase):
    def test_openclaw_reads_tts_provider_without_changing_existing_speaker_keys(self):
        module = importlib.import_module("core.openclaw")
        manager = module.OpenClawManager
        config = {
            "tts_provider": "mlx_audio",
            "tts_speaker": "xiaoai",
            "agent_tts_speakers": {"assistant": "xiaoai"},
        }
        config_manager = types.SimpleNamespace(
            get_app_config=lambda *_args: config,
            add_reload_listener=lambda *_args: None,
        )
        previous_listener_state = manager._reload_listener_registered
        try:
            manager._reload_listener_registered = False
            with patch.object(
                module.ConfigManager, "instance", return_value=config_manager
            ):
                manager.reload_from_config(enabled=True)
            self.assertEqual("mlx_audio", manager._tts_provider)
            self.assertEqual("xiaoai", manager._tts_speaker)
            self.assertEqual({"assistant": "xiaoai"}, manager._agent_tts_speakers)
        finally:
            manager._reload_listener_registered = previous_listener_state

    def test_qwenpaw_reads_tts_provider_without_changing_existing_speaker_keys(self):
        module = importlib.import_module("core.qwenpaw")
        manager = module.QwenPawManager
        config = {
            "tts_provider": "openai",
            "tts_speaker": "sage",
            "session_tts_speakers": {"agent:default:main": "sage"},
        }
        config_manager = types.SimpleNamespace(
            get_app_config=lambda *_args: config,
            add_reload_listener=lambda *_args: None,
        )
        previous_listener_state = manager._reload_listener_registered
        try:
            manager._reload_listener_registered = False
            with patch.object(
                module.ConfigManager, "instance", return_value=config_manager
            ):
                manager.reload_from_config(enabled=True)
            self.assertEqual("openai", manager._tts_provider)
            self.assertEqual("sage", manager._tts_speaker)
            self.assertEqual(
                {"agent:default:main": "sage"}, manager._session_tts_speakers
            )
        finally:
            manager._reload_listener_registered = previous_listener_state


class BackendTTSRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_openclaw_delegates_to_shared_router(self):
        module = importlib.import_module("core.openclaw")
        manager = module.OpenClawManager
        previous = (
            manager._tts_provider,
            manager._tts_speaker,
            manager._tts_speed,
            manager._session_key,
            manager._initialized,
        )
        manager._tts_provider = "mlx_audio"
        manager._tts_speaker = "xiaoai"
        manager._tts_speed = 1.1
        manager._session_key = "agent:assistant:main"
        manager._initialized = True
        try:
            with patch.object(
                router_module.TTSRouter, "play", new=AsyncMock()
            ) as play:
                await manager._play_response_with_tts("你好", playback_token=7)
            play.assert_awaited_once_with(
                "你好",
                configured_provider="mlx_audio",
                tts_speaker="xiaoai",
                tts_speed=1.1,
                playback_token=7,
                log_prefix="OpenClaw",
            )
        finally:
            (
                manager._tts_provider,
                manager._tts_speaker,
                manager._tts_speed,
                manager._session_key,
                manager._initialized,
            ) = previous

    async def test_qwenpaw_delegates_to_shared_router(self):
        module = importlib.import_module("core.qwenpaw")
        manager = module.QwenPawManager
        previous = (
            manager._tts_provider,
            manager._tts_speaker,
            manager._tts_speed,
        )
        manager._tts_provider = "openai"
        manager._tts_speaker = "sage"
        manager._tts_speed = 0.9
        try:
            with patch.object(
                router_module.TTSRouter, "play", new=AsyncMock()
            ) as play:
                await manager._play_response_with_tts("你好", playback_token=8)
            play.assert_awaited_once_with(
                "你好",
                configured_provider="openai",
                tts_speaker="sage",
                tts_speed=0.9,
                playback_token=8,
                log_prefix="QwenPaw",
            )
        finally:
            manager._tts_provider, manager._tts_speaker, manager._tts_speed = previous


class TTSRouterResultTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.speaker = types.SimpleNamespace(
            play=AsyncMock(return_value=True),
            play_server_file=AsyncMock(return_value=True),
        )
        self.enterContext(patch.object(
            importlib.import_module("core.ref"), "get_speaker", return_value=self.speaker,
        ))
        self.active = self.enterContext(patch.object(
            router_module.open_xiaoai_server, "is_playback_session_active",
            return_value=True, create=True,
        ))
        self.config = self.enterContext(patch.object(
            router_module.ConfigManager.instance(), "get_app_config",
            return_value={"base_url": "http://tts.test/v1", "response_format": "wav"},
        ))
        self.synthesize = self.enterContext(patch.object(
            router_module.OpenAITTS, "synthesize", new=AsyncMock(return_value=b"audio"),
        ))

    async def play(self, provider="openai", token=71):
        return await TTSRouter.play(
            "一句话", configured_provider=provider, tts_speaker="xiaoai",
            playback_token=token,
        )

    async def test_native_returns_actual_boolean_without_retry(self):
        for value in (True, False):
            with self.subTest(value=value):
                self.speaker.play.reset_mock()
                self.speaker.play.return_value = value
                self.assertIs(await self.play("xiaoai"), value)
                self.speaker.play.assert_awaited_once()
        self.speaker.play.reset_mock()
        self.speaker.play.side_effect = RuntimeError("native failed")
        self.assertIs(await self.play("xiaoai"), False)
        self.speaker.play.assert_awaited_once()

    async def test_missing_speaker_is_failure_for_native_and_file(self):
        with patch.object(importlib.import_module("core.ref"), "get_speaker", return_value=None):
            self.assertIs(await self.play("xiaoai"), False)
            self.assertIs(await self.play(), False)

    async def test_doubao_none_means_normal_interface_completion(self):
        with patch.object(TTSRouter, "_play_doubao", new=AsyncMock(return_value=None)) as play:
            self.assertIs(await self.play("doubao"), True)
            self.assertEqual(71, play.await_args.kwargs["playback_token"])
        self.speaker.play.assert_not_awaited()

    async def test_file_providers_return_success_forward_token_and_clean_file(self):
        for provider in ("openai", "mlx_audio"):
            with self.subTest(provider=provider):
                async def play_file(*, file_path, blocking, playback_token):
                    self.assertTrue(Path(file_path).is_file())
                    self.assertTrue(blocking)
                    self.assertEqual(71, playback_token)
                    return True
                self.speaker.play_server_file.side_effect = play_file
                self.assertIs(await self.play(provider), True)
                path = self.speaker.play_server_file.await_args.kwargs["file_path"]
                self.assertFalse(Path(path).exists())
        self.speaker.play.assert_not_awaited()

    async def test_provider_failure_returns_fallback_result_once(self):
        for fallback_result in (True, False, RuntimeError("fallback failed")):
            with self.subTest(fallback_result=fallback_result):
                self.speaker.play.reset_mock()
                self.speaker.play.side_effect = (
                    fallback_result if isinstance(fallback_result, Exception) else None
                )
                self.speaker.play.return_value = fallback_result
                self.synthesize.side_effect = TimeoutError("synthesis failed")
                self.assertIs(await self.play(), fallback_result is True)
                self.speaker.play.assert_awaited_once_with(text="一句话", blocking=True)

    async def test_file_failure_cleans_before_single_fallback(self):
        for failure in (False, RuntimeError("file failed")):
            with self.subTest(failure=failure):
                self.speaker.play.reset_mock()
                self.speaker.play_server_file.side_effect = (
                    failure if isinstance(failure, Exception) else None
                )
                self.speaker.play_server_file.return_value = failure
                async def fallback(**_kwargs):
                    path = self.speaker.play_server_file.await_args.kwargs["file_path"]
                    self.assertFalse(Path(path).exists())
                    return True
                self.speaker.play.side_effect = fallback
                self.assertIs(await self.play(), True)
                self.speaker.play.assert_awaited_once()

    async def test_cancellation_during_synthesis_file_or_fallback_propagates(self):
        for stage in ("synthesis", "file", "fallback"):
            with self.subTest(stage=stage):
                self.speaker.play.reset_mock()
                self.speaker.play_server_file.reset_mock()
                self.speaker.play.side_effect = None
                self.speaker.play_server_file.side_effect = None
                self.synthesize.side_effect = None
                target = self.synthesize if stage == "synthesis" else self.speaker.play_server_file
                if stage == "fallback":
                    self.synthesize.side_effect = RuntimeError("provider failed")
                    target = self.speaker.play
                started = asyncio.Event()
                async def block(*_args, **_kwargs):
                    started.set()
                    await asyncio.Future()
                target.side_effect = block
                task = asyncio.create_task(self.play())
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(int(stage == "fallback"), self.speaker.play.await_count)
                if stage == "file":
                    path = self.speaker.play_server_file.await_args.kwargs["file_path"]
                    self.assertFalse(Path(path).exists())

    async def test_stale_token_never_starts_a_request(self):
        self.active.return_value = False
        with self.assertRaises(asyncio.CancelledError):
            await self.play()
        self.synthesize.assert_not_awaited()
        self.speaker.play.assert_not_awaited()

    async def test_late_synthesis_result_or_error_cannot_start_playback_or_fallback(self):
        for error in (False, True):
            with self.subTest(error=error):
                self.active.return_value = True
                async def synthesize(_text):
                    self.active.return_value = False
                    if error:
                        raise RuntimeError("late HTTP error")
                    return b"audio"
                self.synthesize.side_effect = synthesize
                with self.assertRaises(asyncio.CancelledError):
                    await self.play()
        self.speaker.play.assert_not_awaited()
        self.speaker.play_server_file.assert_not_awaited()

    async def test_late_file_failure_cleans_without_fallback(self):
        async def play_file(**_kwargs):
            self.active.return_value = False
            raise RuntimeError("late file error")
        self.speaker.play_server_file.side_effect = play_file
        with self.assertRaises(asyncio.CancelledError):
            await self.play()
        path = self.speaker.play_server_file.await_args.kwargs["file_path"]
        self.assertFalse(Path(path).exists())
        self.speaker.play.assert_not_awaited()

    async def test_no_token_keeps_legacy_calls_without_query(self):
        self.assertIs(await self.play("xiaoai", token=None), True)
        self.assertIs(await self.play(token=None), True)
        self.active.assert_not_called()


if __name__ == "__main__":
    unittest.main()
