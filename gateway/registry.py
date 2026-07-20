"""项目注册表：扫描 container/ 下的子项目并解析 project.json 清单。

约定：
- container/ 下每个包含 project.json 的一级子目录视为一个项目；
- 项目 id 即目录名（用于 URL /apps/<id>/），必须是 URL 安全字符；
- 清单解析失败的项目仍会出现在列表中（error 非空，门户置灰显示），
  方便接入新项目时排查配置问题。
"""

import json
import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from . import config

log = logging.getLogger("gateway.registry")

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

KIND_PROXY = "proxy"    # 自带后端服务，由网关托管进程并反向代理
KIND_STATIC = "static"  # 纯静态站点，由网关直接提供文件
KIND_LINK = "link"      # 站外项目：卡片与 /apps/<id>/ 均跳转到外部地址


@dataclass
class Runtime:
    kind: str = KIND_STATIC
    # ---- kind == "proxy" ----
    command: list[str] = field(default_factory=list)
    cwd: str = "."                # 相对项目目录
    port: int | None = None       # 内部端口；缺省则由网关自动分配并经 $PORT 注入
    env: dict[str, str] = field(default_factory=dict)
    health_path: str | None = None
    startup_timeout: float = 60.0
    auto_start: bool = True
    # ---- kind == "static" ----
    root: str = "."               # 静态文件根目录，相对项目目录
    spa: bool = False             # 单页应用：404 时回退到 index.html
    # ---- kind == "link" ----
    url: str = ""                 # 外部地址（http/https）


@dataclass
class Project:
    id: str
    dir: Path
    name: str = ""
    description: str = ""
    type: str = "Web 应用"
    author: str = ""
    tags: list[str] = field(default_factory=list)
    icon: str | None = None       # 图标文件，相对项目目录
    order: int = 100              # 门户排序，小者靠前
    hidden: bool = False          # 隐藏卡片但仍可通过 URL 访问
    runtime: Runtime = field(default_factory=Runtime)
    manifest_mtime: float = 0.0
    error: str | None = None      # 清单解析/校验错误

    @property
    def prefix(self) -> str:
        return f"{config.APPS_PREFIX}/{self.id}"

    @property
    def url(self) -> str:
        if self.runtime.kind == KIND_LINK and self.runtime.url:
            return self.runtime.url
        return f"{self.prefix}/"


def _parse_runtime(raw: dict, project_dir: Path) -> Runtime:
    rt = Runtime()
    kind = str(raw.get("kind", KIND_STATIC)).strip().lower()
    if kind not in (KIND_PROXY, KIND_STATIC, KIND_LINK):
        raise ValueError(
            f"runtime.kind 必须是 {KIND_PROXY!r}、{KIND_STATIC!r} 或 {KIND_LINK!r}，当前为 {kind!r}"
        )
    rt.kind = kind

    if kind == KIND_LINK:
        url = str(raw.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("runtime.kind=link 时必须提供以 http(s):// 开头的 runtime.url")
        rt.url = url
    elif kind == KIND_PROXY:
        command = raw.get("command")
        if isinstance(command, str):
            rt.command = shlex.split(command)
        elif isinstance(command, list) and all(isinstance(x, str) for x in command):
            rt.command = list(command)
        if not rt.command:
            raise ValueError("runtime.kind=proxy 时必须提供 runtime.command（字符串或字符串数组）")

        rt.cwd = str(raw.get("cwd", "."))
        if not (project_dir / rt.cwd).resolve().is_dir():
            raise ValueError(f"runtime.cwd 目录不存在：{rt.cwd}")

        port = raw.get("port")
        if port is not None:
            if not isinstance(port, int) or not (1 <= port <= 65535):
                raise ValueError(f"runtime.port 必须是 1-65535 的整数，当前为 {port!r}")
            rt.port = port

        env = raw.get("env", {})
        if not isinstance(env, dict):
            raise ValueError("runtime.env 必须是对象（键值均为字符串）")
        rt.env = {str(k): str(v) for k, v in env.items()}

        health = raw.get("healthPath")
        if health is not None:
            health = str(health)
            if not health.startswith("/"):
                raise ValueError("runtime.healthPath 必须以 / 开头")
            rt.health_path = health

        rt.startup_timeout = float(raw.get("startupTimeoutSec", 60))
        rt.auto_start = bool(raw.get("autoStart", True))
    else:
        rt.root = str(raw.get("root", "."))
        if not (project_dir / rt.root).resolve().is_dir():
            raise ValueError(f"runtime.root 目录不存在：{rt.root}")
        rt.spa = bool(raw.get("spa", False))

    return rt


def _load_project(project_dir: Path) -> Project:
    pid = project_dir.name
    manifest = project_dir / config.MANIFEST_NAME
    project = Project(id=pid, dir=project_dir, name=pid)

    try:
        project.manifest_mtime = manifest.stat().st_mtime
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("project.json 顶层必须是 JSON 对象")

        project.name = str(raw.get("name", pid)).strip() or pid
        project.description = str(raw.get("description", "")).strip()
        project.type = str(raw.get("type", "Web 应用")).strip() or "Web 应用"
        project.author = str(raw.get("author", "")).strip()
        tags = raw.get("tags", [])
        if isinstance(tags, list):
            project.tags = [str(t) for t in tags][:8]
        project.order = int(raw.get("order", 100))
        project.hidden = bool(raw.get("hidden", False))

        icon = raw.get("icon")
        if icon:
            icon_path = _safe_join(project_dir, str(icon))
            if icon_path is None or not icon_path.is_file():
                raise ValueError(f"icon 文件不存在或越出项目目录：{icon}")
            project.icon = str(icon)

        project.runtime = _parse_runtime(raw.get("runtime", {}), project_dir)
    except json.JSONDecodeError as exc:
        project.error = f"project.json 不是合法 JSON：{exc}"
    except (ValueError, OSError) as exc:
        project.error = str(exc)

    if not ID_PATTERN.match(pid):
        project.error = (
            f"目录名 {pid!r} 不能用于 URL：仅允许字母、数字、下划线、点、连字符"
        )
    return project


def _safe_join(base: Path, relative: str) -> Path | None:
    """将 relative 拼接到 base 下，拒绝任何越出 base 的路径。"""
    try:
        candidate = (base / relative).resolve()
        base_resolved = base.resolve()
    except OSError:
        return None
    if candidate == base_resolved or base_resolved in candidate.parents:
        return candidate
    return None


def scan() -> dict[str, Project]:
    """扫描 container/，返回 {id: Project}，仅收录带 project.json 的目录。"""
    projects: dict[str, Project] = {}
    root = config.CONTAINER_DIR
    if not root.is_dir():
        log.warning("container 目录不存在：%s", root)
        return projects

    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not (entry / config.MANIFEST_NAME).is_file():
            log.debug("跳过 %s：缺少 %s", entry.name, config.MANIFEST_NAME)
            continue
        project = _load_project(entry)
        if project.error:
            log.warning("项目 %s 配置有误：%s", project.id, project.error)
        projects[project.id] = project
    return projects
