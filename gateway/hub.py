"""Hub：网关运行时状态的编排中心。

持有项目注册表快照、进程 Supervisor 和共享的上游 HTTP 会话；
对外提供节流的「重扫描 + 进程对账」，实现新项目热接入：
把新项目放进 container/ 后，刷新一次门户页即可生效，无需重启网关。
"""

import asyncio
import json
import logging
import random
import time
from datetime import date, datetime

import aiohttp

from . import config, registry
from .registry import KIND_LINK, KIND_STATIC, Project
from .supervisor import Supervisor
from .visits import PORTAL_KEY, VisitCounter

log = logging.getLogger("gateway.hub")


def _iso(timestamp: float) -> str | None:
    """epoch 秒 → 本机时区的 ISO 8601 字符串，供 /api/projects 排查排序用。"""
    if not timestamp:
        return None
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


class Hub:
    def __init__(self):
        self.projects: dict[str, Project] = {}
        self.supervisor = Supervisor()
        self.visits = VisitCounter()
        self.session: aiohttp.ClientSession | None = None
        self._last_scan = 0.0
        self._scan_lock = asyncio.Lock()
        self._site_cache: tuple[float, dict] | None = None
        self._pinned_cache: tuple[float, list[str], bool] | None = None

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

    def _pinned_config(self) -> tuple[list[str], bool]:
        """读 container/pinned.json，返回 (置顶 id 列表, 是否开启每日惊喜)；带 mtime 缓存。

        接受 {"pinned": ["id", …], "dailySurprise": true} 或裸数组 ["id", …]；
        文件缺失、格式错误、写了不存在的 id 都只记一条日志并忽略，不影响门户可用。
        每日惊喜缺省开启（缺文件、坏 JSON 时同样按开启处理）。
        """
        path = config.CONTAINER_DIR / config.PINNED_NAME
        try:
            mtime = path.stat().st_mtime
        except OSError:
            self._pinned_cache = None
            return [], True

        if self._pinned_cache is not None and self._pinned_cache[0] == mtime:
            return self._pinned_cache[1], self._pinned_cache[2]

        ids: list[str] = []
        surprise = True
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = raw.get("pinned", []) if isinstance(raw, dict) else raw
            if not isinstance(entries, list):
                raise ValueError("pinned 必须是由项目 id 组成的数组")
            for entry in entries:
                pid = str(entry).strip() if isinstance(entry, str) else ""
                if pid and pid not in ids:
                    ids.append(pid)
            if isinstance(raw, dict) and "dailySurprise" in raw:
                surprise = bool(raw.get("dailySurprise"))
        except (OSError, ValueError) as exc:  # JSONDecodeError 是 ValueError 的子类
            log.warning("%s 读取失败，本次忽略置顶设置：%s", config.PINNED_NAME, exc)
            ids = []
            surprise = True

        unknown = [pid for pid in ids if pid not in self.projects]
        if unknown:
            log.warning("%s 里有未知项目 id（已忽略）：%s", config.PINNED_NAME, ", ".join(unknown))

        self._pinned_cache = (mtime, ids, surprise)
        return ids, surprise

    def pinned_ids(self) -> list[str]:
        """container/pinned.json 里的置顶项目 id，按文件中的先后顺序。"""
        return self._pinned_config()[0]

    @staticmethod
    def daily_surprise_id(candidates: list[str], today: date | None = None) -> str | None:
        """每日惊喜：每天从候选里定选一个，排在所有置顶之前。

        不是每天独立掷一次骰子——那样约每 N 天就会连着两天挑中同一个，
        「惊喜」当场失效。做法是把候选整体洗牌成一轮：一轮 N 天（N=候选数），
        轮内每个项目恰好轮到一次；换轮时若新一轮的头与上一轮的尾撞车，就与轮内
        第二个对调。于是「不重样」和「人人都能轮到」是保证的，而不是碰运气。

        全程由日期推导，不落盘、不用定时任务：同一天内所有访客、每次刷新看到的
        都是同一个，跨零点自动换人。候选集变了（新接入或下线了项目）整轮排布会
        跟着重算，当天的结果可能变，这是可以接受的。
        """
        pool = sorted(candidates)
        if not pool:
            return None
        if len(pool) == 1:
            return pool[0]

        day = (today or date.today()).toordinal()
        if len(pool) == 2:
            return pool[day % 2]  # 两个候选时严格交替，下面的对调逻辑不适用

        def shuffled(cycle_index: int) -> list[str]:
            order = list(pool)
            random.Random(f"{cycle_index}|" + "|".join(pool)).shuffle(order)
            return order

        cycle, position = divmod(day, len(pool))
        current = shuffled(cycle)
        # 与上一轮的末位比：注意上一轮自己的对调只动前两位，不影响末位，可直接取
        if current[0] == shuffled(cycle - 1)[-1]:
            current[0], current[1] = current[1], current[0]
        return current[position]

    def portal_payload(self) -> list[dict]:
        # 排序：每日惊喜 → pinned.json 里的置顶（按书写顺序）→ 其余按上架时间倒序
        pinned, surprise_on = self._pinned_config()
        rank = {pid: idx for idx, pid in enumerate(pinned)}
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
                # 卡片一律指向门户内的 /apps/<id>/：站外项目由网关 302 转出去，
                # 这样「从门户点进去」这一次访问才统计得到（否则浏览器直接跳走）
                "url": f"{project.prefix}/",
                "externalUrl": project.runtime.url if project.runtime.kind == KIND_LINK else None,
                "icon": f"/api/projects/{project.id}/icon" if project.icon else None,
                "status": status,
                "error": error,
                "order": project.order,
                "addedAt": _iso(project.added_at),
                "pinned": project.id in rank,
                "surprise": False,
                "visits": self.visits.get(project.id),
            })

        # 惊喜只从「没被置顶、配置也没出错」的项目里挑：坏卡片不该被推到最前
        surprise_id = None
        if surprise_on:
            surprise_id = self.daily_surprise_id(
                [item["id"] for item in items if not item["pinned"] and not item["error"]]
            )
        for item in items:
            item["surprise"] = item["id"] == surprise_id

        added = {pid: project.added_at for pid, project in self.projects.items()}
        items.sort(key=lambda item: (
            -1 if item["surprise"] else rank.get(item["id"], len(rank)),
            -added.get(item["id"], 0.0),   # 上架时间倒序：越新越靠前
            item["order"],                 # 同一时间内由 order 决定
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
