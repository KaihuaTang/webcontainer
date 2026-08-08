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
│  /                    门户首页（项目卡片、搜索、类型筛选、访问次数）                 │
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
│   ├── visits.py             #   访问统计：按「一次访问」去重计数并落盘
│   ├── hub.py                #   运行时状态编排（热扫描、状态查询）
│   └── server.py             #   入口：python -m gateway.server
├── portal/                   # 门户前端（纯静态：HTML/CSS/JS，无构建步骤）
├── container/                # ★ 各独立项目，一个子目录一个项目
│   ├── KnowledgeIndex/       #   示例：格物知新（Flask）
│   │   └── project.json      #   项目清单（卡片信息 + 运行方式）
│   └── pinned.json           #   置顶清单 + 每日惊喜开关，刷新页面即生效
├── docs/examples/            # 新项目接入模板（静态版 / Flask 版 / Godot 版 / 前端前缀补丁）
├── scripts/                  # setup.sh / setup-node.sh / start.sh / stop.sh / precompress.js
├── deploy/                   # systemd 服务模板
├── site.config.json          # 门户文案（标题、副标题、页脚），改完刷新页面即生效
├── requirements.txt
├── data/                     # 运行期生成：visits.json 访问统计累计值（git 忽略）
└── logs/                     # 运行期生成：gateway.log 与各项目日志（git 忽略）
```

## 快速开始

```bash
cd /home/kaihua/projects/webcontainer

# 1. 初始化（创建 .venv 并安装依赖；本机首次已完成，可跳过）
./scripts/setup.sh            # 可用 PYTHON=/usr/local/bin/python3 指定基础解释器
./scripts/setup-node.sh       # 仅当有 Node 子项目时：装平台自带 Node 到 .node/（见下）

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
| `WC_NODE_BIN_DIR` | `<仓库>/.node/bin` | 给 Node 子项目用的解释器目录（存在即前置到子进程 PATH） |
| `WC_DATA_DIR` | `<仓库>/data` | 运行期数据目录（访问统计 `visits.json`） |
| `WC_VISIT_IDLE_TTL` | `1800` | 访问会话空闲多少秒后结束（下次再来算新的一次访问） |
| `WC_VISIT_MAX_SESSION` | `21600` | 单次访问最长按多少秒计（防一直开着的标签页把计数冻住） |

例：`WC_PORT=39000 ./scripts/start.sh`

### 两套「平台自带解释器」

跟 Python 侧一律用仓库自带 `.venv` 同理，Node 子项目一律用仓库自带 `.node`：

```bash
./scripts/setup-node.sh                       # 装默认版本（当前 v24.18.1 LTS）到 .node/
NODE_VERSION=v22.23.2 ./scripts/setup-node.sh # 指定版本
https_proxy=http://127.0.0.1:7892 ./scripts/setup-node.sh   # 本机需代理出海时
```

网关启动子进程时会把 `.node/bin` 前置到它的 `PATH`，所以 `project.json` 里写
`"command": ["node", …]` 就是这一份，不受系统 node 新旧影响
（本机 `/usr/bin/node` 是 20.20.2，跑 vinext 会报 `does not provide an export named 'glob'`）。
子项目装依赖同理要用它：`PATH="$PWD/../../.node/bin:$PATH" npm install`
——**尤其是从 macOS 拷过来的项目**：`node_modules` 里的原生模块
（rolldown / workerd / sharp / esbuild）是 darwin-arm64 版，在 Linux 上必须重装一遍。
`.venv/` 与 `.node/` 都不入库。

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
    "addedAt": "2026-07-30",          // 可选，上架时间；门户非置顶项按它倒序排（新的在上）
    "order": 100,                     // 可选，同一上架时间内的次序，小者靠前
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

现成模板在 `docs/examples/`：`hello-static`（纯静态）、`hello-flask`（Flask 后端）、
`godot-web`（Godot 4.7 发布包）、`nextjs-vinext`（Next.js on Vite）。

### 情形一：静态站点（最简单）

```bash
cp -r docs/examples/hello-static container/我的项目名
# 按需修改 container/我的项目名/project.json 与 public/ 下的页面
```

刷新门户页即可看到卡片。**页面内引用资源请用相对路径**（`./style.css`），
不要以 `/` 开头（`/style.css` 会指到门户根路径而不是你的项目）。

静态项目若有大体积资源，可在同目录放一份预压缩副本 `<文件>.br` / `<文件>.gz`：
网关会按请求的 `Accept-Encoding` 优先发送它，并自动补 `Content-Encoding` 与
`Vary` 头，`Content-Type` 仍按原文件名判定（这是 aiohttp `FileResponse` 的内建能力，
见 `web_fileresponse.py` 的 `ENCODING_EXTENSIONS`，网关侧无需额外代码）。生成用共用脚本：

```bash
node scripts/precompress.js container/<项目>/dist        # 递归整棵目录树，幂等
node scripts/precompress.js --force container/<项目>/dist # 忽略新鲜度全部重压
```

**每次换构建后都要重跑**：旧副本会被网关优先发出，让访客拿到上一版代码
（脚本会按 mtime 清掉过期副本，孤儿副本也一并清）。压缩率高于 90% 的文件不留副本。
`container/SWAPSHOT`、`container/PlayCard` 这类 Godot 项目用各自的 `tools/adapt.js`
（打补丁 + 压缩一起做，39MB 的 wasm 压到 6.6MB），逻辑与本脚本一致。

另注两个「单来源多项目」相关的坑：

1. 门户走 HTTP，非 localhost 访问时 `window.isSecureContext` 为 false，依赖安全上下文的
   Web API（Gamepad、crypto.subtle、AudioWorklet 等）不可用；Godot 之类会主动检查
   安全上下文、或在无 AudioWorklet 时直接抛错的运行时需要额外放行。
2. 所有项目同处 `http://<主机>:38000` 一个来源，**localStorage / IndexedDB 是共享的**，
   库名或键名相同的两个项目会互相覆盖数据（Godot 的 IDBFS 固定用 `/userfs`），
   需要各自按 `/apps/<项目>/` 前缀隔离。

两点的具体做法见 `container/SWAPSHOT/README.md` 与其 `tools/adapt.js`。

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
venv），保证依赖一致；写 `node` / `npx` 则解析到平台自带的 `.node/bin`
（见上文「两套平台自带解释器」）。如果项目需要独立环境，写绝对路径即可，例如
`["/path/to/其他venv/bin/python", "app.py"]`（新依赖记得补进
`requirements.txt` 或项目自己的 venv）。

Node 项目的清单长这样（`container/TideLoom`、`container/ZeroHourChoir` 即此类，
vinext = Next.js on Vite，`vinext start` 是一个纯 Node HTTP 生产服务器，读 `PORT`）：

```json
{
    "runtime": {
        "kind": "proxy",
        "command": ["node", "node_modules/.bin/vinext", "start"],
        "cwd": ".",
        "env": { "NODE_ENV": "production" },
        "startupTimeoutSec": 30
    }
}
```

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

目录里只需 `project.json` 和图标文件。卡片指向 `/apps/<id>/`，由网关 302 跳到外部地址
（这样「从门户点进去」才计得到一次访问；鼠标悬停在卡片上会显示真实目标）。
`container/PaperMagician` 即此类样例。

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

#### Next.js / vinext 项目：前缀落在 Vite `base`，不要用 `basePath`

网关注入的 `WC_APP_PREFIX`（即 `/apps/<目录名>`）对这类项目要**只作用于资源 URL**，
路由仍留在 `/` 由网关剥前缀转发。`container/TideLoom`、`container/ZeroHourChoir`
是完整样例，三步：

1. `vite.config.ts`：`base: process.env.WC_APP_PREFIX ? WC_APP_PREFIX + '/' : '/'`；
2. `package.json` 加一条 `"build:portal": "WC_APP_PREFIX=/apps/<目录名> npm run build
   && node ../../scripts/precompress.js dist/client"`——前缀是**构建期**常量，
   忘了带就整站资源 404；`npm test` 会顺带跑一次不带前缀的 `npm run build`，
   **跑完测试要重新 `npm run build:portal`**；
3. `<head>` 里手写的 `<link>`、社交卡片的绝对 URL、`app/icon.svg` 生成的图标链接
   `base` 管不到，用 `process.env.WC_APP_PREFIX` 自己拼（见两个项目的 `app/layout.tsx`）。

**为什么不用 Next 的 `basePath`**：`vinext` 0.0.50 的 Node 生产服务器在 App Router 分支里
用一条不认 `basePath` 的捷径直出 `/assets/*`（`prod-server.js`:
`pathname.startsWith("/assets/")`），配了 `basePath` 后 HTML / 字体 / 图标正常
（它们走 RSC handler）而**所有打包 chunk 404**，很难一眼看出。
另外 `vinext build` 不生成预压缩副本（Cloudflare 在边缘压），
但它的静态直出会优先发同名 `.br/.gz`——所以 `build:portal` 里带上 `precompress`，
600KB 的 three.js chunk 由此降到 ~124KB。

### 卡片排序：每日惊喜 → 置顶 → 上架时间倒序

门户首页从上到下是三段：

1. **每日惊喜**（每天换一个，见下）；
2. **置顶**，按 `container/pinned.json` 里 `pinned` 数组的书写顺序；
3. **其余项目**，按 `project.json` 的 `addedAt`（上架时间）**倒序**——越新的越靠前；
   同一上架时间内由 `order` 决定，再同则按名称。

`addedAt` 接受 `2026-07-30`、`2026-07-30T11:36:48+08:00`（结尾 `Z` 也行），
`2026/7/30`、`2026-7-30 11:36` 这类手写形式同样认；不带时区的按服务器本机时区理解。
**不写这个字段就退回 `project.json` 的 mtime**——所以老项目不填也能排得大致对，
但只要改过一次清单它就会跳到前面去，长期在线的项目建议显式写上。
写成认不出的日期会让该项目在门户上标成「配置有误」（与 `order` 写错的处理一致）。

```json
{
    "pinned": ["StockMonitoring", "KnowledgeIndex"],
    "dailySurprise": true
}
```

- id 用 `container/` 下的**目录名**，数组顺序就是卡片顺序；
- 置顶卡片会带一枚橙色「置顶」徽标，描边也会加深；
- 改完**刷新门户页即生效**，不用重启网关（按文件 mtime 判断是否重读）；
- 文件缺失、写成非法 JSON、或列了不存在的 id 都只当作「无置顶/忽略该条」处理，
  并在 `logs/gateway.log` 留一条 warning，不会让门户挂掉；
- 下划线开头的键（`_说明`、`_示例`）仅作注释。

**每日惊喜**（`dailySurprise`，缺省开启）：每天从「非置顶且配置无误」的项目里挑一个，
排在所有置顶之前，带一枚金色「每日惊喜」徽标。设成 `false` 即关闭。

它不是每天独立掷骰子——那样约每 N 天就会连着两天挑中同一个。实现是把候选整体洗牌
成一轮（N 天走完一轮，轮内人人恰好轮到一次），换轮时首尾撞车就对调，因此
**连续两天不会重样、每个项目获得的曝光次数长期均等**（`gateway/hub.py` 的
`daily_surprise_id`）。全程由日期推导，不落盘、不需要定时任务：同一天内所有访客
看到的是同一个，跨零点自动换人。接入或下线项目会让整轮排布重算，当天结果可能变。

`container/` 下的其他内容默认不入库，`pinned.json` 是例外（属平台配置，见 `.gitignore`）。

### 访问统计

门户卡片右下角的 👁 数字是该项目的**累计访问次数**，页脚显示门户首页自身的累计访问次数。
统计完全在网关内完成（`gateway/visits.py`），不接任何第三方分析服务，也不需要项目做适配。

口径（尽量贴近「一次访问算一次」）：

- **只有页面级请求才计数**：看 `Sec-Fetch-Dest: document`（现代浏览器都会带），
  拿不到就退化为 `Accept` 含 `text/html`。页面里的 JS/CSS/图片/接口请求一律不计，
  所以打开一个页面只会 +1，不会被几十个静态请求刷上去；
- **同一访客的同一次会话只计一次**：访客身份是网关下发的 `wc_vid` Cookie
  （`Path=/`、`HttpOnly`、`SameSite=Lax`、400 天），所以**从门户点进去**和
  **直接用 URL 打开项目**走的是同一套去重。会话默认空闲 30 分钟结束、整场最长 6 小时；
  期间任何请求（含子资源、门户每 30 秒的状态轮询）都会续期——
  连续玩一个网页游戏、或开着门户不动，都只算一次访问；
- 门户与每个项目各自独立计数：从门户点进某个项目，是门户 +1、该项目 +1；
- 明显的爬虫 UA（bot/crawler/spider…）跳过；响应 `>=400`（404、启动中的 503）不计数；
- `kind=link` 的站外项目：卡片指向 `/apps/<id>/`，网关 302 转出时计一次。
  站外页面本身的浏览量统计不到（不在本平台），这里记的是「从门户点出去」的次数，
  且同样按会话去重（30 分钟内反复点算一次），好跟其他项目的数字可比。

累计值存在 `data/visits.json`（每 30 秒有变更才落盘，临时文件 + 原子替换，随部署环境走、不入库），
网关重启后累计值不丢，但会话表只在内存里，重启后所有人重新开始一次新会话。
清空 Cookie / 无痕窗口 / 换浏览器都会被当作新访客——对展示站来说这个精度足够。

想清零或修正某个项目的计数：停掉网关，编辑 `data/visits.json` 的 `counts`
（`@portal` 是门户首页），再启动；运行中改会被内存里的值覆盖回去。

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
| 换了静态项目的构建 | 重跑 `node scripts/precompress.js container/<id>/<静态根>`（旧压缩副本会被优先发出） |
| 换了 Next/vinext 项目的构建 | 在项目里跑 `npm run build:portal`，再 `touch project.json` 让网关重启它 |
| 修改项目清单 | 保存 `project.json` 后刷新门户页，网关检测到变更会自动重启该项目进程 |
| 手动重启某项目 | `touch container/<id>/project.json`，刷新门户页 |
| 下线项目 | 移出 `container/`（或先加 `"hidden": true` 只隐藏卡片），刷新门户页 |
| 置顶常用项目 | 把目录名填进 `container/pinned.json` 的 `pinned` 数组，刷新门户页 |
| 调整卡片先后 | 改 `project.json` 的 `addedAt`（非置顶项按它倒序排），刷新门户页 |
| 关掉每日惊喜 | `container/pinned.json` 里设 `"dailySurprise": false`，刷新门户页 |
| 查/改访问次数 | 看 `data/visits.json`（`@portal` 为门户首页）；要改先停网关，否则会被内存值覆盖 |
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
