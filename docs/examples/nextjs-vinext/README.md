# Next.js / vinext 项目接入模板

把一个 vinext（Next.js on Vite，Cloudflare 系脚手架常见）项目接到门户上。
本目录只有清单骨架 + 这份说明——项目本体是个完整 npm 工程，没法用模板目录套。
现有样例：`container/TideLoom`、`container/ZeroHourChoir`（两者的 `PORTAL.md`
是逐项目版本的同一份说明）。

## 用法

```bash
cp docs/examples/nextjs-vinext/project.json container/我的项目/
cd container/我的项目
# 1) 装依赖（一律用平台自带 Node；macOS 拷来的 node_modules 必须重装）
PATH="$PWD/../../.node/bin:$PATH" npm install
# 2) 按下面三步改配置，然后
PATH="$PWD/../../.node/bin:$PATH" npm run build:portal
```

刷新门户首页即可看到卡片，地址 `/apps/我的项目/`。

## 三步改造

网关会给子进程注入 `WC_APP_PREFIX=/apps/<目录名>`。要让**资源 URL 带前缀、
路由留在 `/`**（前缀由网关剥掉）：

1. `vite.config.ts` —— 只改 Vite 的 `base`：

   ```ts
   const appPrefix = process.env.WC_APP_PREFIX ?? '';
   export default defineConfig(async () => ({
     base: appPrefix ? `${appPrefix}/` : '/',
     // …原有 plugins 不动
   }));
   ```

2. `package.json` —— 加一条构建脚本（前缀是**构建期**常量，忘了带就整站资源 404）：

   ```json
   "build:portal": "WC_APP_PREFIX=/apps/我的项目 npm run build && node ../../scripts/precompress.js dist/client"
   ```

3. `app/layout.tsx` —— `base` 管不到的三处自己拼：`<head>` 里手写的 `<link>`、
   社交卡片的绝对 URL（`og:image` / `og:url`）、图标链接。

   ```tsx
   const basePath = process.env.WC_APP_PREFIX ?? '';
   // <link rel="stylesheet" href={`${basePath}/fonts/…`} />
   // icons: { icon: `${basePath}/icon.svg` }
   // images: [{ url: `${origin}${basePath}/og.png` }]
   ```

## 两个坑（都排查过，别重复踩）

1. **不要用 Next 的 `basePath`。** vinext 0.0.50 的 Node 生产服务器在 App Router 分支
   用 `pathname.startsWith("/assets/")` 这条不认 `basePath` 的捷径直出打包产物
   （`node_modules/vinext/dist/server/prod-server.js`）。配了 `basePath` 之后 HTML、
   `public/` 资源、`app/icon.svg` 都正常（走 RSC handler，handler 认 basePath），
   但**所有 JS/CSS chunk 404**：HTML 200 而页面白屏，很难一眼看出原因。
2. **预压缩必须自己做。** `vinext build` 不生成 `.br/.gz`（Cloudflare 在边缘压），
   而 `vinext start` 的静态直出只会**发同名副本、自己不压**。600KB 的 three.js chunk
   压完约 124KB，所以 `build:portal` 里带上 `scripts/precompress.js`。

## 两条纪律

1. **`npm test` 会顺带跑一次不带前缀的 `npm run build`**——跑完测试要重新
   `npm run build:portal`，否则门户上的资源全 404。
2. 换完构建 `touch project.json` 让网关重启该项目（清掉旧的 `dist/server` 模块缓存
   与静态文件表），再刷新门户页。

## 运行期

网关启动 `vinext start`（读环境变量 `PORT`，纯 Node HTTP 服务，不需要 wrangler /
workerd），日志写 `logs/<项目>.log`，崩溃指数退避重启，网关退出时整组回收。
Node 用仓库自带的 `.node/bin`（系统 node 20.x 跑不了 vinext：
`does not provide an export named 'glob'`）。
