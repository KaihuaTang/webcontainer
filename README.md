# webcontainer · 同济大学工程智能研究院独立项目展示平台

一个自包含的「项目门户 + 应用网关」：门户首页以卡片形式展示 `container/`
下的各个独立网页项目（图标 / 名称 / 简介 / 类型 / 作者），并让所有项目
共用 **38000 一个端口**，仅通过 URL 路径区分、直接访问。

- 门户首页：`http://<服务器>:38000/`
- 各个项目：`http://<服务器>:38000/apps/<项目目录名>/`

门户页面同时适配桌面与手机（响应式布局，自动跟随系统深色模式）。

## 整体架构

```
浏览器
  │  http://<host>:38000
  ▼
┌───────────────────────── gateway（aiohttp，单端口 38000）─────────────────────────┐
│  /                    门户首页（项目卡片、搜索、类型筛选）                          │
│  /assets/…  /api/…    门户静态资源与数据接口（项目列表、图标、站点文案）             │
│  /apps/<id>/…         子项目入口，按 container/<id>/project.json 分两类处理：       │
│      kind=static  →  网关直接托管静态文件                                          │
│      kind=proxy   →  网关启动并看护子进程（日志/崩溃重启），反向代理到内部端口        │
└──────────────────────────────────────────────────────────────────────────────────┘
         │ 反代时注入 X-Forwarded-Prefix，并自动改写 Location 与 Cookie Path
         ▼
   container/KnowledgeIndex（Flask，内部端口 3008）…以及后续接入的更多项目
```

## 目录结构

```
webcontainer/
├── gateway/                  # 网关后端（Python / aiohttp）
│   ├── config.py             #   路径与端口配置（支持环境变量覆盖）
│   ├── registry.py           #   扫描 container/、解析 project.json
│   ├── supervisor.py         #   子项目进程托管：启动/健康检查/崩溃重启/回收
│   ├── proxy.py              #   反向代理（HTTP/WebSocket/流式），前缀与 Cookie 改写
│   ├── portal.py             #   门户路由与 API
│   ├── hub.py                #   运行时状态编排（热扫描、状态查询）
│   └── server.py             #   入口：python -m gateway.server
├── portal/                   # 门户前端（纯静态：HTML/CSS/JS，无构建步骤）
├── container/                # ★ 各独立项目，一个子目录一个项目
│   └── KnowledgeIndex/       #   示例：格物知新（Flask）
│       └── project.json      #   项目清单（卡片信息 + 运行方式）
├── docs/examples/            # 新项目接入模板（静态版 / Flask 版 / 前端前缀补丁）
├── scripts/                  # setup.sh / start.sh / stop.sh
├── deploy/                   # systemd 服务模板
├── site.config.json          # 门户文案（标题、副标题、页脚），改完刷新页面即生效
├── requirements.txt
└── logs/                     # 运行期生成：gateway.log 与各项目日志（git 忽略）
```

## 快速开始

```bash
cd /home/kaihua/projects/webcontainer

# 1. 初始化（创建 .venv 并安装依赖；本机首次已完成，可跳过）
./scripts/setup.sh            # 可用 PYTHON=/usr/local/bin/python3 指定基础解释器

# 2. 启动
./scripts/start.sh            # 前台运行，Ctrl+C 退出
./scripts/start.sh -d         # 或后台守护运行
./scripts/stop.sh             # 停止后台运行的网关

# 3. 访问
#    门户    http://<服务器IP>:38000/
#    格物知新 http://<服务器IP>:38000/apps/KnowledgeIndex/
```

网关退出时会自动回收它启动的全部子项目进程，不会残留。

### 修改端口 / 常用环境变量

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WC_PORT` | `38000` | 网关监听端口 |
| `WC_HOST` | `0.0.0.0` | 监听地址 |
| `WC_CONTAINER_DIR` | `<仓库>/container` | 子项目目录 |
| `WC_LOGS_DIR` | `<仓库>/logs` | 日志目录 |

例：`WC_PORT=39000 ./scripts/start.sh`

### 开机自启（systemd，可选）

```bash
# 按需修改 deploy/webcontainer.service 中的 User 与路径
sudo cp deploy/webcontainer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now webcontainer
```

## 接入新项目

> 一句话：**在 `container/` 下放一个目录，目录里放一个 `project.json`，
> 刷新门户页即可**。网关会热扫描 `container/`，新项目无需重启网关。

### project.json 字段说明

```jsonc
{
    "name": "项目显示名",             // 必填建议项；缺省用目录名
    "description": "一句话简介",       // 卡片上的介绍
    "type": "Web 应用",               // 卡片类型徽标，也用于门户筛选（自由文本）
    "author": "作者/团队",
    "tags": ["标签1", "标签2"],        // 可选，参与门户搜索
    "icon": "public/icon.png",        // 可选，相对本项目目录；缺省显示首字头像
    "order": 100,                     // 可选，门户排序，小者靠前
    "hidden": false,                  // 可选，true 时不出卡片但 URL 仍可访问

    "runtime": {
        "kind": "static | proxy | link",  // 三选一，见下文

        // ---- kind = "static"（纯静态站点）----
        "root": "public",             // 静态文件根目录，相对本项目目录
        "spa": false,                 // 单页应用路由时设 true（404 回退 index.html）

        // ---- kind = "link"（站外项目，仅做展示卡片）----
        "url": "https://…",           // 点击卡片直达的外部地址；/apps/<id>/ 也会 302 跳转过去

        // ---- kind = "proxy"（自带后端进程）----
        "command": ["python3", "app.py"],  // 启动命令（数组或字符串）
        "cwd": ".",                   // 工作目录，相对本项目目录
        "port": 3008,                 // 内部端口；不写则网关自动分配空闲端口
        "env": {"KEY": "VALUE"},      // 附加环境变量
        "healthPath": "/",            // 可选，就绪探测路径
        "startupTimeoutSec": 60,      // 等待启动就绪的超时
        "autoStart": true             // false 时首次被访问才启动
    }
}
```

注意：目录名会成为 URL（`/apps/<目录名>/`），只能包含字母、数字、`_`、`.`、`-`。

### 情形一：静态站点（最简单）

```bash
cp -r docs/examples/hello-static container/我的项目名
# 按需修改 container/我的项目名/project.json 与 public/ 下的页面
```

刷新门户页即可看到卡片。**页面内引用资源请用相对路径**（`./style.css`），
不要以 `/` 开头（`/style.css` 会指到门户根路径而不是你的项目）。

### 情形二：自带后端的动态项目

```bash
cp -r docs/examples/hello-flask container/我的项目名
```

网关会启动 `runtime.command` 指定的进程，并做三件事：

1. 注入环境变量 `PORT`（以及 `WC_APP_ID`、`WC_APP_PREFIX`）——
   **程序必须监听 `PORT` 指定的端口**（或在清单中写死 `port` 并保持一致）；
2. 把 `/apps/<id>/xxx` 剥掉前缀转发到 `http://127.0.0.1:<port>/xxx`，
   同时携带 `X-Forwarded-Prefix: /apps/<id>` 等标准转发头；
3. 看护进程：写日志到 `logs/<id>.log`，崩溃后指数退避自动重启，
   网关退出时整组回收。

`command` 第一个词写 `python3` 时会自动替换为网关所用解释器（即项目
venv），保证依赖一致；如果项目需要独立环境，写绝对路径即可，例如
`["/path/to/其他venv/bin/python", "app.py"]`（新依赖记得补进
`requirements.txt` 或项目自己的 venv）。

### 情形三：站外项目（纯链接卡片）

项目部署在别处（GitHub Pages、独立服务器、应用官网等），只想在门户挂一张卡片：

```json
{
    "name": "PaperMagician",
    "description": "……",
    "type": "桌面软件",
    "author": "汤凯华",
    "icon": "icon.png",
    "runtime": { "kind": "link", "url": "https://kaihuatang.github.io/PaperMagician/" }
}
```

目录里只需 `project.json` 和图标文件。点击卡片直接打开外部地址，
`/apps/<id>/` 也会 302 跳转过去。`container/PaperMagician` 即此类样例。

### 关键：让后端适配「子路径前缀」

项目通过 `/apps/<id>/` 访问，页面里 **以 `/` 开头的绝对路径都会跳出项目
自己的命名空间**，这是接入既有项目时唯一需要改造的点。改造清单（Flask 为例）：

1. **服务端感知前缀** —— 挂 ProxyFix 并开启 `x_prefix`：

   ```python
   from werkzeug.middleware.proxy_fix import ProxyFix
   app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)
   ```

   此后 `url_for()`、`redirect(url_for(...))`、模板里的静态资源引用全部自动带前缀。

2. **模板不要硬编码绝对路径** —— `href="/app"` 改为 `href="{{ url_for('app_view') }}"`。

3. **前端 JS**：
   - 在模板注入 `window.APP_ROOT = {{ request.script_root|tojson }};`
     并引入 `docs/examples/prefix-shim.js`（放到项目静态目录），
     即可让存量 `fetch('/api/...')`、`XMLHttpRequest`、`EventSource` 自动补前缀；
   - 拼进 HTML 的链接（`innerHTML` 里的 `href/src`）与 `location.href`
     跳转，shim 拦截不到，需显式写成 `${window.APP_ROOT || ''}/xxx`。

4. **兜底**：即使后端偶有漏网的 `redirect('/xxx')` 或 `Path=/` 的
   Cookie，网关也会自动改写 Location 与 Set-Cookie 的 Path 到前缀之下；
   但 HTML 里的绝对路径网关无法代改，仍需按 2、3 处理。

其他框架同理：Express 用 `app.use(prefix, router)` 或读
`X-Forwarded-Prefix`；FastAPI 传 `root_path`；前端构建产物把
`base`/`publicPath` 设为 `./`（相对路径）即可免改造。
`container/KnowledgeIndex` 是一个完整的适配样例，可对照参考。

### 接入自测清单

- [ ] 门户页出现卡片，图标/名称/简介/类型/作者显示正确；
- [ ] 打开 `/apps/<id>/`，页面样式与脚本正常加载（浏览器 Network 面板里
      资源地址都在 `/apps/<id>/` 之下，没有 404）；
- [ ] 页内跳转、表单提交、登录后重定向都停留在 `/apps/<id>/` 前缀下；
- [ ] `kind=proxy` 项目：`logs/<id>.log` 有正常启动日志。

## 日常运维

| 操作 | 方法 |
| --- | --- |
| 看网关日志 | `tail -f logs/gateway.log`（后台模式） |
| 看某项目日志 | `tail -f logs/KnowledgeIndex.log` |
| 新增项目 | 放入 `container/`，刷新门户页 |
| 修改项目清单 | 保存 `project.json` 后刷新门户页，网关检测到变更会自动重启该项目进程 |
| 手动重启某项目 | `touch container/<id>/project.json`，刷新门户页 |
| 下线项目 | 移出 `container/`（或先加 `"hidden": true` 只隐藏卡片），刷新门户页 |
| 重启全部 | `./scripts/stop.sh && ./scripts/start.sh -d` |
| 修改门户文案 | 编辑 `site.config.json`，刷新页面即生效 |
| 调整门户样式 | 改 `portal/` 下的 HTML/CSS/JS，无需构建，刷新即生效 |

## 常见问题

- **卡片显示「配置错误」**：`project.json` 不是合法 JSON 或字段不符，
  卡片上会给出具体原因；修正后刷新门户页即可。
- **项目页 503「正在启动/暂不可用」**：后端未就绪或启动失败，查
  `logs/<id>.log`；固定 `port` 与其他程序冲突时也会在此提示。
- **页面白屏/样式丢失**：多半是绝对路径未适配前缀，按上文「适配子路径
  前缀」逐条检查。
- **两个项目登录态互相顶掉**：确认经网关访问（网关会把 Cookie Path 隔离
  到各自 `/apps/<id>` 下）；若项目把 Cookie 写到自定义域/路径则需项目侧调整。
