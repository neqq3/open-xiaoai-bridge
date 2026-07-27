"""Reusable OpenAI-compatible streaming helpers.

The helpers in this module understand only standard SSE framing and OpenAI
``delta.content`` payloads. Backend-specific events, such as Hermes tool
progress, are interpreted by their own backend modules.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re


@dataclass(frozen=True)
class SSEEvent:
    """One decoded Server-Sent Event."""

    event: str
    data: str


class SSEDecoder:
    """Incrementally decode SSE blocks without assuming network boundaries."""

    def __init__(self):
        self._buffer = ""

    def feed(self, text: str, *, final: bool = False) -> list[SSEEvent]:
        self._buffer += text.replace("\r\n", "\n").replace("\r", "\n")
        blocks = self._buffer.split("\n\n")
        if final:
            self._buffer = ""
        else:
            self._buffer = blocks.pop()

        events: list[SSEEvent] = []
        for block in blocks:
            event = "message"
            data_lines: list[str] = []
            for line in block.splitlines():
                if not line or line.startswith(":"):
                    continue
                field, separator, value = line.partition(":")
                if separator and value.startswith(" "):
                    value = value[1:]
                if field == "event":
                    event = value or "message"
                elif field == "data":
                    data_lines.append(value)
            if data_lines:
                events.append(SSEEvent(event=event, data="\n".join(data_lines)))
        return events


class SentenceChunker:
    """Frame streamed text into ordered, sentence-safe TTS chunks."""

    _BOUNDARY_RE = re.compile(r"[。！？!?；;\n]")

    def __init__(self, min_chars: int = 24, max_chars: int = 160):
        self.min_chars = max(1, int(min_chars))
        self.max_chars = max(self.min_chars, int(max_chars))
        self._buffer = ""

    def feed(self, delta: str, *, final: bool = False) -> list[str]:
        self._buffer += delta
        chunks: list[str] = []

        while True:
            boundaries = list(self._BOUNDARY_RE.finditer(self._buffer))
            if not boundaries:
                break

            cut = None
            for match in boundaries:
                if match.end() >= self.min_chars:
                    cut = match.end()
                    break
            if cut is None:
                if final:
                    cut = boundaries[-1].end()
                else:
                    break

            chunk = self._buffer[:cut].strip()
            self._buffer = self._buffer[cut:].lstrip()
            chunks.extend(self._split_oversized(chunk))

        if final and self._buffer.strip():
            chunks.extend(self._split_oversized(self._buffer.strip()))
            self._buffer = ""
        return [chunk for chunk in chunks if chunk]

    def _split_oversized(self, text: str) -> list[str]:
        if len(text) <= self.max_chars:
            return [text]
        return [
            text[index : index + self.max_chars].strip()
            for index in range(0, len(text), self.max_chars)
            if text[index : index + self.max_chars].strip()
        ]


def extract_openai_delta(data: str) -> tuple[str, str | None]:
    """Extract text delta and finish reason from one OpenAI SSE data item."""

    body = json.loads(data)
    choices = body.get("choices") if isinstance(body, dict) else None
    if not choices or not isinstance(choices[0], dict):
        return "", None
    choice = choices[0]
    delta = choice.get("delta")
    content = delta.get("content") if isinstance(delta, dict) else ""
    if isinstance(content, list):
        content = "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return (content if isinstance(content, str) else ""), choice.get("finish_reason")
