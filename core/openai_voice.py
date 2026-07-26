"""OpenAI/Hermes voice interaction helpers.

This module intentionally contains no speaker or network code. Keeping mode
parsing, sentence framing and SSE parsing pure makes the ordering and
de-duplication rules straightforward to test.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any


VOICE_MODES = ("fast", "standard", "deep")

MODE_LABELS = {
    "fast": "快速",
    "standard": "标准",
    "deep": "深度",
}

DEFAULT_MODE_PROMPTS = {
    "fast": (
        "当前是语音快速模式。优先直接、简短回答，通常控制在120字以内，"
        "不要复述问题。减少不必要或等价的重复工具调用，但实时信息所必需的"
        "查询必须执行。只输出适合直接朗读的纯文字。"
    ),
    "standard": (
        "当前是语音标准模式。回答清晰、长度适中，通常控制在300字以内。"
        "按问题需要使用工具，并避免等价的重复查询。只输出适合直接朗读的纯文字。"
    ),
    "deep": (
        "当前是语音深度模式。可以更充分地检索、核对和使用必要工具，"
        "给出结构完整的答案，通常控制在700字以内。不要为了显得深入而重复"
        "等价查询。只输出适合直接朗读的纯文字。"
    ),
}

_PERSISTENT_COMMANDS = {
    "切换快速模式": "fast",
    "切换到快速模式": "fast",
    "进入快速模式": "fast",
    "切换标准模式": "standard",
    "切换到标准模式": "standard",
    "进入标准模式": "standard",
    "恢复标准模式": "standard",
    "切换深度模式": "deep",
    "切换到深度模式": "deep",
    "进入深度模式": "deep",
}

_ONE_SHOT_PREFIXES = (
    ("简单说一下", "fast"),
    ("简单说", "fast"),
    ("简短回答", "fast"),
    ("快速回答", "fast"),
    ("详细查一下", "deep"),
    ("详细查查", "deep"),
    ("深入查一下", "deep"),
    ("深入分析", "deep"),
)

_TRAILING_COMMAND_PUNCTUATION = "。！？!?，,；;：: "


@dataclass(frozen=True)
class VoiceModeDecision:
    """Result of interpreting a voice-mode phrase."""

    mode: str
    query: str | None
    persistent: bool = False

    @property
    def is_switch_command(self) -> bool:
        return self.persistent and self.query is None


def normalize_voice_mode(mode: Any, default: str = "standard") -> str:
    """Return a supported mode name."""
    normalized = str(mode or "").strip().lower()
    if normalized in VOICE_MODES:
        return normalized
    return default


def interpret_voice_mode(text: str, current_mode: str) -> VoiceModeDecision:
    """Interpret persistent and one-shot Chinese voice-mode phrases."""
    current_mode = normalize_voice_mode(current_mode)
    stripped = (text or "").strip()
    command = stripped.strip(_TRAILING_COMMAND_PUNCTUATION).replace(" ", "")
    persistent_mode = _PERSISTENT_COMMANDS.get(command)
    if persistent_mode:
        return VoiceModeDecision(
            mode=persistent_mode,
            query=None,
            persistent=True,
        )

    for prefix, mode in _ONE_SHOT_PREFIXES:
        if stripped.startswith(prefix):
            query = stripped[len(prefix) :].lstrip(_TRAILING_COMMAND_PUNCTUATION)
            if query:
                return VoiceModeDecision(mode=mode, query=query)

    return VoiceModeDecision(mode=current_mode, query=stripped)


def mode_confirmation(mode: str) -> str:
    """Build a short local confirmation for a persistent mode switch."""
    return f"已切换到{MODE_LABELS[normalize_voice_mode(mode)]}模式"


def mode_prompt(mode: str, overrides: Any = None) -> str:
    """Resolve a mode prompt, allowing config.py to override each profile."""
    normalized = normalize_voice_mode(mode)
    if isinstance(overrides, dict):
        override = overrides.get(normalized)
        if isinstance(override, str) and override.strip():
            return override.strip()
    return DEFAULT_MODE_PROMPTS[normalized]


@dataclass(frozen=True)
class SSEEvent:
    """One decoded Server-Sent Event."""

    event: str
    data: str


class SSEDecoder:
    """Incrementally decode SSE blocks without assuming network chunk borders."""

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

    def __init__(self, min_chars: int = 16, max_chars: int = 160):
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
                candidate_len = match.end()
                if candidate_len >= self.min_chars:
                    cut = candidate_len
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


def safe_tool_category(tool_name: Any) -> str:
    """Map a tool name to a safe spoken category without exposing arguments."""
    normalized = str(tool_name or "").strip().lower()
    if any(word in normalized for word in ("weather", "forecast")):
        return "weather"
    if any(
        word in normalized
        for word in ("web", "search", "browser", "fetch", "extract", "url")
    ):
        return "research"
    if any(word in normalized for word in ("calendar", "schedule")):
        return "calendar"
    if any(word in normalized for word in ("memory", "recall")):
        return "memory"
    if any(word in normalized for word in ("terminal", "shell", "code", "python")):
        return "compute"
    return "working"


PROGRESS_MESSAGES = {
    "weather": "我正在查询最新天气，请稍等",
    "research": "我正在查询并核对相关信息",
    "calendar": "我正在核对日程信息",
    "memory": "我正在查找相关记录",
    "compute": "我正在处理和核对结果",
    "working": "我还在处理，请稍等",
}
