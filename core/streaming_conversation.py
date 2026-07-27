"""Shared streamed-text delivery for OpenAI-compatible backends."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from core.external_conversation import ExternalConversationController
from core.openai_stream import SentenceChunker
from core.speech_queue import SequentialSpeechQueue
from core.utils.logger import logger


class StreamingConversationController(ExternalConversationController):
    """Add optional SSE-to-sequential-TTS delivery to an external backend.

    Subclasses decide whether streaming is enabled and may provide a progress
    narrator. The queue and fallback rules stay protocol-neutral.
    """

    STREAMING_DEFAULT = False

    def _stream_config(self) -> dict[str, Any]:
        value = self._cfg("streaming", self.STREAMING_DEFAULT)
        if isinstance(value, dict):
            return value
        return {"enabled": bool(value)}

    def _streaming_enabled(self) -> bool:
        return bool(self._stream_config().get("enabled", self.STREAMING_DEFAULT))

    def _progress_config(self) -> dict[str, Any]:
        return {}

    def _create_progress_narrator(self, _text: str):
        return None

    async def _request_stream(
        self,
        prompt: str,
        *,
        on_delta,
        on_progress,
    ) -> str | None:
        """Call the backend's generic OpenAI-compatible stream."""

        return await self.backend.request_streaming_chat_completion(
            prompt,
            on_delta=on_delta,
        )

    async def _request_backend_turn(
        self,
        text: str,
        *,
        play_send_sound: bool,
    ) -> tuple[str | None, bool]:
        if not self._streaming_enabled():
            return await super()._request_backend_turn(
                text,
                play_send_sound=play_send_sound,
            )
        return await self._request_streaming_turn(
            text,
            play_send_sound=play_send_sound,
        )

    async def _request_streaming_turn(
        self,
        text: str,
        *,
        play_send_sound: bool,
    ) -> tuple[str | None, bool]:
        """Stream complete clauses into one race-safe TTS worker."""

        prompt = text
        if self.backend._rule_prompt:
            prompt = text + "\n" + self.backend._rule_prompt

        stream_config = self._stream_config()
        chunker = SentenceChunker(
            min_chars=int(stream_config.get("sentence_min_chars", 24)),
            max_chars=int(stream_config.get("sentence_max_chars", 160)),
        )
        speech_queue = SequentialSpeechQueue()
        playback_ready = asyncio.Event()
        progress_stop = asyncio.Event()
        received_parts: list[str] = []
        delivered_final_chunks: list[str] = []
        narrator = self._create_progress_narrator(text)

        async def playback_worker():
            await playback_ready.wait()
            while True:
                item = await speech_queue.get()
                if item is None:
                    return
                try:
                    await self._play_tts(item.text)
                finally:
                    await speech_queue.finish(item)

        async def on_delta(delta: str):
            if not delta:
                return
            received_parts.append(delta)
            # The first real answer token makes all pending/future progress
            # stale, even if a complete TTS sentence has not formed yet.
            removed = await speech_queue.mark_final_started()
            if removed:
                logger.debug(
                    f"Skipped {removed} stale queued progress item(s)",
                    module=self.LOG_MODULE,
                )
            for sentence in chunker.feed(delta):
                delivered_final_chunks.append(sentence)
                await speech_queue.put_final(sentence)

        async def on_progress(event):
            if narrator is not None:
                narrator.observe(event)

        async def progress_announcer():
            progress = self._progress_config()
            if narrator is None or not bool(progress.get("enabled", True)):
                return
            initial_delay = max(1.0, float(progress.get("initial_delay", 8)))
            min_interval = max(3.0, float(progress.get("min_interval", 20)))
            max_messages = max(0, int(progress.get("max_messages", 2)))

            await playback_ready.wait()
            try:
                await asyncio.wait_for(progress_stop.wait(), timeout=initial_delay)
                return
            except asyncio.TimeoutError:
                pass

            for _ in range(max_messages):
                if speech_queue.final_started or progress_stop.is_set():
                    return
                announcement = narrator.next_message()
                if announcement:
                    message, key = announcement
                    await speech_queue.put_progress(message, key=key)
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
        logger.user_speech(prompt, module=f"{self.BACKEND_NAME}({self.backend._session_key})")

        stream_task = asyncio.create_task(
            self._request_stream(
                prompt,
                on_delta=on_delta,
                on_progress=on_progress,
            )
        )
        if play_send_sound:
            await self._play_send_sound()
        playback_ready.set()

        response: str | None = None
        try:
            response = await stream_task
            for sentence in chunker.feed("", final=True):
                delivered_final_chunks.append(sentence)
                await speech_queue.put_final(sentence)
        except Exception as exc:
            if not delivered_final_chunks:
                logger.warning(
                    f"Streaming unavailable, falling back to non-streaming: "
                    f"{type(exc).__name__}: {exc}",
                    module=self.LOG_MODULE,
                )
                response = await self.backend._request_chat_completion(prompt)
                if response:
                    fallback_chunker = SentenceChunker(
                        min_chars=1,
                        max_chars=int(stream_config.get("sentence_max_chars", 160)),
                    )
                    for sentence in fallback_chunker.feed(response, final=True):
                        delivered_final_chunks.append(sentence)
                        await speech_queue.put_final(sentence)
            else:
                # Retrying after speech started can produce different wording
                # and duplicate the answer. Finish buffered text and report the
                # interruption instead.
                logger.error(
                    f"Stream interrupted after final TTS started: "
                    f"{type(exc).__name__}: {exc}",
                    module=self.LOG_MODULE,
                )
                for sentence in chunker.feed("", final=True):
                    delivered_final_chunks.append(sentence)
                    await speech_queue.put_final(sentence)
                interruption = "回答传输中断了，请再问一次"
                delivered_final_chunks.append(interruption)
                await speech_queue.put_final(interruption)
                response = "".join(received_parts).strip() or None
        finally:
            progress_stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task
            await speech_queue.close()
            await worker_task

        return response, bool(delivered_final_chunks)
