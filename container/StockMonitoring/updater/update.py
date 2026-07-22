"""每日更新流程：抓取行情 → Claude 联网分析买卖信号 → 写入时间轴数据。

可独立运行：
    python updater/update.py                # 完整更新（行情 + AI 分析）
    python updater/update.py --prices-only  # 仅刷新行情曲线

环境变量：
    STOCK_UPDATE_TIME   每日自动更新时刻，默认 07:30
    STOCK_CLAUDE_BIN    claude CLI 路径，默认自动查找
    STOCK_CLAUDE_TIMEOUT  分析超时秒数，默认 1800
    STOCK_SMTP_HOST/PORT/USER/PASS/FROM/TO  可选：出现可信买卖信号时发送中文邮件
"""

import json
import logging
import os
import re
import shutil
import smtplib
import subprocess
import sys
import urllib.request
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DAILY_DIR = DATA_DIR / "daily"
STOCKS_DIR = DATA_DIR / "stocks"
STATUS_PATH = DATA_DIR / "status.json"
PROMPT_TEMPLATE = Path(__file__).resolve().parent / "prompt_template.md"

VALID_SIGNALS = {"none", "watch", "buy", "sell", "trim"}
SIGNAL_LABEL = {"buy": "买入", "sell": "卖出", "trim": "减仓", "watch": "关注", "none": ""}

# 默认：美东 09:00，美股开盘前半小时（zoneinfo 自动处理夏令时）
DEFAULT_UPDATE_TIME = "09:00"
DEFAULT_TIMEZONE = "America/New_York"
TZ_LABELS = {"America/New_York": "美东", "Asia/Shanghai": "北京", "UTC": "UTC"}

log = logging.getLogger("stockmon.updater")


def load_settings() -> dict:
    """调度设置：stocks.json 的 schedule 段 > 环境变量 > 默认值（每次读文件，改后即生效）。"""
    settings = {
        "update_time": os.environ.get("STOCK_UPDATE_TIME", DEFAULT_UPDATE_TIME),
        "timezone": os.environ.get("STOCK_UPDATE_TZ", DEFAULT_TIMEZONE),
    }
    try:
        raw = json.loads((BASE_DIR / "stocks.json").read_text(encoding="utf-8")).get("schedule")
        if isinstance(raw, dict):
            settings.update({k: str(v) for k, v in raw.items() if k in settings})
    except (OSError, json.JSONDecodeError):
        pass
    return settings


def update_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(load_settings()["timezone"])
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def update_time() -> tuple[int, int]:
    raw = load_settings()["update_time"]
    try:
        hh, mm = raw.strip().split(":")
        return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except ValueError:
        return 9, 0


def update_time_str() -> str:
    settings = load_settings()
    label = TZ_LABELS.get(settings["timezone"], settings["timezone"])
    return "%s %02d:%02d" % (label, *update_time())


# ---- 行情抓取 -----------------------------------------------------------

def fetch_chart(yahoo_symbol: str, range_: str = "1y") -> dict | None:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
           f"?range={range_}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        result = payload["chart"]["result"][0]
        stamps = result.get("timestamp") or []
        closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
        series = []
        for ts, close in zip(stamps, closes):
            if close is None:
                continue
            series.append([datetime.fromtimestamp(ts).strftime("%Y-%m-%d"), round(float(close), 3)])
        if not series:
            return None
        return {"currency": result.get("meta", {}).get("currency", "USD"), "series": series}
    except Exception as exc:  # 网络/结构异常都不应中断整体流程
        log.warning("行情抓取失败 %s: %s", yahoo_symbol, exc)
        return None


def refresh_prices(watchlist: list[dict]) -> dict[str, dict]:
    """更新 data/stocks/<sym>.json 的 series，保留既有 events。返回最新报价摘要。"""
    STOCKS_DIR.mkdir(parents=True, exist_ok=True)
    quotes = {}
    for stock in watchlist:
        sym = stock["symbol"]
        path = STOCKS_DIR / f"{sym}.json"
        existing = {}
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}

        chart = fetch_chart(stock.get("yahoo", sym))
        if chart:
            existing["symbol"] = sym
            existing["currency"] = chart["currency"]
            existing["series"] = chart["series"]
            existing.setdefault("events", [])
            path.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")

        series = existing.get("series") or []
        if series:
            last_date, last_close = series[-1]
            prev_close = series[-2][1] if len(series) > 1 else last_close
            change = (last_close - prev_close) / prev_close * 100 if prev_close else 0.0
            quotes[sym] = {
                "price": last_close,
                "change_pct": round(change, 2),
                "currency": existing.get("currency", "USD"),
                "price_date": last_date,
            }
        else:
            quotes[sym] = {"price": None, "change_pct": None, "currency": "USD", "price_date": None}
    return quotes


# ---- Claude 联网分析 ----------------------------------------------------

def resolve_claude_bin() -> str | None:
    configured = os.environ.get("STOCK_CLAUDE_BIN")
    if configured:
        return configured if shutil.which(configured) or Path(configured).is_file() else None
    return shutil.which("claude")


def build_prompt(watchlist: list[dict], macro_watch: list[str],
                 quotes: dict[str, dict], today: str) -> str:
    lines = []
    example = []
    for stock in watchlist:
        q = quotes.get(stock["symbol"], {})
        price = q.get("price")
        quote_txt = (f"最新收盘 {price} {q.get('currency', '')}（{q.get('price_date', '?')}，"
                     f"日涨跌 {q.get('change_pct', '?')}%）") if price else "行情暂缺"
        focus = stock.get("focus") or "常规基本面与消息面"
        lines.append(f"- {stock['symbol']}（{stock['name']}，关注点：{focus}）：{quote_txt}")
        example.append(f'    "{stock["symbol"]}": '
                       '{"summary": "…", "signal": "none", "signal_brief": "", '
                       '"signal_reason": "", "events": []}')
    template = PROMPT_TEMPLATE.read_text(encoding="utf-8")
    return (template
            .replace("{{DATE}}", today)
            .replace("{{STOCK_LINES}}", "\n".join(lines))
            .replace("{{STOCK_JSON_EXAMPLE}}", ",\n".join(example))
            .replace("{{MACRO_WATCH}}", "、".join(macro_watch)))


def extract_json(text: str) -> dict | None:
    """从模型输出中提取最外层 JSON 对象。"""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fence.group(1)] if fence else []
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def run_claude_analysis(prompt: str) -> tuple[dict | None, str]:
    claude_bin = resolve_claude_bin()
    if not claude_bin:
        return None, "未找到 claude CLI（可用 STOCK_CLAUDE_BIN 指定路径）"

    timeout = int(os.environ.get("STOCK_CLAUDE_TIMEOUT", "1800"))
    cmd = [
        claude_bin, "-p", prompt,
        "--output-format", "text",
        "--allowedTools", "WebSearch", "WebFetch",
    ]
    log.info("调用 Claude 进行联网分析（超时 %ds）…", timeout)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(BASE_DIR), env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return None, f"分析超时（>{timeout}s）"
    except OSError as exc:
        return None, f"claude 启动失败：{exc}"

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        return None, f"claude 退出码 {proc.returncode}：{tail}"

    data = extract_json(proc.stdout or "")
    if data is None:
        return None, "未能从分析输出中解析出 JSON"
    return data, "ok"


def normalize_analysis(data: dict, watchlist: list[dict]) -> dict:
    """校验并裁剪模型输出，防御缺字段/非法信号值。"""
    stocks_out = {}
    raw_stocks = data.get("stocks") if isinstance(data.get("stocks"), dict) else {}
    for stock in watchlist:
        sym = stock["symbol"]
        item = raw_stocks.get(sym) if isinstance(raw_stocks.get(sym), dict) else {}
        signal = str(item.get("signal", "none")).lower()
        if signal not in VALID_SIGNALS:
            signal = "none"
        events = item.get("events") if isinstance(item.get("events"), list) else []
        stocks_out[sym] = {
            "summary": str(item.get("summary", "")).strip()[:300],
            "signal": signal,
            "signal_brief": "" if signal == "none"
                            else str(item.get("signal_brief", "")).strip()[:40],
            "signal_reason": str(item.get("signal_reason", "")).strip()[:300],
            "events": [str(e).strip()[:200] for e in events][:5],
        }
        earnings = item.get("earnings")
        if isinstance(earnings, dict):
            date_str = str(earnings.get("date", "")).strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
                stocks_out[sym]["earnings"] = {
                    "date": date_str,
                    "forecast": str(earnings.get("forecast", "")).strip()[:300],
                    "opportunities": str(earnings.get("opportunities", "")).strip()[:300],
                    "risks": str(earnings.get("risks", "")).strip()[:300],
                }
    alerts = []
    for alert in data.get("alerts", []) if isinstance(data.get("alerts"), list) else []:
        if not isinstance(alert, dict):
            continue
        sym = str(alert.get("symbol", "")).strip()
        atype = str(alert.get("type", "")).lower()
        if sym and atype in ("buy", "sell", "trim"):
            alerts.append({"symbol": sym, "type": atype,
                           "reason": str(alert.get("reason", "")).strip()[:400]})
    return {
        "market_overview": str(data.get("market_overview", "")).strip()[:800],
        "stocks": stocks_out,
        "alerts": alerts,
    }


# ---- 结果落盘 -----------------------------------------------------------

def append_signal_events(daily: dict) -> None:
    """把 buy/sell/trim 信号写入个股事件表（供个股页在曲线上打点）。"""
    for sym, item in daily.get("stocks", {}).items():
        if item.get("signal") not in ("buy", "sell", "trim"):
            continue
        path = STOCKS_DIR / f"{sym}.json"
        try:
            stock_data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            stock_data = {}
        events = stock_data.setdefault("events", [])
        if any(e.get("date") == daily["date"] for e in events):
            continue
        events.append({
            "date": daily["date"],
            "signal": item["signal"],
            "reason": item.get("signal_reason") or item.get("summary") or "",
        })
        stock_data.setdefault("symbol", sym)
        path.write_text(json.dumps(stock_data, ensure_ascii=False, indent=1), encoding="utf-8")


def send_alert_email(daily: dict) -> str:
    host = os.environ.get("STOCK_SMTP_HOST")
    to_raw = os.environ.get("STOCK_SMTP_TO", "")
    if not host or not to_raw:
        return "未配置 SMTP，跳过邮件"
    alerts = daily.get("alerts") or []
    if not alerts:
        return "无可信信号，不发邮件"

    lines = [f"日期：{daily['date']}", "", "触发信号："]
    for alert in alerts:
        stock = daily["stocks"].get(alert["symbol"], {})
        lines.append(f"· {alert['symbol']} —— {SIGNAL_LABEL.get(alert['type'], alert['type'])}")
        lines.append(f"  理由：{alert.get('reason') or stock.get('signal_reason', '')}")
    lines += ["", "市场概览：", daily.get("market_overview", ""), "", "（股票信号监控 · 自动发送）"]

    msg = MIMEText("\n".join(lines), "plain", "utf-8")
    kinds = "/".join(sorted({SIGNAL_LABEL.get(a["type"], a["type"]) for a in alerts}))
    msg["Subject"] = Header(f"【股票信号】{daily['date']} {kinds}提醒", "utf-8")
    sender = os.environ.get("STOCK_SMTP_FROM") or os.environ.get("STOCK_SMTP_USER", "")
    msg["From"] = sender
    recipients = [t.strip() for t in to_raw.split(",") if t.strip()]
    msg["To"] = ", ".join(recipients)

    port = int(os.environ.get("STOCK_SMTP_PORT", "465"))
    user = os.environ.get("STOCK_SMTP_USER", "")
    password = os.environ.get("STOCK_SMTP_PASS", "")
    try:
        cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
        with cls(host, port, timeout=30) as smtp:
            if port != 465:
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.sendmail(sender, recipients, msg.as_string())
        return f"已发送邮件提醒（{len(alerts)} 条信号）"
    except Exception as exc:
        log.warning("邮件发送失败：%s", exc)
        return f"邮件发送失败：{exc}"


def run_update(prices_only: bool = False) -> dict:
    logging.basicConfig(level=logging.INFO)
    started = datetime.now()
    today = started.strftime("%Y-%m-%d")
    config = json.loads((BASE_DIR / "stocks.json").read_text(encoding="utf-8"))
    watchlist = config["stocks"]

    log.info("刷新行情（%d 支）…", len(watchlist))
    quotes = refresh_prices(watchlist)
    fetched = sum(1 for q in quotes.values() if q.get("price") is not None)

    analysis, reason = (None, "仅行情模式") if prices_only else (
        run_claude_analysis(build_prompt(watchlist, config.get("macro_watch", []), quotes, today))
    )
    if analysis is not None:
        analysis = normalize_analysis(analysis, watchlist)

    daily = {
        "date": today,
        "generated_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "analysis_ok": analysis is not None,
        "market_overview": (analysis or {}).get(
            "market_overview",
            "" if prices_only else f"（今日 AI 分析未完成：{reason}）"),
        "alerts": (analysis or {}).get("alerts", []),
        "stocks": {},
    }
    for stock in watchlist:
        sym = stock["symbol"]
        item = (analysis or {}).get("stocks", {}).get(sym, {})
        daily["stocks"][sym] = {
            "name": stock["name"],
            **quotes.get(sym, {}),
            "summary": item.get("summary", ""),
            "signal": item.get("signal", "none"),
            "signal_brief": item.get("signal_brief", ""),
            "signal_reason": item.get("signal_reason", ""),
            "events": item.get("events", []),
        }

    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    (DAILY_DIR / f"{today}.json").write_text(
        json.dumps(daily, ensure_ascii=False, indent=1), encoding="utf-8")
    append_signal_events(daily)

    mail_note = send_alert_email(daily) if analysis is not None else "分析未完成，不发邮件"
    message = (f"行情 {fetched}/{len(watchlist)} 支；"
               f"AI 分析 {'成功' if analysis is not None else '未完成（' + reason + '）'}；{mail_note}")
    status = {
        "last_run": daily["generated_at"],
        "ok": analysis is not None or prices_only,
        "message": message,
        "duration_sec": round((datetime.now() - started).total_seconds(), 1),
    }
    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("更新完成：%s", message)
    return status


if __name__ == "__main__":
    run_update(prices_only="--prices-only" in sys.argv)
