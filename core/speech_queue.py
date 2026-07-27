"""Single-worker speech queue used by streamed conversation backends."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Literal


SpeechKind = Literal["progress", "final"]


@dataclass(frozen=True)
class SpeechItem:
    """One item awaiting sequential TTS playback."""

    kind: SpeechKind
    text: str
    key: str = ""


class SequentialSpeechQueue:
    """Serialize TTS and atomically discard progress made stale by final text.

    The queue never interrupts an item that has already started playing. If
    final text wins the lock first, all pending progress is removed. If a
    progress item wins first, it is allowed to finish and final text follows.
    This matches the real native TTS constraint: interruption is less safe than
    a short, bounded delay.
    """

    def __init__(self):
        self._condition = asyncio.Condition()
        self._items: deque[SpeechItem] = deque()
        self._closed = False
        self._final_started = False
        self._playing: SpeechItem | None = None

    @property
    def final_started(self) -> bool:
        return self._final_started

    @property
    def playing(self) -> SpeechItem | None:
        return self._playing

    async def mark_final_started(self) -> int:
        """Block future progress and remove pending progress atomically."""

        async with self._condition:
            if self._final_started:
                return 0
            self._final_started = True
            kept = deque(item for item in self._items if item.kind != "progress")
            removed = len(self._items) - len(kept)
            self._items = kept
            self._condition.notify_all()
            return removed

    async def put_progress(self, text: str, *, key: str) -> bool:
        """Queue one non-duplicate progress item unless final text exists."""

        async with self._condition:
            if self._closed or self._final_started:
                return False
            if self._playing and self._playing.kind == "progress":
                if self._playing.key == key:
                    return False
            if any(
                item.kind == "progress" and item.key == key
                for item in self._items
            ):
                return False
            # Only the latest pending progress remains useful. This also
            # coalesces a burst of different tools before playback catches up.
            self._items = deque(
                item for item in self._items if item.kind != "progress"
            )
            self._items.append(SpeechItem("progress", text, key))
            self._condition.notify()
            return True

    async def put_final(self, text: str) -> bool:
        """Queue final text in arrival order."""

        normalized = text.strip()
        if not normalized:
            return False
        await self.mark_final_started()
        async with self._condition:
            if self._closed:
                return False
            self._items.append(SpeechItem("final", normalized))
            self._condition.notify()
            return True

    async def get(self) -> SpeechItem | None:
        """Return the next item, or ``None`` after a drained close."""

        async with self._condition:
            while not self._items and not self._closed:
                await self._condition.wait()
            if not self._items:
                return None
            item = self._items.popleft()
            self._playing = item
            return item

    async def finish(self, item: SpeechItem):
        """Mark an item as no longer playing."""

        async with self._condition:
            if self._playing is item:
                self._playing = None
            self._condition.notify_all()

    async def close(self):
        """Stop accepting items after all already queued speech is consumed."""

        async with self._condition:
            self._closed = True
            self._condition.notify_all()
