"""可选音乐灯效；业务循环负责优先级，设备脚本负责独立期限和原厂让路。"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import shlex

from aiohttp import web

from core.ref import get_app, get_speaker
from core.services.music_spectrum import SpectrumLease, SpectrumRenderer
from core.services.native_visual_profiles import select_profile
from core.utils.logger import logger


class MusicVisualService:
    def __init__(self, visual):
        self.visual = visual
        self.task = None
        self.job = None
        self.lease = None
        self.holders = set()
        self.loop = None
        self.stopping = False
        self.quarantined = False

    async def audio(self, request):
        lease = self.lease
        if lease is None:
            raise web.HTTPNotFound()
        return await lease.audio(request)

    async def frames(self, request):
        lease = self.lease
        if lease is None:
            raise web.HTTPNotFound()
        return await lease.frames(request)

    async def start(self, settings):
        if not settings.get('music', {}).get('enabled', False) or self.task:
            return
        try:
            brightness = settings['music'].get('brightness', 50)
            SpectrumRenderer(brightness)
            self.visual.public_url(settings)
        except (ValueError, TypeError) as exc:
            logger.warning(f"音乐灯效配置无效：{exc}", module="Music Visual")
            return
        self.loop = asyncio.get_running_loop()
        self.stopping = False
        self.task = asyncio.create_task(self._run(dict(settings), brightness))

    def preempt(self):
        """可从原生回调线程调用；所有租约状态更改仍在业务循环执行。"""
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._revoke, True)

    def _revoke(self, preserve=False):
        if self.lease:
            self.lease.close('native_takeover' if preserve else 'speech', preserve)

    async def _settle(self):
        job = self.job
        if job is None:
            return
        try:
            result = await asyncio.wait_for(asyncio.shield(job), timeout=4)
            if result is None or 'music_result=' not in result.stdout:
                self.quarantined = True
        except asyncio.CancelledError:
            self.quarantined = True
            raise
        except Exception:
            # 取消 RPC 不能证明音箱上的进程已经退出，不启动另一份频谱任务。
            self.quarantined = True
        if self.quarantined:
            self.visual.quarantined = True
            logger.warning("设备清理未确认，停用新增灯效直到重启 Bridge；保留语音流程", module="Music Visual")

    @asynccontextmanager
    async def speech(self):
        """对话持有期间禁止重启音乐；嵌套会话各自释放自己的令牌。"""
        holder = object()
        self.holders.add(holder)
        try:
            self._revoke()
            await self._settle()
            yield
        finally:
            self.holders.discard(holder)

    def _busy(self):
        app = get_app()
        return (self.stopping or self.quarantined or self.visual.quarantined or self.holders
                or self.visual.session is not None
                or (app is not None and app.device_state != 'idle'))

    async def _run(self, settings, brightness):
        warned = False
        while not self.stopping and not self.quarantined:
            try:
                if self._busy() or get_speaker() is None:
                    await asyncio.sleep(1)
                    continue
                speaker = get_speaker()
                identity = await speaker.run_shell("printf '%s\\n' \"$(micocfg_model)\"; cat /etc/banner", timeout=4000)
                if not identity or identity.exit_code:
                    raise RuntimeError('device unavailable')
                profile = select_profile(identity.stdout, settings.get('profile', 'auto'))
                if not profile.music_script:
                    raise ValueError('selected profile has no music visual capability')
                script = Path(__file__).with_name(profile.music_script).read_text(encoding='utf-8')
                command = 'sh -c ' + shlex.quote(script) + ' sh '
                check = await speaker.run_shell(command + 'check', timeout=6000)
                if not check or check.exit_code:
                    await asyncio.sleep(3)
                    continue
                if self._busy():
                    continue
                base = await self.visual.ensure_server(settings)
                if self._busy():
                    continue
                lease = SpectrumLease(brightness)
                self.lease = lease
                args = ['live', base, lease.token, str(lease.seconds)]
                self.job = asyncio.create_task(speaker.run_shell(
                    command + ' '.join(map(shlex.quote, args)), timeout=(lease.seconds + 8) * 1000))
                warned = False
                try:
                    result = await asyncio.shield(self.job)
                    if result is None or 'music_result=' not in result.stdout:
                        self.quarantined = True
                        self.visual.quarantined = True
                        logger.warning("音乐灯效设备任务结果不明，停止重试直到重启 Bridge", module="Music Visual")
                    else:
                        logger.debug(result.stdout.strip(), module="Music Visual")
                finally:
                    lease.close('job_ended')
                    if self.job.done():
                        self.job = None
                    self.lease = None
                await asyncio.sleep(.1 if result is not None and result.exit_code == 0 else 2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not warned:
                    logger.warning(f"音乐灯效暂不可用：{type(exc).__name__}: {exc}", module="Music Visual")
                    warned = True
                await asyncio.sleep(15)

    async def shutdown(self):
        self.stopping = True
        self._revoke()
        await self._settle()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.job:
            self.job.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
