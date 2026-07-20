"""动态项目接入示例：一个前缀自适应的最小 Flask 应用。

网关约定（适配新后端项目时照抄这几点即可）：
1. 监听 127.0.0.1 + 环境变量 PORT 指定的端口（清单里写死 port 亦可）；
2. 挂 ProxyFix 并开启 x_prefix，服务端即可感知 /apps/<id> 前缀，
   此后 url_for() / redirect(url_for(...)) 生成的地址都自动带前缀；
3. 页面内的资源和接口地址一律用 url_for 或相对路径，不要硬编码以 / 开头。
"""

import os

from flask import Flask, Response, redirect, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
# x_prefix=1：信任网关注入的 X-Forwarded-Prefix（一层代理）
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)


@app.route("/")
def index():
    return Response(f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>动态服务示例</title>
<style>body{{font-family:system-ui,"PingFang SC","Microsoft YaHei",sans-serif;
background:#f3f6fb;color:#1c2b3a;display:flex;flex-direction:column;align-items:center;
justify-content:center;min-height:100vh;margin:0;gap:10px}}
code{{background:#e8f1fa;border-radius:6px;padding:2px 8px}}
a{{color:#11559c}}</style></head>
<body>
<h1>动态服务接入成功 🎉</h1>
<p>当前路径前缀 <code>{request.script_root or "（无，直连模式）"}</code>，
   内部端口 <code>{os.environ.get("PORT", "?")}</code></p>
<p><a href="{url_for("ping")}">用 url_for 生成的链接（自动带前缀）</a></p>
<p><a href="/">← 返回项目主页</a></p>
</body></html>""", content_type="text/html; charset=utf-8")


@app.route("/ping")
def ping():
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
