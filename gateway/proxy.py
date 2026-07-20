"""/apps/<id>/ 路由：静态项目直接出文件，动态项目反向代理到内部端口。

反代要点：
- 剥离前缀后转发，同时注入 X-Forwarded-Prefix，后端据此生成带前缀的 URL；
- 响应的 Location / Content-Location 若为不带前缀的绝对路径，自动补前缀
  （等价于 nginx proxy_redirect），兜底未完全适配前缀的后端；
- Set-Cookie 的 Path 属性同样补前缀，避免同源下多个项目的 Cookie 互相覆盖；
- 请求与响应均流式转发（不整体缓冲），支持大文件上传下载与 SSE；
- 支持 WebSocket 双向转发。
"""

import asyncio
import logging
import re
from urllib.parse import urlsplit

import aiohttp
from aiohttp import WSMsgType, web
from yarl import URL

from .registry import KIND_LINK, KIND_STATIC, Project
from .supervisor import STATE_ERROR, STATE_RUNNING, STATE_STARTING, AppProcess

log = logging.getLogger("gateway.proxy")

# RFC 7230 定义的逐跳首部，转发时必须剥离
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
}

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "[::1]"}
_COOKIE_PATH_RE = re.compile(r"(?i)(;\s*path=)([^;]*)")

UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=15, sock_connect=15, sock_read=None)


# ---- 响应头改写 ---------------------------------------------------------

def rewrite_location(value: str, prefix: str, upstream_port: int | None) -> str:
    """把后端返回的跳转地址映射回 /apps/<id>/ 前缀之下。"""
    if value.startswith("/"):
        if value == prefix or value.startswith(prefix + "/"):
            return value
        return prefix + value
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if parts.scheme in ("http", "https") and parts.hostname in _LOCAL_HOSTS:
        if upstream_port is None or parts.port == upstream_port:
            rebuilt = parts.path or "/"
            if parts.query:
                rebuilt += "?" + parts.query
            return rewrite_location(rebuilt, prefix, upstream_port)
    return value


def rewrite_cookie_path(set_cookie: str, prefix: str) -> str:
    """将 Set-Cookie 中 Path=/xxx 改写为 Path=<prefix>/xxx。"""

    def _sub(match: re.Match) -> str:
        path = match.group(2).strip()
        if path.startswith("/") and path != prefix and not path.startswith(prefix + "/"):
            path = prefix if path == "/" else prefix + path
        return match.group(1) + path

    return _COOKIE_PATH_RE.sub(_sub, set_cookie)


def _build_forward_headers(request: web.Request, project: Project, upstream_port: int) -> dict:
    headers = {}
    connection_tokens = {
        token.strip().lower()
        for token in request.headers.get("Connection", "").split(",") if token.strip()
    }
    for key, value in request.headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP or lower in connection_tokens or lower == "host":
            continue
        if lower.startswith("x-forwarded-") or lower == "x-real-ip":
            continue  # 不信任来路的转发头，统一由网关生成
        headers[key] = value

    peer = request.remote or ""
    host = request.host or ""
    if ":" in host:
        forwarded_port = host.rsplit(":", 1)[1]
    else:
        forwarded_port = "443" if request.scheme == "https" else "80"
    headers["Host"] = f"127.0.0.1:{upstream_port}"
    headers["X-Real-IP"] = peer
    headers["X-Forwarded-For"] = peer
    headers["X-Forwarded-Proto"] = request.scheme
    headers["X-Forwarded-Host"] = host
    headers["X-Forwarded-Port"] = forwarded_port
    headers["X-Forwarded-Prefix"] = project.prefix
    return headers


def _build_response_headers(upstream: aiohttp.ClientResponse, project: Project,
                            upstream_port: int | None) -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    for key, value in upstream.headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP:
            continue
        if lower in ("location", "content-location"):
            value = rewrite_location(value, project.prefix, upstream_port)
        elif lower == "set-cookie":
            value = rewrite_cookie_path(value, project.prefix)
        headers.append((key, value))
    return headers


# ---- 错误/等待页 --------------------------------------------------------

def _page(status: int, title: str, message: str, auto_refresh: bool = False) -> web.Response:
    refresh = '<meta http-equiv="refresh" content="3">' if auto_refresh else ""
    hint = "页面将自动重试…" if auto_refresh else '<a href="/">返回项目主页</a>'
    body = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{refresh}
<title>{title}</title>
<style>
  body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
       background:#f0eee6;color:#1f1e1b}}
  .box{{text-align:center;padding:40px 24px;max-width:420px}}
  .code{{font-size:56px;font-weight:700;color:#d97757;margin-bottom:8px;
        font-family:ui-serif,Georgia,"Songti SC",serif}}
  p{{color:#75716a;line-height:1.7}}
  a{{color:#c15f3c}}
</style></head>
<body><div class="box"><div class="code">{status}</div>
<h1 style="font-size:20px;margin:0 0 12px">{title}</h1>
<p>{message}</p><p>{hint}</p></div></body></html>"""
    return web.Response(status=status, text=body, content_type="text/html", charset="utf-8")


def page_not_found(message: str = "请检查地址是否正确，或回到主页查看全部项目。") -> web.Response:
    return _page(404, "页面不存在", message)


# ---- 静态项目 -----------------------------------------------------------

def _resolve_static_file(project: Project, tail: str) -> "web.FileResponse | web.Response":
    root = (project.dir / project.runtime.root).resolve()
    parts = [p for p in tail.split("/") if p not in ("", ".", "..")]
    target = root.joinpath(*parts) if parts else root

    try:
        target = target.resolve()
        if target != root and root not in target.parents:
            return page_not_found()
    except OSError:
        return page_not_found()

    if target.is_dir():
        target = target / "index.html"
    if not target.is_file() and project.runtime.spa:
        target = root / "index.html"
    if not target.is_file():
        return page_not_found()
    return web.FileResponse(target, headers={"Cache-Control": "no-cache"})


# ---- WebSocket 转发 -----------------------------------------------------

async def _proxy_websocket(request: web.Request, session: aiohttp.ClientSession,
                           upstream_url: str, headers: dict) -> web.WebSocketResponse:
    protocols = tuple(
        p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()
    )
    ws_server = web.WebSocketResponse(protocols=protocols)
    await ws_server.prepare(request)

    ws_headers = {
        k: v for k, v in headers.items()
        if k.lower() not in ("upgrade", "connection", "host")
        and not k.lower().startswith("sec-websocket-")
    }
    try:
        ws_client = await session.ws_connect(
            upstream_url, headers=ws_headers, protocols=protocols, heartbeat=30
        )
    except aiohttp.ClientError as exc:
        log.warning("WS 上游连接失败 %s: %s", upstream_url, exc)
        await ws_server.close(code=1011, message=b"upstream connect failed")
        return ws_server

    async def pump(source, target):
        async for msg in source:
            if msg.type == WSMsgType.TEXT:
                await target.send_str(msg.data)
            elif msg.type == WSMsgType.BINARY:
                await target.send_bytes(msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                break

    try:
        await asyncio.gather(pump(ws_server, ws_client), pump(ws_client, ws_server))
    finally:
        await ws_client.close()
        if not ws_server.closed:
            await ws_server.close(code=ws_client.close_code or 1000)
    return ws_server


# ---- 主处理入口 ---------------------------------------------------------

def _is_websocket_upgrade(request: web.Request) -> bool:
    connection = request.headers.get("Connection", "").lower()
    upgrade = request.headers.get("Upgrade", "").lower()
    return "upgrade" in connection and upgrade == "websocket"


async def handle_app_request(request: web.Request, project: Project,
                             app_proc: AppProcess | None,
                             session: aiohttp.ClientSession) -> web.StreamResponse:
    if project.error is not None:
        return _page(503, "项目配置有误", f"{project.id} 的 project.json 存在问题：{project.error}")

    if project.runtime.kind == KIND_LINK:
        raise web.HTTPFound(location=project.runtime.url)

    if project.runtime.kind == KIND_STATIC:
        return _resolve_static_file(project, request.match_info.get("tail", ""))

    if app_proc is None:
        return _page(503, "项目未托管", "后端进程尚未注册，请稍后重试或联系管理员。", auto_refresh=True)

    # 等待启动中的后端就绪（最多 15s），给出友好的等待页
    if app_proc.state != STATE_RUNNING:
        deadline = asyncio.get_running_loop().time() + 15
        while app_proc.state == STATE_STARTING and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.3)
        if app_proc.state == STATE_STARTING:
            return _page(503, "项目正在启动", "后端服务正在启动中，请稍候。", auto_refresh=True)
        if app_proc.state != STATE_RUNNING:
            detail = app_proc.error or "后端进程未在运行。"
            return _page(503, "项目暂不可用", detail, auto_refresh=True)

    assert app_proc.port is not None
    # raw_path 含原始百分号编码与查询串；剥掉 /apps/<id> 前缀原样转发给后端
    stripped = request.raw_path[len(project.prefix):] or "/"
    upstream_url = URL(f"http://127.0.0.1:{app_proc.port}{stripped}", encoded=True)

    headers = _build_forward_headers(request, project, app_proc.port)

    if _is_websocket_upgrade(request):
        return await _proxy_websocket(request, session, upstream_url, headers)

    body = request.content if request.body_exists else None
    try:
        async with session.request(
            request.method, upstream_url,
            headers=headers, data=body,
            allow_redirects=False, timeout=UPSTREAM_TIMEOUT,
        ) as upstream:
            response = web.StreamResponse(status=upstream.status, reason=upstream.reason)
            for key, value in _build_response_headers(upstream, project, app_proc.port):
                response.headers.add(key, value)

            no_body = (
                request.method == "HEAD"
                or upstream.status in (204, 304)
                or 100 <= upstream.status < 200
            )
            if not no_body and "Content-Length" not in response.headers:
                response.enable_chunked_encoding()

            await response.prepare(request)
            if not no_body:
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await response.write(chunk)
            await response.write_eof()
            return response
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("[%s] 上游请求失败 %s %s: %s", project.id, request.method, upstream_url, exc)
        return _page(502, "后端响应异常",
                     "项目后端未能正常响应，请稍后重试；若持续出现请查看该项目日志。")
