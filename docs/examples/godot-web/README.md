# Godot 4.7 网页游戏接入模板

把 Godot 4.7 导出的 Web 发布包接到门户上的最小骨架。现有样例：
`container/SWAPSHOT`（手写页面外壳）、`container/PlayCard`（Godot 默认导出模板），
两者用的 `tools/adapt.js` 就是本目录这一份。

## 用法

```bash
cp -r docs/examples/godot-web container/我的游戏
cd container/我的游戏
mkdir public && cp -r <Godot 导出目录>/* public/
# 按实际情况改 project.json（名称/简介/作者/order），放一个 icon.svg
node tools/adapt.js
```

刷新门户首页即可看到卡片，地址 `/apps/我的游戏/`。

## adapt.js 解决了什么

门户走 HTTP，非 localhost 访问时 `window.isSecureContext === false`，
Godot 4.7 的 Web 运行时有三处会挂：加载器把 Secure Context 当硬性门槛、
`godot_audio_init` 无条件调 `ctx.audioWorklet.addModule()`、
`connectPositionWorklet` 失败时连 `start()` 一起吞掉（音效全哑）。
另有一处是门户特有的：所有项目同一个来源，Godot 存档的 IndexedDB 库名固定为 `/userfs`，
必须按 `/apps/<项目>/` 加前缀隔离，否则两个游戏互相覆盖存档。

脚本还会生成 `.br` / `.gz` 预压缩副本（网关按 `Accept-Encoding` 自动改发），
39MB 的 `index.wasm` 压到约 6.6MB。

代价：播放位置上报（`get_playback_position`）失效、手柄不可用；键鼠与触屏不受影响。
门户若改走 HTTPS，这几处补丁会自动回到原逻辑。

`index.html` 的两处补丁内置了多套锚点（手写外壳 / Godot 默认模板），命中哪套自动判断；
都对不上会报错退出并指出是哪一处，不会写出半成品。

## 两条硬性纪律

1. **每次换新构建后必须重跑 `node tools/adapt.js`**——新包会覆盖掉补丁（外部访问直接打不开），
   残留的旧压缩副本还会让浏览器拿到上一版 `index.wasm`。脚本幂等，重跑总是安全的。
2. **只替换 `public/`，不要用发布包整包覆盖项目目录**——那会连 `project.json`、`icon.svg`、
   `tools/` 一起删掉，门户扫不到清单，卡片直接消失。真删了就从本目录 `cp` 回去。
