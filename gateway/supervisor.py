"""子进程托管：为 runtime.kind=proxy 的项目启动、看护、重启后端进程。

- 每个项目一个进程组（start_new_session），停止时整组发信号，避免残留孙进程；
- stdout/stderr 追加写入 logs/<id>.log；
- 进程意外退出后按指数退避自动重启（运行满 60 秒后重置退避计数）；
- 清单中 command 的第一个词若为 python/python3，会替换为运行网关的解释器
  （即项目 venv），保证 Python 子项目与网关使用同一套依赖环境。
"""

import asyncio
import contextlib
import logging
import os
import signal
import socket
import sys
import time
from datetime import datetime

from . import config
from .registry import KIND_PROXY, Project

log = logging.getLogger("gateway.supervisor")

STATE_STOPPED = "stopped"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_ERROR = "error"

_STOP_GRACE_SECONDS = 8.0


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class AppProcess:
    """单个被托管项目的运行状态。"""

    def __init__(self, project: Project):
        self.project = project
        self.state = STATE_STOPPED
        self.error: str | None = None
        self.port: int | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.restarts = 0
        self.started_at = 0.0
        self._log_file = None
        self._watcher: asyncio.Task | None = None
        self._stopping = False
        self.log_path = config.LOGS_DIR / f"{project.id}.log"

    # ---- 生命周期 -------------------------------------------------------

    async def start(self) -> None:
        rt = self.project.runtime
        self._stopping = False
        self.state = STATE_STARTING
        self.error = None

        port = rt.port or _find_free_port()
        if rt.port and _port_open(rt.port):
            self.state = STATE_ERROR
            self.error = f"内部端口 {rt.port} 已被其他进程占用，无法启动"
            log.error("[%s] %s", self.project.id, self.error)
            return
        self.port = port

        argv = list(rt.command)
        if argv[0] in ("python", "python3"):
            argv[0] = sys.executable

        env = {
            **os.environ,
            **rt.env,
            "PORT": str(port),
            "WC_APP_ID": self.project.id,
            "WC_APP_PREFIX": self.project.prefix,
        }
        cwd = (self.project.dir / rt.cwd).resolve()

        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self._log_file = open(self.log_path, "a", buffering=1, encoding="utf-8", errors="replace")
        self._log_file.write(
            f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 启动 {' '.join(argv)} "
            f"(cwd={cwd}, PORT={port}) =====\n"
        )
        self._log_file.flush()

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=self._log_file,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            self.state = STATE_ERROR
            self.error = f"进程启动失败：{exc}"
            log.error("[%s] %s", self.project.id, self.error)
            self._close_log()
            return

        log.info("[%s] 已启动 pid=%s，等待端口 %s 就绪…", self.project.id, self.proc.pid, port)
        ready = await self._wait_ready(rt.startup_timeout)
        if not ready:
            if self.proc.returncode is not None:
                self.error = (
                    f"进程启动后立即退出（exit={self.proc.returncode}），"
                    f"详见 logs/{self.project.id}.log"
                )
            else:
                self.error = f"等待端口 {port} 就绪超时（{rt.startup_timeout:.0f}s）"
            self.state = STATE_ERROR
            log.error("[%s] %s", self.project.id, self.error)
        else:
            self.state = STATE_RUNNING
            self.started_at = time.monotonic()
            log.info("[%s] 就绪：http://127.0.0.1:%s", self.project.id, port)

        self._watcher = asyncio.create_task(self._watch(), name=f"watch:{self.project.id}")

    async def _wait_ready(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc is not None and self.proc.returncode is not None:
                return False
            if self.port is not None and _port_open(self.port):
                return True
            await asyncio.sleep(0.25)
        return False

    async def _watch(self) -> None:
        assert self.proc is not None
        rc = await self.proc.wait()
        self._close_log()
        if self._stopping:
            self.state = STATE_STOPPED
            return

        ran_seconds = time.monotonic() - self.started_at if self.started_at else 0.0
        if ran_seconds > 60:
            self.restarts = 0
        delay = min(30, 2 ** min(self.restarts, 5))
        self.restarts += 1
        self.state = STATE_ERROR
        self.error = f"进程意外退出（exit={rc}），{delay}s 后第 {self.restarts} 次重启"
        log.warning("[%s] %s", self.project.id, self.error)

        await asyncio.sleep(delay)
        if not self._stopping:
            await self.start()

    async def stop(self) -> None:
        self._stopping = True
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

        proc = self.proc
        if proc is not None and proc.returncode is None:
            log.info("[%s] 停止 pid=%s", self.project.id, proc.pid)
            self._signal_group(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_SECONDS)
            except asyncio.TimeoutError:
                log.warning("[%s] SIGTERM 超时，强制 SIGKILL", self.project.id)
                self._signal_group(proc, signal.SIGKILL)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=5)
        self._close_log()
        self.proc = None
        self.state = STATE_STOPPED
        self.error = None

    @staticmethod
    def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), sig)

    def _close_log(self) -> None:
        if self._log_file is not None:
            with contextlib.suppress(OSError):
                self._log_file.close()
            self._log_file = None


class Supervisor:
    """管理全部 proxy 类项目的进程集合，并按清单变化增删/重启。"""

    def __init__(self):
        self.apps: dict[str, AppProcess] = {}

    def get(self, project_id: str) -> AppProcess | None:
        return self.apps.get(project_id)

    async def reconcile(self, projects: dict[str, Project]) -> None:
        """让运行中的进程集合与最新扫描结果保持一致。"""
        wanted = {
            pid: p for pid, p in projects.items()
            if p.error is None and p.runtime.kind == KIND_PROXY
        }

        # 项目被移除 → 停进程
        for pid in list(self.apps):
            if pid not in wanted:
                log.info("[%s] 项目已从 container 移除，停止进程", pid)
                await self.apps.pop(pid).stop()

        starts: list[asyncio.Task] = []
        for pid, project in wanted.items():
            app = self.apps.get(pid)
            if app is None:
                app = AppProcess(project)
                self.apps[pid] = app
                if project.runtime.auto_start:
                    starts.append(asyncio.create_task(app.start()))
            elif project.manifest_mtime != app.project.manifest_mtime:
                log.info("[%s] project.json 有更新，重启进程", pid)
                await app.stop()
                app.project = project
                starts.append(asyncio.create_task(app.start()))
            else:
                app.project = project  # 同步描述性字段
        if starts:
            await asyncio.gather(*starts)

    async def ensure_started(self, project_id: str) -> AppProcess | None:
        """首个请求到达时按需启动（autoStart=false 或此前启动失败的项目）。"""
        app = self.apps.get(project_id)
        if app is None:
            return None
        if app.state in (STATE_STOPPED, STATE_ERROR) and (
            app.proc is None or app.proc.returncode is not None
        ):
            if app.state == STATE_ERROR and app._watcher is not None:
                return app  # 看护任务正在退避重启中，不重复拉起
            await app.start()
        return app

    async def shutdown(self) -> None:
        if self.apps:
            log.info("停止全部子项目进程…")
            await asyncio.gather(*(app.stop() for app in self.apps.values()))
