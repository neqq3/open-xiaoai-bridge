"""OpenAI-compatible continuous conversation controller."""

import asyncio
import contextlib
from typing import Any

from core.openai import OpenAIManager
from core.external_conversation import ExternalConversationController
from core.openai_voice import (
    PROGRESS_MESSAGES,
    SentenceChunker,
    interpret_voice_mode,
    mode_confirmation,
    mode_prompt,
    normalize_voice_mode,
    safe_tool_category,
)
from core.utils.logger import logger


class OpenAIConversationController(ExternalConversationController):
    """Manages multi-turn conversation for OpenAI-compatible services."""

    CONFIG_PREFIX = "openai"
    BACKEND_NAME = "OpenAI"
    LOG_MODULE = "OpenAI Conv"
    WAKEUP_SOURCE = "openai"
    MANAGER = OpenAIManager

    def __init__(self):
        super().__init__()
        # A mode switch belongs to the current OpenAI/Hermes session only.
        # Dynamic session_key changes therefore do not leak a mode to another
        # agent or user.
        self._session_voice_modes: dict[str, str] = {}

    def _voice_config(self) -> dict[str, Any]:
        value = self._cfg("voice", {})
        return value if isinstance(value, dict) else {}

    def _voice_enabled(self) -> bool:
        return bool(self._voice_config().get("enabled", False))

    def _hermes_streaming_enabled(self) -> bool:
        hermes = self._voice_config().get("hermes", {})
        return bool(
            isinstance(hermes, dict)
            and hermes.get("enabled", False)
            and hermes.get("streaming", True)
        )

    def _current_voice_mode(self) -> str:
        voice = self._voice_config()
        default_mode = normalize_voice_mode(
            voice.get("default_mode", "standard")
        )
        return self._session_voice_modes.get(
            self.backend._session_key,
            default_mode,
        )

    async def _request_backend_turn(
        self,
        text: str,
        *,
        play_send_sound: bool,
    ) -> tuple[str | None, bool]:
        """Apply voice strategy and optionally use Hermes' enhanced SSE."""
        if not self._voice_enabled():
            return await super()._request_backend_turn(
                text,
                play_send_sound=play_send_sound,
            )

        decision = interpret_voice_mode(text, self._current_voice_mode())
        if decision.is_switch_command:
            self._session_voice_modes[self.backend._session_key] = decision.mode
            logger.info(
                f"Voice mode switched to {decision.mode}",
                module=self.LOG_MODULE,
            )
            return mode_confirmation(decision.mode), False

        query = decision.query or text
        voice = self._voice_config()
        prompt = query + "\n" + mode_prompt(
            decision.mode,
            voice.get("mode_prompts"),
        )
        logger.info(
            f"Voice turn mode={decision.mode}, "
            f"one_shot={decision.mode != self._current_voice_mode()}",
            module=self.LOG_MODULE,
        )

        if not self._hermes_streaming_enabled():
            return await self._request_non_streaming_voice_turn(
                prompt,
                play_send_sound=play_send_sound,
            )

        return await self._request_streaming_voice_turn(
            prompt,
            play_send_sound=play_send_sound,
        )

    async def _request_non_streaming_voice_turn(
        self,
        prompt: str,
        *,
        play_send_sound: bool,
    ) -> tuple[str | None, bool]:
        """Keep the established OpenAI-compatible non-streaming path."""
        if play_send_sound:
            run_id = await self.backend._send_and_track(prompt)
            await self._play_send_sound()
            response = (
                await self.backend._wait_response(run_id)
                if run_id
                else None
            )
        else:
            response = await self.backend.send(prompt, wait_response=True)
        return response, False

    async def _request_streaming_voice_turn(
        self,
        prompt: str,
        *,
        play_send_sound: bool,
    ) -> tuple[str | None, bool]:
        """Stream Hermes text into a single ordered TTS worker."""
        voice = self._voice_config()
        hermes = voice.get("hermes", {})
        if not isinstance(hermes, dict):
            hermes = {}
        progress_config = hermes.get("progress", {})
        if not isinstance(progress_config, dict):
            progress_config = {}

        chunker = SentenceChunker(
            min_chars=int(hermes.get("sentence_min_chars", 16)),
            max_chars=int(hermes.get("sentence_max_chars", 160)),
        )
        playback_queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        playback_ready = asyncio.Event()
        final_arrived = asyncio.Event()
        progress_stop = asyncio.Event()
        received_parts: list[str] = []
        delivered_final_chunks: list[str] = []
        latest_tool_category = "working"

        async def playback_worker():
            await playback_ready.wait()
            while True:
                item = await playback_queue.get()
                try:
                    if item is None:
                        return
                    kind, spoken_text = item
                    # Drop a stale queued status as soon as final text exists.
                    if kind == "progress" and final_arrived.is_set():
                        continue
                    await self._play_tts(spoken_text)
                finally:
                    playback_queue.task_done()

        async def on_delta(delta: str):
            received_parts.append(delta)
            for sentence in chunker.feed(delta):
                final_arrived.set()
                delivered_final_chunks.append(sentence)
                await playback_queue.put(("final", sentence))

        async def on_tool_progress(event: dict[str, Any]):
            nonlocal latest_tool_category
            if event.get("status") == "running":
                latest_tool_category = safe_tool_category(event.get("tool"))

        async def progress_announcer():
            if not bool(progress_config.get("enabled", True)):
                return
            initial_delay = max(
                1.0,
                float(progress_config.get("initial_delay", 8)),
            )
            min_interval = max(
                3.0,
                float(progress_config.get("min_interval", 20)),
            )
            max_messages = max(
                0,
                int(progress_config.get("max_messages", 2)),
            )
            await playback_ready.wait()
            try:
                await asyncio.wait_for(progress_stop.wait(), timeout=initial_delay)
                return
            except asyncio.TimeoutError:
                pass

            announced: set[str] = set()
            for _ in range(max_messages):
                if final_arrived.is_set() or progress_stop.is_set():
                    return
                category = latest_tool_category
                if category in announced:
                    category = "working"
                if category in announced:
                    return
                announced.add(category)
                await playback_queue.put(
                    ("progress", PROGRESS_MESSAGES[category])
                )
                try:
                    await asyncio.wait_for(
                        progress_stop.wait(),
                        timeout=min_interval,
                    )
                    return
                except asyncio.TimeoutError:
                    continue

        await self._stop_recording()
        worker_task = asyncio.create_task(playback_worker())
        progress_task = asyncio.create_task(progress_announcer())
        logger.user_speech(prompt, module=f"OpenAI({self.backend._session_key})")

        stream_task = asyncio.create_task(
            self.backend.request_streaming_chat_completion(
                prompt,
                on_delta=on_delta,
                on_tool_progress=on_tool_progress,
            )
        )
        if play_send_sound:
            await self._play_send_sound()
        playback_ready.set()

        response: str | None = None
        try:
            response = await stream_task
            for sentence in chunker.feed("", final=True):
                final_arrived.set()
                delivered_final_chunks.append(sentence)
                await playback_queue.put(("final", sentence))
        except Exception as exc:
            if not delivered_final_chunks:
                logger.warning(
                    f"Hermes streaming unavailable, falling back to "
                    f"non-streaming: {type(exc).__name__}: {exc}",
                    module=self.LOG_MODULE,
                )
                # No final sentence has been queued, so replaying the stable
                # non-streaming response cannot duplicate spoken answer text.
                response = await self.backend._request_chat_completion(prompt)
                if response:
                    fallback_chunker = SentenceChunker(
                        min_chars=1,
                        max_chars=int(hermes.get("sentence_max_chars", 160)),
                    )
                    for sentence in fallback_chunker.feed(response, final=True):
                        final_arrived.set()
                        delivered_final_chunks.append(sentence)
                        await playback_queue.put(("final", sentence))
            else:
                # A mid-stream retry could produce different wording and
                # duplicate already spoken text. Preserve order and be honest
                # about the interruption instead.
                logger.error(
                    f"Hermes stream interrupted after final TTS started: "
                    f"{type(exc).__name__}: {exc}",
                    module=self.LOG_MODULE,
                )
                for sentence in chunker.feed("", final=True):
                    delivered_final_chunks.append(sentence)
                    await playback_queue.put(("final", sentence))
                await playback_queue.put(
                    ("final", "回答传输中断了，请再问一次")
                )
                response = "".join(received_parts).strip() or None
        finally:
            progress_stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task
            await playback_queue.put(None)
            await playback_queue.join()
            await worker_task

        return response, bool(delivered_final_chunks)
