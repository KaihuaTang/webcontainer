"""Hub：网关运行时状态的编排中心。

持有项目注册表快照、进程 Supervisor 和共享的上游 HTTP 会话；
对外提供节流的「重扫描 + 进程对账」，实现新项目热接入：
把新项目放进 container/ 后，刷新一次门户页即可生效，无需重启网关。
"""

import asyncio
import json
import logging
import time

import aiohttp

from . import config, registry
from .registry import KIND_LINK, KIND_STATIC, Project
from .supervisor import Supervisor

log = logging.getLogger("gateway.hub")


class Hub:
    def __init__(self):
        self.projects: dict[str, Project] = {}
        self.supervisor = Supervisor()
        self.session: aiohttp.ClientSession | None = None
        self._last_scan = 0.0
        self._scan_lock = asyncio.Lock()
        self._site_cache: tuple[float, dict] | None = None

    # ---- 生命周期 -------------------------------------------------------

    async def start(self) -> None:
        # 上游会话：不解压缩、不保存 Cookie（透明转发），连接数不设上限
        self.session = aiohttp.ClientSession(
            auto_decompress=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            connector=aiohttp.TCPConnector(limit=0),
        )
        await self.refresh(force=True)
        log.info("已加载 %d 个项目：%s", len(self.projects), ", ".join(self.projects) or "（空）")

    async def close(self) -> None:
        await self.supervisor.shutdown()
        if self.session is not None:
            await self.session.close()

    # ---- 扫描与对账 -----------------------------------------------------

    async def refresh(self, force: bool = False) -> None:
        """重扫描 container/ 并同步进程；带节流避免高频请求反复扫盘。"""
        async with self._scan_lock:
            now = time.monotonic()
            if not force and now - self._last_scan < config.SCAN_INTERVAL:
                return
            self._last_scan = now
            self.projects = registry.scan()
            await self.supervisor.reconcile(self.projects)

    # ---- 查询 -----------------------------------------------------------

    def get_project(self, project_id: str) -> Project | None:
        return self.projects.get(project_id)

    def project_status(self, project: Project) -> tuple[str, str | None]:
        """返回 (status, error)。status ∈ static/running/starting/stopped/error。"""
        if project.error is not None:
            return "error", project.error
        if project.runtime.kind == KIND_LINK:
            return "link", None
        if project.runtime.kind == KIND_STATIC:
            return "static", None
        app = self.supervisor.get(project.id)
        if app is None:
            return "stopped", None
        return app.state, app.error

    def portal_payload(self) -> list[dict]:
        items = []
        for project in self.projects.values():
            if project.hidden:
                continue
            status, error = self.project_status(project)
            items.append({
                "id": project.id,
                "name": project.name,
                "description": project.description,
                "type": project.type,
                "author": project.author,
                "tags": project.tags,
                "url": project.url,
                "icon": f"/api/projects/{project.id}/icon" if project.icon else None,
                "status": status,
                "error": error,
                "order": project.order,
            })
        items.sort(key=lambda item: (item["order"], item["name"].lower()))
        return items

    def site_config(self) -> dict:
        """site.config.json 与默认值合并，带 mtime 缓存。"""
        path = config.SITE_CONFIG_PATH
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return dict(config.SITE_DEFAULTS)

        if self._site_cache is not None and self._site_cache[0] == mtime:
            return self._site_cache[1]

        merged = dict(config.SITE_DEFAULTS)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                merged.update({k: v for k, v in raw.items() if isinstance(v, str)})
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("site.config.json 读取失败，使用默认文案：%s", exc)
        self._site_cache = (mtime, merged)
        return merged
