"""门户路由：首页、静态资源、项目列表 API、项目图标。

保留给门户的一级路径：/ 、/assets/ 、/api/ 、/favicon.ico；
子项目一律挂在 /apps/<id>/ 下，二者不会冲突。
"""

import logging

from aiohttp import web

from . import config
from .hub import Hub
from .proxy import page_not_found
from .registry import _safe_join  # 复用同一套路径防护逻辑

log = logging.getLogger("gateway.portal")


def _hub(request: web.Request) -> Hub:
    return request.app["hub"]


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(
        config.PORTAL_DIR / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


async def favicon(request: web.Request) -> web.FileResponse:
    return web.FileResponse(
        config.PORTAL_DIR / "img" / "favicon.svg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def assets(request: web.Request) -> web.StreamResponse:
    tail = request.match_info["tail"]
    target = _safe_join(config.PORTAL_DIR, tail)
    if target is None or not target.is_file():
        return page_not_found()
    return web.FileResponse(target, headers={"Cache-Control": "public, max-age=300"})


async def api_site(request: web.Request) -> web.Response:
    return web.json_response(_hub(request).site_config())


async def api_projects(request: web.Request) -> web.Response:
    hub = _hub(request)
    await hub.refresh()  # 节流的热扫描：新放入 container/ 的项目由此生效
    return web.json_response({"projects": hub.portal_payload()})


async def api_project_icon(request: web.Request) -> web.StreamResponse:
    hub = _hub(request)
    project = hub.get_project(request.match_info["id"])
    if project is None or not project.icon:
        return page_not_found("该项目没有配置图标。")
    target = _safe_join(project.dir, project.icon)
    if target is None or not target.is_file():
        return page_not_found("图标文件不存在。")
    return web.FileResponse(target, headers={"Cache-Control": "public, max-age=3600"})


async def fallback(request: web.Request) -> web.Response:
    return page_not_found()
