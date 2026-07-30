"""访问统计：按「一次访问」为粒度累计门户首页与各子项目的浏览量。

统计口径（与常见站点分析工具一致，力求「一次访问只记一次」）：
- 只有**页面级**请求才计数：Sec-Fetch-Dest: document（现代浏览器都会带），
  拿不到该头时退化为 Accept 含 text/html；页面内的 JS/CSS/图片/接口请求一律不计；
- 同一访客对同一目标在**同一次会话**内只计一次：空闲超过 idle_ttl、或整场超过
  max_session 才算新的一次访问；会话期内的任何请求（含子资源、接口轮询）都会续期，
  因此长时间玩一个网页游戏、或开着门户不动，都只算一次；
- 访客身份取网关下发的 wc_vid Cookie（Path=/，对门户与所有 /apps/<id>/ 通用），
  因此从门户点进项目、或直接用 URL 打开项目，走的是同一套身份与去重；
- 明显的爬虫 UA 直接跳过，不计数也不建会话；出错的响应（>=400）不计数。

计数常驻内存，脏了就按 flush_interval 落盘到 data/visits.json（临时文件 + 原子替换），
进程重启后累计值不丢。会话表只在内存里，重启后所有人重新开始一次新会话。

已知取舍：清空 Cookie、无痕窗口、换浏览器都会被当作新访客；反之同一浏览器的多个
标签页共用一次会话。对项目展示站来说这个精度足够，也不需要额外的第三方统计服务。
"""

import asyncio
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aiohttp import web

from . import config

log = logging.getLogger("gateway.visits")

# 门户首页在计数表里的键；项目 id 不允许含 '@'，不会与之冲突
PORTAL_KEY = "@portal"

COOKIE_NAME = "wc_vid"
COOKIE_MAX_AGE = 400 * 24 * 3600  # 浏览器普遍把 Cookie 有效期上限压到 400 天

_REQ_NEW_COOKIE = "wc_new_visitor"  # request 上暂存「本次响应需要下发的访客 id」
_VID_RE = re.compile(r"^[0-9a-f]{32}$")
_BOT_RE = re.compile(r"(?i)bot|crawler|spider|slurp|facebookexternalhit|embedly|feedfetcher")

# 会话表的兜底上限：异常流量下先清理过期项，避免内存无限增长
_MAX_SESSIONS = 20000

_FILE_VERSION = 1


@dataclass
class _Session:
    started: float   # 会话开始（time.monotonic）
    last_seen: float  # 最近一次请求


class VisitCounter:
    """累计访问数 + 活跃会话表；负责去重、持久化。"""

    def __init__(self, path: Path | None = None, *,
                 idle_ttl: float | None = None,
                 max_session: float | None = None,
                 flush_interval: float | None = None):
        self.path = path or config.VISITS_PATH
        self.idle_ttl = config.VISIT_IDLE_TTL if idle_ttl is None else idle_ttl
        self.max_session = config.VISIT_MAX_SESSION if max_session is None else max_session
        self.flush_interval = (
            config.VISIT_FLUSH_INTERVAL if flush_interval is None else flush_interval
        )
        self._counts: dict[str, int] = {}
        self._sessions: dict[tuple[str, str], _Session] = {}
        self._dirty = False
        self._flusher: asyncio.Task | None = None

    # ---- 生命周期 -------------------------------------------------------

    async def start(self) -> None:
        self.load()
        if self.flush_interval > 0 and self._flusher is None:
            self._flusher = asyncio.create_task(self._flush_loop())

    async def close(self) -> None:
        if self._flusher is not None:
            self._flusher.cancel()
            try:
                await self._flusher
            except asyncio.CancelledError:
                pass
            self._flusher = None
        self.flush()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval)
            self.prune()
            self.flush()

    # ---- 查询 -----------------------------------------------------------

    def get(self, target: str) -> int:
        return self._counts.get(target, 0)

    def snapshot(self) -> dict[str, int]:
        return dict(self._counts)

    # ---- 记账 -----------------------------------------------------------

    def record(self, target: str, visitor: str, is_page: bool) -> bool:
        """登记一次请求；返回 True 表示开启了新的一次访问（计数 +1）。

        is_page=False 的子资源请求只为当前会话续期，永远不会自己开一次新访问——
        否则页面里几十个静态请求就会把计数刷上去。
        """
        now = time.monotonic()
        key = (visitor, target)
        session = self._sessions.get(key)

        if session is not None and not self._expired(session, now):
            session.last_seen = now
            return False

        if not is_page:
            return False

        if len(self._sessions) >= _MAX_SESSIONS:
            self.prune()
        self._sessions[key] = _Session(started=now, last_seen=now)
        self._counts[target] = self._counts.get(target, 0) + 1
        self._dirty = True
        return True

    def _expired(self, session: _Session, now: float) -> bool:
        return (now - session.last_seen > self.idle_ttl
                or now - session.started > self.max_session)

    def prune(self) -> None:
        now = time.monotonic()
        dead = [key for key, session in self._sessions.items() if self._expired(session, now)]
        for key in dead:
            del self._sessions[key]

    # ---- 持久化 ---------------------------------------------------------

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            log.info("访问统计从零开始（%s 尚不存在）", self.path)
            return
        except (OSError, json.JSONDecodeError) as exc:
            # 别让坏文件被下一次落盘直接覆盖掉，留个副本便于人工抢救
            log.warning("%s 读取失败，本次从零开始：%s", self.path, exc)
            try:
                self.path.replace(self.path.with_name(self.path.name + ".bad"))
            except OSError:
                pass
            return

        counts = raw.get("counts") if isinstance(raw, dict) else None
        if not isinstance(counts, dict):
            log.warning("%s 结构不符合预期（缺少 counts 对象），本次从零开始", self.path)
            return
        for key, value in counts.items():
            if isinstance(key, str) and isinstance(value, int) and value >= 0:
                self._counts[key] = value
        log.info("已载入访问统计：%d 个目标，门户 %d 次",
                 len(self._counts), self.get(PORTAL_KEY))

    def flush(self, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        payload = {
            "version": _FILE_VERSION,
            "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
            "counts": dict(sorted(self._counts.items())),
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            os.replace(tmp, self.path)  # 原子替换：读到的永远是完整文件
            self._dirty = False
        except OSError as exc:
            log.warning("访问统计落盘失败（计数仍在内存中）：%s", exc)


# ---- 请求侧：身份、目标、页面判定 ---------------------------------------

def _target_of(path: str) -> str | None:
    """请求归属的统计目标：项目 id、门户，或 None（不参与统计）。"""
    prefix = config.APPS_PREFIX + "/"
    if path.startswith(prefix):
        pid = path[len(prefix):].split("/", 1)[0]
        return pid or None
    return PORTAL_KEY  # 门户自身的页面、静态资源与接口


def _is_page_request(request: web.Request) -> bool:
    """是否是「打开一个页面」的请求（而非页面内的子资源）。"""
    if request.method != "GET":
        return False
    dest = request.headers.get("Sec-Fetch-Dest")
    if dest is not None:
        return dest == "document"
    return "text/html" in request.headers.get("Accept", "")


def apply_visitor_cookie(request: web.Request, response: web.StreamResponse) -> None:
    """给还没发出的响应补上访客 Cookie；已 prepare 的响应会被跳过。

    反代与 WebSocket 的响应是自己 prepare 的，由 proxy 在 prepare 之前主动调用，
    其余响应统一由中间件在拿到返回值后调用。
    """
    value = request.get(_REQ_NEW_COOKIE)
    if not value or response.prepared:
        return
    response.set_cookie(
        COOKIE_NAME, value,
        max_age=COOKIE_MAX_AGE, path="/", httponly=True, samesite="Lax",
    )


def _account(request: web.Request, status: int, visitor: str) -> None:
    hub = request.app.get("hub")
    counter: VisitCounter | None = getattr(hub, "visits", None)
    if counter is None:
        return

    target = _target_of(request.path)
    if target is None:
        return
    if target != PORTAL_KEY and hub.get_project(target) is None:
        return  # 未知项目 id：不建会话，免得被乱刷 URL 撑爆会话表
    if _BOT_RE.search(request.headers.get("User-Agent", "")):
        return

    is_page = _is_page_request(request) and status < 400
    if target == PORTAL_KEY and request.path != "/":
        is_page = False  # 门户只认首页本身，/assets、/api 等只续期
    counter.record(target, visitor, is_page)


@web.middleware
async def visit_middleware(request: web.Request, handler):
    """统一入口：识别访客 → 执行请求 → 按响应状态记账并下发 Cookie。"""
    visitor = request.cookies.get(COOKIE_NAME, "")
    if not _VID_RE.match(visitor):
        visitor = secrets.token_hex(16)
        request[_REQ_NEW_COOKIE] = visitor

    try:
        response = await handler(request)
    except web.HTTPException as exc:
        # 跳转（含 link 型项目的外链跳转、/apps/<id> 的补斜杠跳转）走的是这条路径
        _account(request, exc.status, visitor)
        apply_visitor_cookie(request, exc)
        raise

    _account(request, response.status, visitor)
    apply_visitor_cookie(request, response)
    return response
