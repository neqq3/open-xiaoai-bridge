"""将 Hermes 工具生命周期事件安全转换为可播报进度。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|authorization|secret|password)"
    r"\b\s*[:=]\s*\S+"
)


@dataclass(frozen=True)
class HermesToolProgress:
    """Hermes Chat Completions 发出的结构化工具生命周期事件。"""

    tool: str
    status: str
    tool_call_id: str
    label: str = ""
    emoji: str = ""

    @classmethod
    def from_json(cls, data: str) -> "HermesToolProgress | None":
        body = json.loads(data)
        if not isinstance(body, dict):
            return None
        tool = str(body.get("tool") or "").strip()
        status = str(body.get("status") or "").strip().lower()
        tool_call_id = str(body.get("toolCallId") or "").strip()
        if not tool or status not in {
            "running",
            "completed",
            "failed",
        }:
            return None
        return cls(
            tool=tool,
            status=status,
            tool_call_id=tool_call_id,
            label=str(body.get("label") or "").strip(),
            emoji=str(body.get("emoji") or "").strip(),
        )

    def technical_log(self) -> dict[str, str]:
        """返回已脱敏的生命周期字段，供技术日志使用。"""

        return {
            "tool": self.tool,
            "status": self.status,
            "tool_call_id": self.tool_call_id,
            "label": self._safe_log_label(self.label),
            "emoji": self.emoji,
        }

    @staticmethod
    def _safe_log_label(label: str) -> str:
        redacted = _URL_RE.sub("[URL]", str(label or ""))
        redacted = _SECRET_RE.sub(
            r"\1=[REDACTED]",
            redacted,
        )
        return redacted[:500]


@dataclass(frozen=True)
class _ProgressCategory:
    name: str
    message: str
    tool_terms: tuple[str, ...]
    task_terms: tuple[str, ...] = ()


_CATEGORIES = (
    _ProgressCategory(
        "weather",
        "正在查看天气，请稍等",
        ("weather", "forecast", "meteorology"),
        ("天气", "下雨", "带伞", "气温", "温度", "空气质量"),
    ),
    _ProgressCategory(
        "music_service",
        "正在连接音乐服务",
        (
            "music_login",
            "music_connect",
            "spotify_auth",
            "netease_auth",
        ),
    ),
    _ProgressCategory(
        "music",
        "正在帮你找这首歌",
        (
            "music",
            "song",
            "playlist",
            "spotify",
            "netease",
            "youtube_music",
        ),
        ("歌曲", "这首歌", "音乐", "歌单", "播放"),
    ),
    _ProgressCategory(
        "home",
        "正在查看家里的设备状态",
        (
            "home_assistant",
            "homeassistant",
            "smart_home",
            "hass",
            "iot",
            "device_state",
        ),
        ("家里的设备", "智能家居", "灯", "空调", "门锁", "传感器"),
    ),
    _ProgressCategory(
        "lark",
        "正在处理飞书里的内容",
        ("lark", "feishu", "bitable"),
        ("飞书", "多维表格", "妙记"),
    ),
    _ProgressCategory(
        "calendar",
        "正在查看日程",
        ("calendar", "schedule", "agenda"),
        ("日程", "行程", "会议", "安排"),
    ),
    _ProgressCategory(
        "market",
        "正在查看最新行情",
        ("finance", "stock", "market", "price", "crypto", "quote"),
        ("行情", "股价", "显卡价格", "价格走势", "汇率"),
    ),
    _ProgressCategory(
        "research",
        "正在查最新资料",
        (
            "web_search",
            "web_extract",
            "browser",
            "search",
            "fetch",
            "wikipedia",
            "news",
        ),
        ("最新", "查一下", "搜索", "资料", "新闻"),
    ),
    _ProgressCategory(
        "memory",
        "正在查找相关记录",
        ("memory", "recall", "session_search"),
        ("之前", "记得", "记录", "我说过"),
    ),
)


class HermesProgressNarrator:
    """将 Hermes 事件归类为预定义的用户进度提示。"""

    def __init__(self, user_text: str):
        self._user_text = str(user_text or "").lower()
        self._latest_category = self._classify_task()
        self._active_calls: dict[str, str] = {}
        self._announced: set[str] = set()
        self._completed_since_announcement = False
        self._organizing_announced = False

    def observe(self, event: HermesToolProgress):
        category = self._classify_tool(
            event.tool
        ) or self._classify_task()
        if category:
            self._latest_category = category

        call_key = event.tool_call_id or (
            f"{event.tool}:{event.status}"
        )
        if event.status == "running":
            self._active_calls[call_key] = category or "working"
        elif event.status == "completed":
            if self._active_calls.pop(call_key, None) is not None:
                self._completed_since_announcement = True
        elif event.status == "failed":
            # 失败只结束对应调用，绝不能进入“已经找到信息”的成功汇总路径。
            # Hermes 仍可继续执行其他工具并自行生成最终回答。
            self._active_calls.pop(call_key, None)

    def next_message(self) -> tuple[str, str] | None:
        """返回安全提示和用于语义去重的键。"""

        if (
            self._completed_since_announcement
            and self._announced
            and not self._organizing_announced
        ):
            self._completed_since_announcement = False
            self._organizing_announced = True
            return "已经找到一些信息，正在整理", "organizing"

        category = self._latest_category
        if category and category not in self._announced:
            self._announced.add(category)
            return self._message_for(category), category

        if not self._announced:
            self._announced.add("working")
            return "还在为你处理，请稍等", "working"
        return None

    def _classify_tool(self, tool: Any) -> str | None:
        normalized = str(tool or "").strip().lower()
        if not normalized:
            return None
        # 连接和鉴权类事件必须优先于宽泛的音乐关键词。
        for category in _CATEGORIES:
            if any(
                term in normalized
                for term in category.tool_terms
            ):
                return category.name
        return None

    def _classify_task(self) -> str | None:
        for category in _CATEGORIES:
            if any(
                term in self._user_text
                for term in category.task_terms
            ):
                return category.name
        return None

    @staticmethod
    def _message_for(name: str) -> str:
        for category in _CATEGORIES:
            if category.name == name:
                return category.message
        return "还在为你处理，请稍等"
