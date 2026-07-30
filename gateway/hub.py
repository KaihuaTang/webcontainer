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
from .visits import PORTAL_KEY, VisitCounter

log = logging.getLogger("gateway.hub")


class Hub:
    def __init__(self):
        self.projects: dict[str, Project] = {}
        self.supervisor = Supervisor()
        self.visits = VisitCounter()
        self.session: aiohttp.ClientSession | None = None
        self._last_scan = 0.0
        self._scan_lock = asyncio.Lock()
        self._site_cache: tuple[float, dict] | None = None
        self._pinned_cache: tuple[float, list[str]] | None = None

    # ---- 生命周期 -------------------------------------------------------

    async def start(self) -> None:
        # 上游会话：不解压缩、不保存 Cookie（透明转发），连接数不设上限
        self.session = aiohttp.ClientSession(
            auto_decompress=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            connector=aiohttp.TCPConnector(limit=0),
        )
        await self.visits.start()
        await self.refresh(force=True)
        log.info("已加载 %d 个项目：%s", len(self.projects), ", ".join(self.projects) or "（空）")

    async def close(self) -> None:
        await self.visits.close()
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

    def pinned_ids(self) -> list[str]:
        """container/pinned.json 里的置顶项目 id，按文件中的先后顺序；带 mtime 缓存。

        接受 {"pinned": ["id", …]} 或裸数组 ["id", …]；文件缺失、格式错误、
        写了不存在的 id 都只记一条日志并忽略，不影响门户可用。
        """
        path = config.CONTAINER_DIR / config.PINNED_NAME
        try:
            mtime = path.stat().st_mtime
        except OSError:
            self._pinned_cache = None
            return []

        if self._pinned_cache is not None and self._pinned_cache[0] == mtime:
            return self._pinned_cache[1]

        ids: list[str] = []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = raw.get("pinned", []) if isinstance(raw, dict) else raw
            if not isinstance(entries, list):
                raise ValueError("pinned 必须是由项目 id 组成的数组")
            for entry in entries:
                pid = str(entry).strip() if isinstance(entry, str) else ""
                if pid and pid not in ids:
                    ids.append(pid)
        except (OSError, ValueError) as exc:  # JSONDecodeError 是 ValueError 的子类
            log.warning("%s 读取失败，本次忽略置顶设置：%s", config.PINNED_NAME, exc)
            ids = []

        unknown = [pid for pid in ids if pid not in self.projects]
        if unknown:
            log.warning("%s 里有未知项目 id（已忽略）：%s", config.PINNED_NAME, ", ".join(unknown))

        self._pinned_cache = (mtime, ids)
        return ids

    def portal_payload(self) -> list[dict]:
        # 置顶项按 pinned.json 里的书写顺序排在最前；其余仍按 order + 名称
        rank = {pid: idx for idx, pid in enumerate(self.pinned_ids())}
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
                "pinned": project.id in rank,
                "visits": self.visits.get(project.id),
            })
        items.sort(key=lambda item: (
            rank.get(item["id"], len(rank)),
            item["order"],
            item["name"].lower(),
        ))
        return items

    def portal_visits(self) -> int:
        """门户首页自身的累计访问次数。"""
        return self.visits.get(PORTAL_KEY)

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
