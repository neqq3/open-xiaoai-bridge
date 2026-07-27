import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.speech_queue import SequentialSpeechQueue


class SequentialSpeechQueueRaceTest(unittest.TestCase):
    def test_final_before_progress_is_never_followed_by_stale_status(self):
        async def scenario():
            queue = SequentialSpeechQueue()
            await queue.mark_final_started()
            queued = await queue.put_progress("正在查看天气", key="weather")
            await queue.put_final("最终答案")
            await queue.close()
            item = await queue.get()
            await queue.finish(item)
            return queued, item, await queue.get()

        queued, item, end = asyncio.run(scenario())
        self.assertFalse(queued)
        self.assertEqual(("final", "最终答案"), (item.kind, item.text))
        self.assertIsNone(end)

    def test_queued_progress_is_removed_when_final_wins_before_playback(self):
        async def scenario():
            queue = SequentialSpeechQueue()
            await queue.put_progress("正在查看天气", key="weather")
            removed = await queue.mark_final_started()
            await queue.put_final("最终答案")
            await queue.close()
            item = await queue.get()
            await queue.finish(item)
            return removed, item

        removed, item = asyncio.run(scenario())
        self.assertEqual(1, removed)
        self.assertEqual("final", item.kind)

    def test_progress_that_just_started_is_not_interrupted(self):
        async def scenario():
            queue = SequentialSpeechQueue()
            await queue.put_progress("正在查看天气", key="weather")
            progress = await queue.get()
            removed = await queue.mark_final_started()
            await queue.put_final("最终答案")
            await queue.finish(progress)
            final = await queue.get()
            await queue.finish(final)
            await queue.close()
            return removed, progress, final

        removed, progress, final = asyncio.run(scenario())
        self.assertEqual(0, removed)
        self.assertEqual("progress", progress.kind)
        self.assertEqual("final", final.kind)

    def test_final_arriving_during_progress_waits_without_overlap(self):
        async def scenario():
            queue = SequentialSpeechQueue()
            progress_started = asyncio.Event()
            release_progress = asyncio.Event()
            active = 0
            max_active = 0
            played = []

            async def play(item):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                played.append(("start", item.kind))
                if item.kind == "progress":
                    progress_started.set()
                    await release_progress.wait()
                played.append(("end", item.kind))
                active -= 1

            async def worker():
                while True:
                    item = await queue.get()
                    if item is None:
                        return
                    try:
                        await play(item)
                    finally:
                        await queue.finish(item)

            task = asyncio.create_task(worker())
            await queue.put_progress("正在查看天气", key="weather")
            await progress_started.wait()
            await queue.mark_final_started()
            await queue.put_final("最终答案")
            await asyncio.sleep(0)
            self.assertNotIn(("start", "final"), played)
            release_progress.set()
            await queue.close()
            await task
            return max_active, played

        max_active, played = asyncio.run(scenario())
        self.assertEqual(1, max_active)
        self.assertEqual(
            [
                ("start", "progress"),
                ("end", "progress"),
                ("start", "final"),
                ("end", "final"),
            ],
            played,
        )

    def test_final_after_completed_progress_keeps_natural_order(self):
        async def scenario():
            queue = SequentialSpeechQueue()
            await queue.put_progress("正在查看天气", key="weather")
            progress = await queue.get()
            await queue.finish(progress)
            await queue.mark_final_started()
            await queue.put_final("最终答案")
            final = await queue.get()
            await queue.finish(final)
            await queue.close()
            return progress.kind, final.kind

        self.assertEqual(
            ("progress", "final"),
            asyncio.run(scenario()),
        )


if __name__ == "__main__":
    unittest.main()
