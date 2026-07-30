"""全局配置：路径约定与可用环境变量覆盖项。

所有可调项均支持环境变量覆盖，便于部署时不改代码：
    WC_HOST           监听地址，默认 0.0.0.0
    WC_PORT           监听端口，默认 38000
    WC_CONTAINER_DIR  子项目目录，默认 <仓库>/container
    WC_LOGS_DIR       日志目录，默认 <仓库>/logs
    WC_SCAN_INTERVAL  /api/projects 触发重扫描的最小间隔（秒），默认 5
    WC_NODE_BIN_DIR   子项目可用的 Node 运行时 bin 目录，默认 <仓库>/.node/bin
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

CONTAINER_DIR = Path(os.environ.get("WC_CONTAINER_DIR", BASE_DIR / "container"))
PORTAL_DIR = BASE_DIR / "portal"
LOGS_DIR = Path(os.environ.get("WC_LOGS_DIR", BASE_DIR / "logs"))
SITE_CONFIG_PATH = BASE_DIR / "site.config.json"

GATEWAY_HOST = os.environ.get("WC_HOST", "0.0.0.0")
GATEWAY_PORT = int(os.environ.get("WC_PORT", "38000"))

# Node 子项目的解释器：与 Python 侧「一律用仓库自带 .venv」同理，
# 仓库自带一份 Node（.node/bin），存在时会被前置到子进程 PATH，
# 于是清单里写 "node" / "npx" 就是这个版本，不受系统 node 新旧影响。
NODE_BIN_DIR = Path(os.environ.get("WC_NODE_BIN_DIR", BASE_DIR / ".node" / "bin"))

# 子项目统一挂载在 /apps/<id>/ 下；其余一级路径保留给门户自身
APPS_PREFIX = "/apps"

MANIFEST_NAME = "project.json"
SCAN_INTERVAL = float(os.environ.get("WC_SCAN_INTERVAL", "5"))

# 门户站点文案的兜底值（可被 site.config.json 覆盖）
SITE_DEFAULTS = {
    "org": "同济大学工程智能研究院",
    "title": "用AI点亮灵感，让想象触手可及",
    "subtitle": "同济大学工程智能研究院独立项目展示网页",
    "footer": "同济大学工程智能研究院",
}
