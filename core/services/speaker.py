import asyncio
import os
import time
from typing import Literal

import open_xiaoai_server

from core.ref import get_xiaoai, set_speaker
from core.utils.base import json_decode, json_encode
from core.utils.logger import logger


class CommandResult:
    def __init__(self, stdout: str, stderr: str, exit_code: int):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class SpeakerManager:
    status: Literal["playing", "paused", "idle"] = "idle"
    _NATIVE_TTS_SCRIPT_MARKER = "tts_play.sh]"
    _NATIVE_TTS_COMPLETION_MARKER = "Audio playback completed successfully"
    _NATIVE_TTS_MAX_CHARS = 180
    _TTS_DIAGNOSTIC_LIMIT = 1200
    _TTS_TEXT_LOG_MARKERS = (
        "Script started with arguments:",
        "Text to speech:",
        "Final parameters - Text:",
        " - Text:",
    )

    def __init__(self):
        set_speaker(self)

    async def get_playing(self, sync=False):
        """获取播放状态"""
        if sync:
            # 同步远端最新状态
            res = await self.run_shell("mphelper mute_stat")
            if "1" in res.stdout:
                self.status = "playing"
            elif "2" in res.stdout:
                self.status = "paused"
        return self.status

    async def set_playing(self, playing=True):
        """播放/暂停"""
        command = "mphelper play" if playing else "mphelper pause"
        res = await self.run_shell(command)
        return '"code": 0' in res.stdout

    async def play(
        self,
        text=None,
        url=None,
        buffer=None,
        server_file=None,
        blocking=True,
        timeout=10 * 60 * 1000,
    ):
        """
        播放文字、音频链接、音频流

        参数:
            text: 文字内容
            url: 音频链接
            buffer: 音频流
            server_file: 服务端本地音频文件路径
            timeout: 超时时长（毫秒），默认10分钟
            blocking: 是否阻塞运行
        """
        if server_file is not None:
            return await self.play_server_file(
                file_path=server_file,
                blocking=blocking,
            )

        if buffer is not None:
            return get_xiaoai().on_output_data(buffer)

        if blocking:
            command = (
                f"miplayer -f '{url}'"
                if url
                else f"/usr/sbin/tts_play.sh '{text.replace("'", "'\\''") or '你好'}'"
            )
            res = await self.run_shell(command, timeout=timeout)
            return res.exit_code == 0

        if url:
            data = json_encode({"url": url, "type": 1})
            command = f"ubus call mediaplayer player_play_url '{data}'"
        else:
            data = json_encode({"text": text or "你好", "save": 0})
            command = f"ubus call mibrain text_to_speech '{data}'"

        res = await self.run_shell(command, timeout=timeout)
        return '"code": 0' in res.stdout if res else False

    async def play_verified_text(
        self,
        text: str,
        *,
        timeout: int = 10 * 60 * 1000,
        attempts: int = 2,
    ) -> bool:
        """播放长原生 TTS，并检查完整性及执行有限重试。

        该方法按需调用，不改变小爱、OpenAI、OpenClaw 和 QwenPaw 已有
        ``play`` 路径的行为。
        """

        normalized = self._normalize_native_tts_text(text or "你好")
        chunks = self._split_native_tts_text(normalized)
        if len(chunks) > 1:
            logger.info(
                f"[Speaker] Verified native TTS split into "
                f"{len(chunks)} chunks"
            )

        max_attempts = max(1, int(attempts))
        for index, chunk in enumerate(chunks, start=1):
            escaped_text = chunk.replace("'", "'\\''")
            command = f"/usr/sbin/tts_play.sh '{escaped_text}'"
            completed = False
            for attempt in range(1, max_attempts + 1):
                started_at = time.monotonic()
                result = await self.run_shell(command, timeout=timeout)
                elapsed = time.monotonic() - started_at
                if self._native_tts_completed(result):
                    completed = True
                    if attempt > 1:
                        logger.info(
                            "[Speaker] Verified native TTS retry completed "
                            f"(part {index}/{len(chunks)})"
                        )
                    break

                logger.warning(
                    "[Speaker] Verified native TTS did not complete "
                    f"(part {index}/{len(chunks)}, "
                    f"attempt {attempt}/{max_attempts}): "
                    f"exit_code={result.exit_code}, "
                    f"elapsed={elapsed:.1f}s, "
                    f"stdout={self._diagnostic_output(result.stdout)!r}, "
                    f"stderr={self._diagnostic_output(result.stderr)!r}"
                )
                if attempt < max_attempts:
                    await asyncio.sleep(0.5)
            if not completed:
                return False
        return True

    @classmethod
    def _native_tts_completed(cls, result: CommandResult) -> bool:
        """检查播放是否完整，同时兼容不输出完成标记的旧脚本。"""

        if result.exit_code != 0:
            return False
        if (
            cls._NATIVE_TTS_SCRIPT_MARKER in result.stdout
            and cls._NATIVE_TTS_COMPLETION_MARKER not in result.stdout
        ):
            return False
        return True

    @classmethod
    def _diagnostic_output(cls, output: str | None) -> str:
        """脱敏朗读文本，并限制远程命令诊断信息长度。"""

        redacted_lines = []
        for line in (output or "").splitlines():
            if any(
                marker in line
                for marker in cls._TTS_TEXT_LOG_MARKERS
            ):
                timestamp = line.split(" - ", 1)[0]
                redacted_lines.append(
                    f"{timestamp} - [TTS text omitted]"
                )
            else:
                redacted_lines.append(line)
        compact = " ".join("\n".join(redacted_lines).split())
        if len(compact) <= cls._TTS_DIAGNOSTIC_LIMIT:
            return compact
        return compact[:cls._TTS_DIAGNOSTIC_LIMIT] + "…"

    @staticmethod
    def _normalize_native_tts_text(text: str) -> str:
        """替换可能破坏设备 TTS 脚本的字符。"""

        replacements = {
            '"': "“",
            "\\": "／",
            "\r": " ",
            "\n": " ",
            "\t": " ",
        }
        normalized = "".join(
            replacements.get(char, char)
            for char in text
            if ord(char) >= 32 or char in "\r\n\t"
        )
        return " ".join(normalized.split())

    @classmethod
    def _split_native_tts_text(cls, text: str) -> list[str]:
        """尽量沿自然标点拆分长语音文本。"""

        remaining = text.strip()
        chunks: list[str] = []
        major_punctuation = "。！？!?；;"
        minor_punctuation = "，,、：: "

        while len(remaining) > cls._NATIVE_TTS_MAX_CHARS:
            window = remaining[:cls._NATIVE_TTS_MAX_CHARS]
            cut = max(
                window.rfind(char)
                for char in major_punctuation
            )
            if cut < cls._NATIVE_TTS_MAX_CHARS // 2:
                cut = max(
                    window.rfind(char)
                    for char in minor_punctuation
                )
            if cut < cls._NATIVE_TTS_MAX_CHARS // 2:
                cut = cls._NATIVE_TTS_MAX_CHARS
            else:
                cut += 1

            chunk = remaining[:cut].strip()
            if chunk:
                chunks.append(chunk)
            remaining = remaining[cut:].strip()

        if remaining:
            chunks.append(remaining)
        return chunks or ["你好"]

    async def play_server_file(
        self,
        file_path: str,
        blocking: bool = True,
        sample_rate: int = 24000,
    ) -> bool:
        """播放服务端本地音频文件（解码为 PCM 后推流到音箱）"""
        if not file_path:
            raise ValueError("file_path is required")

        if not os.path.isfile(file_path):
            raise FileNotFoundError(file_path)

        logger.info(
            f"[Speaker] Playing local file via Rust audio pipeline: {file_path}, "
            f"sample_rate={sample_rate}"
        )

        if blocking:
            await open_xiaoai_server.play_audio_file(file_path, sample_rate=sample_rate)
            return True

        asyncio.create_task(
            open_xiaoai_server.play_audio_file(file_path, sample_rate=sample_rate)
        )
        return True

    async def stop_device_audio(self) -> None:
        """
        停止设备上的全部播放链路。
        aplay 不立即重启，由 Rust 侧 ensure_player_ready() 在首次
        发送音频数据时按需启动，避免空 buffer 导致 underrun。
        """
        await self.run_shell(
            "killall tts_play.sh miplayer 2>/dev/null; mphelper pause"
        )
        await open_xiaoai_server.stop_playing()

    async def wake_up(self, awake=True, silent=True):
        """
        （取消）唤醒小爱

        参数:
            awake: 是否唤醒
            silent: 是否静默唤醒
        """

        if awake:
            if silent:
                command = 'ubus call pnshelper event_notify \'{"src":1,"event":0}\''
            else:
                command = 'ubus call pnshelper event_notify \'{"src":0,"event":0}\''
        else:
            command = """
                ubus call pnshelper event_notify '{"src":3, "event":7}'
                sleep 0.1
                ubus call pnshelper event_notify '{"src":3, "event":8}'
            """
        res = await self.run_shell(command)
        return '"code": 0' in res.stdout

    async def ask_xiaoai(self, text: str, silent=False):
        """
        把文字指令交给原来的小爱执行

        参数:
            text: 文字指令
            silent: 是否静默执行
        """

        data = {"nlp": 1, "nlp_text": text}
        if not silent:
            data["tts"] = 1

        command = f"ubus call mibrain ai_service '{json_encode(data)}'"
        res = await self.run_shell(command)
        return '"code": 0' in res.stdout

    async def abort_xiaoai(self):
        """
        中断原来小爱的运行

        注意：重启需要大约 1-2s 的时间，在此期间无法使用小爱音箱自带的 TTS 服务
        """
        # Stop current audio playback first, then restart xiaoai voice service
        res = await self.run_shell("/etc/init.d/mico_aivs_lab restart >/dev/null 2>&1")
        return res.exit_code == 0

    async def get_boot(self):
        """获取启动分区"""
        res = await self.run_shell("echo $(fw_env -g boot_part)")
        return res.stdout.strip()

    async def set_boot(self, boot_part: Literal["boot0", "boot1"]):
        """设置启动分区"""
        command = f"fw_env -s boot_part {boot_part} >/dev/null 2>&1 && echo $(fw_env -g boot_part)"
        res = await self.run_shell(command)
        return boot_part in res.stdout

    async def get_device(self):
        """获取设备型号、序列号信息"""
        res = await self.run_shell("echo $(micocfg_model) $(micocfg_sn)")
        info = res.stdout.strip().split(" ")
        return {
            "model": info[0] if len(info) > 0 else "unknown",
            "sn": info[1] if len(info) > 1 else "unknown",
        }

    async def get_mic(self):
        """获取麦克风状态"""
        res = await self.run_shell("[ ! -f /tmp/mipns/mute ] && echo on || echo off")
        status = "off"
        if "on" in res.stdout:
            status = "on"
        return status

    async def set_mic(self, on=True):
        """打开/关闭麦克风"""
        if on:
            command = (
                'ubus -t1 -S call pnshelper event_notify \'{"src":3, "event":7}\' 2>&1'
            )
        else:
            command = (
                'ubus -t1 -S call pnshelper event_notify \'{"src":3, "event":8}\' 2>&1'
            )
        res = await self.run_shell(command)
        return '"code":0' in res.stdout

    async def run_shell(self, script: str, timeout=10000):
        """
        执行脚本

        参数:
            script: 脚本内容
            timeout: 超时时间（毫秒）
        """
        res = "unknown"
        try:
            res = await get_xiaoai().run_shell(script, timeout=timeout)
            data = json_decode(res)
            if data:
                return CommandResult(
                    data.get("stdout", ""),
                    data.get("stderr", ""),
                    data.get("exit_code", 0),
                )
        except Exception:
            return CommandResult("error", res, -1)
