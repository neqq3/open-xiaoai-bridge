import unittest

from core.hermes_progress import HermesProgressNarrator, HermesToolProgress


class HermesProgressNarratorTest(unittest.TestCase):
    def test_tool_names_and_labels_are_never_spoken(self):
        narrator = HermesProgressNarrator("帮我看看今天会不会下雨")
        event = HermesToolProgress(
            tool="mcp__weather__forecast",
            status="running",
            tool_call_id="call-1",
            label=(
                "GET https://private.example/api?"
                "token=secret-value"
            ),
        )

        narrator.observe(event)
        message, key = narrator.next_message()

        self.assertEqual("weather", key)
        self.assertEqual("正在查看天气，请稍等", message)
        self.assertNotIn("mcp", message.lower())
        self.assertNotIn("https://", message)
        self.assertNotIn("secret-value", message)

    def test_repeated_events_are_deduplicated_then_summarized(self):
        narrator = HermesProgressNarrator("查一下最新资料")
        running = HermesToolProgress(
            tool="web_search",
            status="running",
            tool_call_id="call-1",
        )
        completed = HermesToolProgress(
            tool="web_search",
            status="completed",
            tool_call_id="call-1",
        )

        narrator.observe(running)
        self.assertEqual(
            ("正在查最新资料", "research"),
            narrator.next_message(),
        )
        narrator.observe(running)
        self.assertIsNone(narrator.next_message())
        narrator.observe(completed)
        self.assertEqual(
            ("已经找到一些信息，正在整理", "organizing"),
            narrator.next_message(),
        )
        self.assertIsNone(narrator.next_message())

    def test_technical_log_redacts_urls_and_secrets(self):
        event = HermesToolProgress(
            tool="web_search",
            status="running",
            tool_call_id="call-1",
            label=(
                "fetch https://private.example/path "
                "Authorization: Bearer-private"
            ),
        )

        label = event.technical_log()["label"]

        self.assertNotIn("private.example", label)
        self.assertNotIn("Bearer-private", label)
        self.assertIn("[URL]", label)
        self.assertIn("[REDACTED]", label)


if __name__ == "__main__":
    unittest.main()
