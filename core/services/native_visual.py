"""共享对话阶段接口；OH2P 的数字事件和原厂命令只存在于此适配层。

默认关闭。当前固件没有独占租约/带请求归属的播放完成事件，因此属于显式启用的
实验功能，不能将状态轮询当作无竞态的原厂资源锁。
"""

import asyncio
import json
import math
import re
import shlex
import threading
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from core.services.visual_audio import visual_audio
from core.services.music_visual import MusicVisualService
from core.services.native_visual_profiles import OH2P, select_profile
from core.utils.logger import logger


class NativeVisualUnavailable(RuntimeError):
    """设备忙、能力不匹配或会话结果不明确，不能继续操作原厂状态。"""


def ubus_command(service, method, payload):
    return f"ubus -t 2 call {service} {method} " + shlex.quote(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def parse_reply(result):
    if result is None or result.exit_code != 0:
        raise NativeVisualUnavailable("device command failed")
    try:
        reply = json.loads(result.stdout)
        if (not isinstance(reply, dict) or type(reply.get("code")) is not int
                or reply["code"] != 0):
            raise ValueError()
        return reply
    except (ValueError, TypeError):
        raise NativeVisualUnavailable("invalid device reply") from None


class NativeVisualService:
    """在 MainApp.loop 上管理单设备阶段，音频线程仅写入有限 PCM 槽。"""

    def __init__(self, relay=visual_audio):
        self.relay = relay
        self.runner = None
        self.session = None
        self.lock = asyncio.Lock()
        self.quarantined = False
        self.http_lock = asyncio.Lock()
        self.music = MusicVisualService(self)

    async def open(self, speaker, settings):
        if not settings.get("enabled", False):
            return None
        async with self.lock:
            if self.quarantined or self.session is not None:
                raise NativeVisualUnavailable("visual service still occupied")
            # auto 只启用已实测组合；实验 profile 仍须匹配真实设备身份。
            result = await speaker.run_shell(
                "printf '%s\\n' \"$(micocfg_model)\"; "
                "cat /etc/banner",
                timeout=4000,
            )
            if not result or result.exit_code:
                raise NativeVisualUnavailable("unsupported native visual capability")
            try:
                profile = select_profile(result.stdout, settings.get('profile', 'auto'))
            except ValueError as exc:
                raise NativeVisualUnavailable(str(exc)) from None
            prerequisites = "command -v curl >/dev/null && "
            prerequisites += ("test -p /tmp/mic_audio.fifo" if profile.microphone_relay
                              else "test -x /bin/ledserver && pidof mipns-xiaomi >/dev/null")
            check = await speaker.run_shell(prerequisites, timeout=3000)
            if not check or check.exit_code:
                raise NativeVisualUnavailable("missing native visual prerequisites")
            base = await self.ensure_server(settings)
            session = NativeVisualSession(self, speaker, base, settings.get("listening_gain", 0.25), profile)
            self.session = session
            if profile.experimental:
                logger.warning("LX06 灯效为固件静态分析原型，尚未实机验证；不支持麦克风幅度回送", module="Native Visual")
            return session

    @staticmethod
    def public_url(settings):
        base = str(settings.get("public_url", "")).rstrip("/")
        parsed = urlsplit(base)
        if (parsed.scheme != "http" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("set a reachable HTTP public_url without path")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("invalid public_url port")
        return base

    async def ensure_server(self, settings):
        try:
            base = self.public_url(settings)
        except ValueError as exc:
            raise NativeVisualUnavailable(str(exc)) from None
        async with self.http_lock:
            if self.runner is None:
                app = web.Application()
                app.router.add_get("/native-visual/{token}", self.relay.handle_stream, allow_head=False)
                app.router.add_put("/music-visual/audio/{token}", self.music.audio)
                app.router.add_get("/music-visual/frames/{token}", self.music.frames, allow_head=False)
                runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.5)
                await runner.setup()
                try:
                    await web.TCPSite(runner, str(settings.get("bind_host", "0.0.0.0")),
                                      int(settings.get("port", 9093))).start()
                except BaseException:
                    await runner.cleanup()
                    raise
                self.runner = runner
        return base

    def preempt(self):
        """原厂唤醒回调可跨线程撤销供数；不从回调线程发送设备命令。"""
        self.music.preempt()
        session = self.session
        if session is not None:
            session.preempted.set()
            lease = session.lease
            if lease is not None:
                lease.preserve_on_close = True
                self.relay.close(lease)

    async def shutdown(self):
        await self.music.shutdown()
        if self.session:
            await self.session.close()
        self.relay.close_all()
        if self.runner:
            await self.runner.cleanup()
            self.runner = None


class NativeVisualSession:
    def __init__(self, service, speaker, base, listening_gain=0.25, profile=OH2P):
        self.service, self.speaker, self.base = service, speaker, base
        self.profile = profile
        self.listening_gain = float(listening_gain)
        if not math.isfinite(self.listening_gain) or not 0.05 <= self.listening_gain <= 1.0:
            raise NativeVisualUnavailable("listening_gain must be between 0.05 and 1.0")
        self.preempted = threading.Event()
        self.lease = None
        self.task = None
        self.closed = False

    def _check(self):
        if self.closed or self.preempted.is_set():
            raise NativeVisualUnavailable("native visual session interrupted")

    async def phase(self, name, seconds=30):
        self._check()
        await self.clear()
        self._check()
        if name not in ("listening", "thinking") or not math.isfinite(seconds):
            raise ValueError("invalid conversation phase")
        seconds = min(60, max(1, math.ceil(seconds)))
        lease = self.service.relay.begin(seconds, heartbeat=name == "thinking" or not self.profile.microphone_relay,
                                        gain=self.listening_gain)
        lease.control = not self.profile.microphone_relay
        self.lease = lease
        if self.preempted.is_set():
            lease.preserve_on_close = True
            self.service.relay.close(lease)
            self._check()
        script = Path(__file__).with_name(self.profile.phase_script).read_text(encoding="utf-8")
        url = self.base + "/native-visual/" + lease.token
        command = "busybox timeout -t " + str(seconds + 5) + " sh -c " + shlex.quote(script)
        command += " sh " + " ".join(map(shlex.quote, [name, url, str(seconds)]))
        self.task = asyncio.create_task(self.speaker.run_shell(command, timeout=(seconds + 8) * 1000))
        try:
            async with asyncio.timeout(4):
                while not lease.claimed:
                    self._check()
                    if self.task.done() or lease.closed:
                        raise NativeVisualUnavailable("device did not accept visual phase")
                    await asyncio.sleep(0.02)
        except BaseException:
            await self.clear()
            raise

    async def clear(self):
        """先撤销供数，再等待设备自己清理；旧 RPC 未结束前禁止进入下一阶段。"""
        lease, task = self.lease, self.task
        self.lease = self.task = None
        if lease:
            if self.preempted.is_set():
                lease.preserve_on_close = True
            self.service.relay.close(lease)
        if task:
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=4)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # RPC 取消不等于远端子进程退出；隔离至应用重启，设备独立期限兜底。
                self.service.quarantined = True
                task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
                raise
            # 正常阶段期限结束已经由设备脚本完成清理，可继续进入回答阶段。
            expired = (result is not None and result.exit_code == 25
                       and "visual_result=25 native_takeover=0" in result.stdout)
            if result is None or (result.exit_code != 0 and not expired):
                self.preempted.set()
                details = re.search(r"visual_result=\d+ native_takeover=\d+", result.stdout) if result else None
                reason = re.search(r"visual_abort=[a-z_]+", result.stdout) if result else None
                raise NativeVisualUnavailable("device phase ended abnormally: " +
                                              (details.group() if details else "no completion marker") +
                                              (" " + reason.group() if reason else ""))

    async def _status(self):
        reply = parse_reply(await self.speaker.run_shell(
            ubus_command("mediaplayer", "player_get_play_status", {}), timeout=3000))
        info = reply.get("info")
        try:
            info = json.loads(info) if isinstance(info, str) else info
            value = info["status"]
            if type(value) is not int or value not in (0, 1, 2, 3):
                raise ValueError()
            return value
        except (TypeError, KeyError, ValueError):
            raise NativeVisualUnavailable("unknown native player state") from None

    async def speak(self, text, timeout=120):
        """只发起一次原厂 TTS；观察 busy→idle，不在结果不明时重播或全局停止。

        此状态是设备级观测，并非带请求归属的完成凭据；原厂抢占时直接退出会话。
        """
        await self.clear()
        self._check()
        # 2 为暂停，3 也可由原厂结束路径返回；仍需下方会话/灯效占用检查。
        if await self._status() not in (0, 2, 3):
            raise NativeVisualUnavailable("native player busy")
        guard = Path(__file__).with_name(self.profile.phase_script).read_text(encoding="utf-8")
        guard_result = await self.speaker.run_shell("sh -c " + shlex.quote(guard) + " sh check '' 1", timeout=6000)
        if not guard_result or guard_result.exit_code:
            raise NativeVisualUnavailable("native speech lifecycle occupied")
        request = ubus_command("mibrain", "text_to_speech", {"text": text, "save": 0, "play": 1})
        # 合成可能超过普通 UBus 两秒期限；调用后失败不做自动回退，以免重复播报。
        request = request.replace("ubus -t 2 ", "ubus -t 30 ", 1)
        parse_reply(await self.speaker.run_shell(request, timeout=32000))
        loop = asyncio.get_running_loop()
        started = loop.time()
        seen_playing = False
        idle_since = None
        while loop.time() - started < timeout:
            self._check()
            status = await self._status()
            if status == 1:
                seen_playing, idle_since = True, None
            elif status in (0, 3) and seen_playing:
                idle_since = loop.time() if idle_since is None else idle_since
                if loop.time() - idle_since >= 0.3:
                    return
            elif status == 2 and seen_playing:
                raise NativeVisualUnavailable("native playback paused or preempted")
            if not seen_playing and loop.time() - started > 8:
                raise NativeVisualUnavailable("native playback start not observed")
            await asyncio.sleep(0.1)
        raise NativeVisualUnavailable("native playback completion not observed")

    async def close(self):
        try:
            await self.clear()
        except Exception as exc:
            logger.warning(f"灯效会话清理结果不明确：{type(exc).__name__}", module="Native Visual")
        finally:
            self.closed = True
            if self.service.session is self:
                self.service.session = None


native_visual = NativeVisualService()
