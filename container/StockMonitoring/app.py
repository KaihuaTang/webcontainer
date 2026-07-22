"""股票信号监控 WebUI。

- 主页：顶部「今日信号」（买入/卖出提醒 + 财报临近），下方为时间轴（最新在上）；
- 个股页：价格曲线 + 买卖节点标记 + 逐日事件与财报前瞻；
- 管理页 /manage：只读展示监控清单与每日更新计划；全部配置集中在本地
  stocks.json（schedule / stocks / macro_watch 三段），不提供在线修改接口；
- 更新只按每日计划自动执行，不提供手动触发接口（防恶意刷新）。

遵循 webcontainer 网关的子路径前缀约定：ProxyFix(x_prefix) + url_for +
模板注入 window.APP_ROOT。直连调试：python app.py（端口取 $PORT，默认 3009）。
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from updater import update as updater

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DAILY_DIR = DATA_DIR / "daily"
STOCKS_DIR = DATA_DIR / "stocks"
STATUS_PATH = DATA_DIR / "status.json"
STOCKS_CONFIG = BASE_DIR / "stocks.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stockmon")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

update_lock = threading.Lock()
update_running = threading.Event()


# ---- 配置读取（只读；修改需直接编辑 stocks.json） ------------------------

def load_config() -> dict:
    return json.loads(STOCKS_CONFIG.read_text(encoding="utf-8"))


def load_watchlist() -> list[dict]:
    return load_config()["stocks"]


def watchlist_map() -> dict[str, dict]:
    return {s["symbol"]: s for s in load_watchlist()}


def read_json(path: Path, fallback=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def list_daily(limit: int | None = None) -> list[dict]:
    """按日期倒序返回每日汇总。"""
    if not DAILY_DIR.is_dir():
        return []
    files = sorted(DAILY_DIR.glob("????-??-??.json"), reverse=True)
    if limit:
        files = files[:limit]
    days = []
    for f in files:
        day = read_json(f)
        if isinstance(day, dict):
            days.append(day)
    return days


def schedule_info() -> dict:
    settings = updater.load_settings()
    return {
        "update_time": settings["update_time"],
        "timezone": settings["timezone"],
        "label": updater.update_time_str(),
    }


# ---- 页面 ---------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", watchlist=load_watchlist())


@app.route("/stock/<symbol>")
def stock_page(symbol):
    meta = watchlist_map().get(symbol)
    if meta is None:
        # 已移出监控列表但仍有历史数据的标的，保留只读访问
        if (STOCKS_DIR / f"{symbol}.json").is_file():
            meta = {"symbol": symbol, "name": symbol, "focus": "（该标的已移出监控列表，仅展示历史数据）"}
        else:
            abort(404)
    return render_template("stock.html", stock=meta)


@app.route("/manage")
def manage_page():
    return render_template("manage.html")


# ---- 数据 API -----------------------------------------------------------

@app.route("/api/timeline")
def api_timeline():
    limit = min(int(request.args.get("limit", 45)), 365)
    return jsonify({
        "status": read_json(STATUS_PATH, {}),
        "running": update_running.is_set(),
        "schedule": schedule_info(),
        "days": list_daily(limit),
    })


@app.route("/api/stock/<symbol>")
def api_stock(symbol):
    meta = watchlist_map().get(symbol) or {"symbol": symbol, "name": symbol, "focus": ""}
    stock_data = read_json(STOCKS_DIR / f"{symbol}.json", {}) or {}
    if not stock_data and symbol not in watchlist_map():
        abort(404)

    entries = []
    for day in list_daily(120):
        item = (day.get("stocks") or {}).get(symbol)
        if item:
            entries.append({"date": day.get("date"), **item})

    return jsonify({
        "meta": meta,
        "series": stock_data.get("series", []),
        "events": stock_data.get("events", []),
        "currency": stock_data.get("currency", "USD"),
        "entries": entries,
    })


@app.route("/api/status")
def api_status():
    return jsonify({
        "running": update_running.is_set(),
        "status": read_json(STATUS_PATH, {}),
        "schedule": schedule_info(),
    })


# ---- 管理 API（只读；配置修改需直接编辑 stocks.json） --------------------

@app.route("/api/settings")
def api_settings():
    # 只返回展示所需内容，不暴露服务器路径等部署信息
    config = load_config()
    return jsonify({
        "schedule": schedule_info(),
        "watchlist": config["stocks"],
        "macro_watch": config.get("macro_watch", []),
    })


# ---- 更新调度（仅定时，无手动触发） --------------------------------------

def _run_update_safe() -> None:
    if not update_lock.acquire(blocking=False):
        return
    update_running.set()
    try:
        updater.run_update()
    except Exception:
        log.exception("更新任务执行失败")
    finally:
        update_running.clear()
        update_lock.release()


def _scheduler_loop() -> None:
    last_run_day = None
    log.info("定时更新：每天 %s（编辑 stocks.json 的 schedule 段修改）", updater.update_time_str())
    while True:
        time.sleep(30)
        try:
            now = datetime.now(updater.update_timezone())
        except Exception:
            continue
        hh, mm = updater.update_time()
        day_key = now.strftime("%Y-%m-%d")
        if now.hour == hh and now.minute == mm and last_run_day != day_key:
            last_run_day = day_key
            log.info("到达计划时刻 %s %02d:%02d，开始每日更新", day_key, hh, mm)
            _run_update_safe()


def start_scheduler() -> None:
    threading.Thread(target=_scheduler_loop, daemon=True, name="stockmon-scheduler").start()


# ---- 入口 ---------------------------------------------------------------

DAILY_DIR.mkdir(parents=True, exist_ok=True)
STOCKS_DIR.mkdir(parents=True, exist_ok=True)

if not os.environ.get("STOCK_NO_SCHEDULER"):
    start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3009))
    app.run(host="127.0.0.1", port=port, threaded=True)
