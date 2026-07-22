"""股票信号监控 WebUI。

- 主页：顶部「今日信号」（买入/卖出提醒 + 财报临近），下方为时间轴（最新在上）；
- 个股页：价格曲线 + 买卖节点标记 + 逐日事件与财报前瞻；
- 管理页 /manage：增删监控股票、修改每日更新时刻（默认美东 09:00，开盘前半小时）；
- 更新只按每日计划自动执行，不提供手动触发接口（防恶意刷新）。

遵循 webcontainer 网关的子路径前缀约定：ProxyFix(x_prefix) + url_for +
模板注入 window.APP_ROOT。直连调试：python app.py（端口取 $PORT，默认 3009）。
"""

import json
import logging
import os
import re
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
SETTINGS_PATH = DATA_DIR / "settings.json"
STOCKS_CONFIG = BASE_DIR / "stocks.json"

SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,11}$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
ALLOWED_TIMEZONES = ["America/New_York", "Asia/Shanghai", "UTC"]
MAX_STOCKS = 30

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stockmon")

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1, x_prefix=1)

update_lock = threading.Lock()
update_running = threading.Event()
config_lock = threading.Lock()


# ---- 配置读写 -----------------------------------------------------------

def load_config() -> dict:
    return json.loads(STOCKS_CONFIG.read_text(encoding="utf-8"))


def save_config(config: dict) -> None:
    tmp = STOCKS_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=4), encoding="utf-8")
    os.replace(tmp, STOCKS_CONFIG)


def load_watchlist() -> list[dict]:
    return load_config()["stocks"]


def watchlist_map() -> dict[str, dict]:
    return {s["symbol"]: s for s in load_watchlist()}


def save_settings(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, SETTINGS_PATH)


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


# ---- 管理 API -----------------------------------------------------------

@app.route("/api/settings")
def api_settings():
    return jsonify({
        "schedule": schedule_info(),
        "timezones": ALLOWED_TIMEZONES,
        "watchlist": load_watchlist(),
        "max_stocks": MAX_STOCKS,
    })


@app.route("/api/settings/schedule", methods=["POST"])
def api_settings_schedule():
    data = request.get_json(silent=True) or {}
    update_time = str(data.get("update_time", "")).strip()
    timezone = str(data.get("timezone", "")).strip()
    if not TIME_RE.match(update_time):
        return jsonify({"ok": False, "message": "时间格式应为 HH:MM"}), 400
    if timezone not in ALLOWED_TIMEZONES:
        return jsonify({"ok": False, "message": "不支持的时区"}), 400
    hh, mm = update_time.split(":")
    save_settings({"update_time": f"{int(hh):02d}:{mm}", "timezone": timezone})
    log.info("更新计划已修改：%s %s", timezone, update_time)
    return jsonify({"ok": True, "schedule": schedule_info()})


@app.route("/api/watchlist/add", methods=["POST"])
def api_watchlist_add():
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol", "")).strip().upper()
    yahoo = str(data.get("yahoo", "")).strip() or symbol
    name = str(data.get("name", "")).strip() or symbol
    focus = str(data.get("focus", "")).strip()[:120]

    if not SYMBOL_RE.match(symbol) or not SYMBOL_RE.match(yahoo):
        return jsonify({"ok": False, "message": "代码只能包含字母、数字、点、连字符（≤12 位）"}), 400

    with config_lock:
        config = load_config()
        if any(s["symbol"] == symbol for s in config["stocks"]):
            return jsonify({"ok": False, "message": f"{symbol} 已在监控列表中"}), 400
        if len(config["stocks"]) >= MAX_STOCKS:
            return jsonify({"ok": False, "message": f"监控数量已达上限（{MAX_STOCKS} 支）"}), 400

        # 用行情接口验证代码有效性，同时预取近一年曲线
        chart = updater.fetch_chart(yahoo)
        if chart is None:
            return jsonify({"ok": False, "message":
                            f"Yahoo 上找不到代码 {yahoo} 的行情，请检查（港股需带 .HK 后缀）"}), 400
        STOCKS_DIR.mkdir(parents=True, exist_ok=True)
        (STOCKS_DIR / f"{symbol}.json").write_text(json.dumps({
            "symbol": symbol, "currency": chart["currency"],
            "series": chart["series"], "events": [],
        }, ensure_ascii=False, indent=1), encoding="utf-8")

        config["stocks"].append({"symbol": symbol, "yahoo": yahoo, "name": name,
                                 "market": "HK" if yahoo.endswith(".HK") else "US",
                                 "focus": focus})
        save_config(config)
    log.info("已添加监控标的 %s（%s）", symbol, name)
    return jsonify({"ok": True, "watchlist": load_watchlist(),
                    "message": f"已添加 {symbol}，将从下次定时分析起纳入监控"})


@app.route("/api/watchlist/remove", methods=["POST"])
def api_watchlist_remove():
    symbol = str((request.get_json(silent=True) or {}).get("symbol", "")).strip()
    with config_lock:
        config = load_config()
        remain = [s for s in config["stocks"] if s["symbol"] != symbol]
        if len(remain) == len(config["stocks"]):
            return jsonify({"ok": False, "message": f"列表中没有 {symbol}"}), 404
        if not remain:
            return jsonify({"ok": False, "message": "至少保留一支监控标的"}), 400
        config["stocks"] = remain
        save_config(config)
    log.info("已移除监控标的 %s（历史数据保留）", symbol)
    return jsonify({"ok": True, "watchlist": load_watchlist(),
                    "message": f"已移除 {symbol}（历史数据保留，可随时重新添加）"})


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
    log.info("定时更新：每天 %s（可在 /manage 修改）", updater.update_time_str())
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
