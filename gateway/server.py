"""网关入口：组装路由并启动服务。

用法：
    python -m gateway.server [--host 0.0.0.0] [--port 38000]

路由总览：
    /                       门户首页（项目卡片）
    /assets/...             门户静态资源
    /api/site               站点文案配置
    /api/projects           项目列表（附运行状态；访问时热扫描 container/）
    /api/projects/<id>/icon 项目图标
    /apps/<id>/...          各子项目（静态直出或反向代理）
"""

import argparse
import logging

from aiohttp import web

from . import config, portal
from .hub import Hub
from .proxy import handle_app_request, page_not_found
from .registry import KIND_PROXY

log = logging.getLogger("gateway.server")


async def _resolve_project(request: web.Request):
    hub: Hub = request.app["hub"]
    project_id = request.match_info["id"]
    project = hub.get_project(project_id)
    if project is None:
        # 未命中时强制重扫描一次：允许通过 URL 直达刚放入 container/ 的新项目
        await hub.refresh()
        project = hub.get_project(project_id)
    return hub, project


async def apps_root(request: web.Request) -> web.StreamResponse:
    """/apps/<id>（无尾斜杠）→ 308 到 /apps/<id>/，保证页面内相对路径正确。"""
    _, project = await _resolve_project(request)
    if project is None:
        return page_not_found("没有找到这个项目，可能尚未接入或目录名不符。")
    location = project.url
    if request.query_string:
        location += "?" + request.query_string
    raise web.HTTPPermanentRedirect(location=location)


async def apps_dispatch(request: web.Request) -> web.StreamResponse:
    hub, project = await _resolve_project(request)
    if project is None:
        return page_not_found("没有找到这个项目，可能尚未接入或目录名不符。")

    app_proc = None
    if project.error is None and project.runtime.kind == KIND_PROXY:
        app_proc = await hub.supervisor.ensure_started(project.id)
    return await handle_app_request(request, project, app_proc, hub.session)


def build_app() -> web.Application:
    app = web.Application(client_max_size=1024 ** 3)
    hub = Hub()
    app["hub"] = hub

    app.router.add_get("/", portal.index)
    app.router.add_get("/favicon.ico", portal.favicon)
    app.router.add_get("/assets/{tail:.*}", portal.assets)
    app.router.add_get("/api/site", portal.api_site)
    app.router.add_get("/api/projects", portal.api_projects)
    app.router.add_get("/api/projects/{id}/icon", portal.api_project_icon)
    app.router.add_route("*", "/apps/{id}", apps_root)
    app.router.add_route("*", "/apps/{id}/{tail:.*}", apps_dispatch)
    app.router.add_route("*", "/{tail:.*}", portal.fallback)

    async def _on_startup(_app):
        await hub.start()

    async def _on_cleanup(_app):
        await hub.close()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="webcontainer 项目展示网关")
    parser.add_argument("--host", default=config.GATEWAY_HOST)
    parser.add_argument("--port", type=int, default=config.GATEWAY_PORT)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)

    log.info("门户地址: http://%s:%s/", args.host, args.port)
    log.info("子项目目录: %s", config.CONTAINER_DIR)
    web.run_app(
        build_app(),
        host=args.host,
        port=args.port,
        access_log_format='%a "%r" %s %b %Tfs',
        shutdown_timeout=15,
    )


if __name__ == "__main__":
    main()
