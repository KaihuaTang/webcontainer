"""每日更新流程：抓取行情 → Claude 联网分析买卖信号 → 写入时间轴数据。
另含每周一次的宏观信号清单重选（写回 stocks.json 的 macro_watch）。

可独立运行：
    python updater/update.py                # 完整更新（行情 + AI 分析）
    python updater/update.py --prices-only  # 仅刷新行情曲线
    python updater/update.py --macro        # 重选宏观信号清单（组合相关 15 + 热点 10）
    python updater/update.py --check-proxy  # 只打印本次探测到的本机代理，不跑任何分析

环境变量：
    STOCK_UPDATE_TIME   每日自动更新时刻，可逗号分隔多个，默认 08:30,20:30
    STOCK_PROXY         强制指定本机代理（如 http://127.0.0.1:7895）；设 none 表示直连，
                        两者都不设时按下面的端口列表自动探测
    STOCK_PROXY_PORTS   自动探测的候选端口，逗号分隔，默认 7895,7890,7891,7892,7893,7897
    STOCK_CLAUDE_BIN    claude CLI 路径，默认自动查找
    STOCK_CLAUDE_MODEL  分析模型，默认 claude-opus-5
    STOCK_CLAUDE_EFFORT 思考力度，默认 xhigh
    STOCK_CLAUDE_TIMEOUT  分析超时秒数，默认 1800
    STOCK_SMTP_HOST/PORT/USER/PASS/FROM/TO  可选：出现可信买卖信号时发送中文邮件
"""

import json
import logging
import os
import re
import shutil
import smtplib
import socket
import subprocess
import sys
import urllib.parse
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
MACRO_STATUS_PATH = DATA_DIR / "macro_status.json"
PROMPT_TEMPLATE = Path(__file__).resolve().parent / "prompt_template.md"
MACRO_PROMPT_TEMPLATE = Path(__file__).resolve().parent / "macro_prompt.md"

VALID_SIGNALS = {"none", "watch", "buy", "sell", "trim"}
SIGNAL_LABEL = {"buy": "买入", "sell": "卖出", "trim": "减仓", "watch": "关注", "none": ""}

# 附加判断点：消息—股价反应背离（利好不涨 → 偏卖出；利空抗跌 → 偏买入）
REACTION_LABEL = {"good_news_weak": "利好不涨（偏卖出）",
                  "bad_news_resilient": "利空抗跌（偏买入）"}
REACTION_FIELD_LIMIT = {"news": 200, "window": 60, "expected": 150, "actual": 200, "reading": 300}

# 默认：美东 08:30 与 20:30，盘前 + 盘后复盘（zoneinfo 自动处理夏令时）
DEFAULT_UPDATE_TIMES = "08:30,20:30"
DEFAULT_TIMEZONE = "America/New_York"
TZ_LABELS = {"America/New_York": "美东", "Asia/Shanghai": "北京", "UTC": "UTC"}

# 每周宏观信号重选：默认周一 08:00。必须早于当日盘前分析且留出富余（重选约 13 分钟），
# 否则调度循环里日更会先跑并同步阻塞十几分钟，重选出的新清单当天用不上。
DEFAULT_MACRO_WEEKDAY = 1
DEFAULT_MACRO_TIME = "08:00"
WEEKDAY_CN = "一二三四五六日"

# Claude 调用固定默认：Opus 5 + extra high 思考力度，不跟随本机全局配置
DEFAULT_CLAUDE_MODEL = "claude-opus-5"
DEFAULT_CLAUDE_EFFORT = "xhigh"

# 运行时段：相对美股常规交易时段归类，一天盘前、盘后各存一份时间轴数据
US_MARKET_TZ = ZoneInfo("America/New_York")
SLOT_LABEL_CN = {"premarket": "盘前", "postmarket": "盘后", "intraday": "盘中"}

log = logging.getLogger("stockmon.updater")


def load_settings() -> dict:
    """调度设置：stocks.json 的 schedule 段 > 环境变量 > 默认值（每次读文件，改后即生效）。"""
    settings = {
        "update_times": os.environ.get("STOCK_UPDATE_TIME", DEFAULT_UPDATE_TIMES),
        "timezone": os.environ.get("STOCK_UPDATE_TZ", DEFAULT_TIMEZONE),
    }
    try:
        raw = json.loads((BASE_DIR / "stocks.json").read_text(encoding="utf-8")).get("schedule")
        if isinstance(raw, dict):
            if isinstance(raw.get("update_times"), list):
                settings["update_times"] = ",".join(str(t) for t in raw["update_times"])
            elif raw.get("update_time"):  # 兼容旧的单时刻字段
                settings["update_times"] = str(raw["update_time"])
            if raw.get("timezone"):
                settings["timezone"] = str(raw["timezone"])
    except (OSError, json.JSONDecodeError):
        pass
    return settings


def update_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(load_settings()["timezone"])
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def save_config(config: dict) -> None:
    path = BASE_DIR / "stocks.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=4), encoding="utf-8")
    os.replace(tmp, path)


def macro_slot() -> tuple[int, int, int]:
    """每周宏观信号重选时刻：返回 (ISO 周几 1-7, 时, 分)，时区同每日调度。"""
    raw = {}
    try:
        raw = (json.loads((BASE_DIR / "stocks.json").read_text(encoding="utf-8"))
               .get("schedule", {}).get("macro_update") or {})
    except (OSError, json.JSONDecodeError):
        pass
    try:
        weekday = max(1, min(7, int(raw.get("weekday", DEFAULT_MACRO_WEEKDAY))))
    except (TypeError, ValueError):
        weekday = DEFAULT_MACRO_WEEKDAY
    try:
        hh, mm = str(raw.get("time", DEFAULT_MACRO_TIME)).strip().split(":")
        return weekday, max(0, min(23, int(hh))), max(0, min(59, int(mm)))
    except ValueError:
        hh, mm = DEFAULT_MACRO_TIME.split(":")
        return weekday, int(hh), int(mm)


def macro_slot_str() -> str:
    weekday, hh, mm = macro_slot()
    tz = load_settings()["timezone"]
    return "每周%s %s %02d:%02d" % (WEEKDAY_CN[weekday - 1], TZ_LABELS.get(tz, tz), hh, mm)


def run_slot() -> str:
    """按美东时钟归类本次运行：盘前（<09:30）/ 盘中 / 盘后（>=16:00）。"""
    et = datetime.now(US_MARKET_TZ)
    minutes = et.hour * 60 + et.minute
    if minutes < 9 * 60 + 30:
        return "premarket"
    if minutes >= 16 * 60:
        return "postmarket"
    return "intraday"


def update_times() -> list[tuple[int, int]]:
    slots = set()
    for part in load_settings()["update_times"].split(","):
        try:
            hh, mm = part.strip().split(":")
            slots.add((max(0, min(23, int(hh))), max(0, min(59, int(mm)))))
        except ValueError:
            continue
    return sorted(slots) or [(9, 0)]


def update_time_str() -> str:
    settings = load_settings()
    label = TZ_LABELS.get(settings["timezone"], settings["timezone"])
    return label + " " + " / ".join("%02d:%02d" % slot for slot in update_times())


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


# ---- 本机代理（端口会漂移，每次运行实测一遍） ---------------------------

# 出海请求（Anthropic API、Yahoo 行情）都得经本机代理，而代理端口会跟着代理软件
# 的配置变——mihomo 的 mixed-port 就从 7892 换到过 7895。端口写死在 project.json
# 的 runtime.env 里，一变就是 ConnectionRefused，且只在定时任务里炸、往往隔半天
# 才发现。所以每次运行前实测一遍：环境变量里那个能用就用，不能用再探候选端口。
# 顺序即优先级：先试本机 mihomo 当前的 mixed-port，再试常见的几个
DEFAULT_PROXY_PORTS = "7895,7890,7891,7892,7893,7897"
PROXY_PROBE_TARGET = "api.anthropic.com:443"
PROXY_PROBE_TIMEOUT = 2.0
PROXY_ENV_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def proxy_alive(url: str) -> bool:
    """向候选代理发一个 CONNECT api.anthropic.com:443，看它是否回 200。

    只握手、不发真实请求，几十毫秒出结果；比单纯 TCP 连通准——端口上换成别的
    服务时 connect 一样成功，只有 CONNECT 能认出对面是不是 HTTP 代理。
    """
    try:
        parts = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
        host, port = parts.hostname, parts.port
    except ValueError:
        return False
    if not host or not port:
        return False
    probe = f"CONNECT {PROXY_PROBE_TARGET} HTTP/1.1\r\nHost: {PROXY_PROBE_TARGET}\r\n\r\n"
    try:
        with socket.create_connection((host, port), PROXY_PROBE_TIMEOUT) as sock:
            sock.settimeout(PROXY_PROBE_TIMEOUT)
            sock.sendall(probe.encode())
            status = sock.recv(64).split(b"\r\n", 1)[0]
    except OSError:
        return False
    return status.startswith(b"HTTP/1.") and b" 200" in status


def detect_proxy() -> str | None:
    """挑一个实测可用的本机 HTTP 代理返回；都不通返回 None。

    顺序：STOCK_PROXY 显式指定 > 现有 *_PROXY 环境变量 > 候选端口逐个探。
    STOCK_PROXY=none 表示这台机器直连，不探测也不改环境变量。
    """
    forced = os.environ.get("STOCK_PROXY")
    if forced is not None:
        forced = forced.strip()
        return None if forced.lower() in ("", "none", "off", "direct") else forced

    candidates = []
    for var in PROXY_ENV_VARS:
        val = (os.environ.get(var) or "").strip()
        if val and val not in candidates:
            candidates.append(val)
    for port in os.environ.get("STOCK_PROXY_PORTS", DEFAULT_PROXY_PORTS).split(","):
        port = port.strip()
        url = f"http://127.0.0.1:{port}"
        if port.isdigit() and url not in candidates:
            candidates.append(url)

    for url in candidates:
        if proxy_alive(url):
            return url
    return None


def apply_proxy() -> str | None:
    """把探测结果写回 os.environ：行情抓取（urllib）与 claude 子进程都吃这一套。"""
    configured = (os.environ.get("HTTPS_PROXY") or "").strip()
    proxy = detect_proxy()
    if proxy is None:
        log.warning("未探测到可用的本机代理（试过 %s），出海请求可能失败",
                    configured or "候选端口 " + os.environ.get("STOCK_PROXY_PORTS", DEFAULT_PROXY_PORTS))
        return None
    if proxy != configured:
        log.warning("本机代理改用 %s（原 %s 不通；改 project.json 的 runtime.env 可免去这次探测）",
                    proxy, configured or "未设置")
    else:
        log.info("本机代理 %s 可用", proxy)
    for var in PROXY_ENV_VARS:
        os.environ[var] = proxy
    # ALL_PROXY 可能是 socks5h://（mihomo 的 mixed-port 两种协议同一个端口），
    # 只跟着换 host:port，别把 scheme 也改掉。
    netloc = proxy.split("://", 1)[-1].strip("/")
    for var in ("ALL_PROXY", "all_proxy"):
        old = os.environ.get(var)
        if old:
            scheme = old.split("://", 1)[0] if "://" in old else "http"
            os.environ[var] = f"{scheme}://{netloc}"
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1,::1")
    return proxy


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


def iter_brace_blocks(text: str):
    """按大括号配对扫出文本里每一段完整的 {...}（字符串内的括号与转义不计）。

    比「第一个 { 到最后一个 }」稳：模型在 JSON 前后写了说明文字、
    或正文里出现零散花括号时，前者会把整段一起吞进去导致解析失败。
    """
    depth, start, in_str, esc = 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0:
                yield text[start:i + 1]
                start = -1


def extract_json(text: str) -> dict | None:
    """从模型输出中提取 JSON 对象；多个候选时取能解析且最长的那个。"""
    candidates = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(.+?)\s*```", text, re.S)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    candidates.extend(iter_brace_blocks(text))

    best = None
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        # 取最长的那个：正文里可能还夹着示例用的小对象
        if isinstance(data, dict) and (best is None or len(cand) > len(best[0])):
            best = (cand, data)
    return best[1] if best else None


def save_claude_raw(kind: str, stdout: str, stderr: str, returncode: int, note: str) -> Path:
    """把本次 Claude 调用的原始输出落盘，供解析失败时排查（只保留最近一次）。"""
    path = DATA_DIR / f"last_claude_{kind}.txt"
    header = (f"# {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  退出码={returncode}  "
              f"stdout={len(stdout)} 字符  stderr={len(stderr)} 字符\n"
              f"# {note}\n{'=' * 60}\n")
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(header + stdout + ("\n--- stderr ---\n" + stderr if stderr else ""),
                        encoding="utf-8")
    except OSError as exc:
        log.warning("原始输出落盘失败：%s", exc)
    return path


def call_claude_once(claude_bin: str, prompt: str, kind: str,
                     attempt: int) -> tuple[dict | None, str]:
    timeout = int(os.environ.get("STOCK_CLAUDE_TIMEOUT", "1800"))
    model = os.environ.get("STOCK_CLAUDE_MODEL", DEFAULT_CLAUDE_MODEL)
    effort = os.environ.get("STOCK_CLAUDE_EFFORT", DEFAULT_CLAUDE_EFFORT)
    cmd = [
        claude_bin, "-p", prompt,
        "--output-format", "text",
        "--model", model,
        "--allowedTools", "WebSearch", "WebFetch",
    ]
    env = os.environ.copy()
    env["CLAUDE_EFFORT"] = effort
    log.info("调用 Claude 进行联网分析（第 %d 次，model=%s，effort=%s，超时 %ds）…",
             attempt, model, effort, timeout)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=str(BASE_DIR), env=env,
        )
    except subprocess.TimeoutExpired:
        return None, f"分析超时（>{timeout}s）"
    except OSError as exc:
        return None, f"claude 启动失败：{exc}"

    stdout, stderr = proc.stdout or "", proc.stderr or ""
    # 无论成败都留一份原始输出：解析失败时这是唯一的排查线索。
    # 重试写到单独的文件名，避免第二次把第一次的失败现场覆盖掉。
    raw_path = save_claude_raw(kind if attempt == 1 else f"{kind}_retry{attempt - 1}",
                               stdout, stderr, proc.returncode,
                               f"model={model} effort={effort} attempt={attempt}")

    if proc.returncode != 0:
        tail = (stderr or stdout).strip()[-400:]
        return None, f"claude 退出码 {proc.returncode}：{tail}"

    data = extract_json(stdout)
    if data is None:
        tail = stdout.strip()[-200:].replace("\n", " ")
        return None, (f"未能从分析输出中解析出 JSON（输出 {len(stdout)} 字符，"
                      f"原文见 {raw_path.relative_to(BASE_DIR)}；结尾：…{tail}）")
    return data, "ok"


def run_claude_analysis(prompt: str, kind: str = "analysis") -> tuple[dict | None, str]:
    """调用 Claude 做联网分析；失败自动重试。

    2026-07-28 出现过一次「跑满 14 分钟、退出码 0、但输出里没有 JSON」的偶发失败，
    同样的提示词隔一小时重跑即成功。重试只在失败时触发，正常日子不增加任何开销；
    次数按 STOCK_CLAUDE_ATTEMPTS 配置（默认 2），设 1 可关掉。
    """
    claude_bin = resolve_claude_bin()
    if not claude_bin:
        return None, "未找到 claude CLI（可用 STOCK_CLAUDE_BIN 指定路径）"

    try:
        attempts = max(1, int(os.environ.get("STOCK_CLAUDE_ATTEMPTS", "2")))
    except ValueError:
        attempts = 2

    reason = ""
    for attempt in range(1, attempts + 1):
        data, reason = call_claude_once(claude_bin, prompt, kind, attempt)
        if data is not None:
            return data, "ok" if attempt == 1 else f"ok（第 {attempt} 次尝试成功）"
        if attempt < attempts:
            log.warning("分析失败，将重试：%s", reason)
    if attempts > 1:
        reason = f"{reason}；已重试 {attempts - 1} 次仍失败"
    return None, reason


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
        reaction = item.get("reaction")
        if isinstance(reaction, dict):
            rtype = str(reaction.get("type", "")).strip().lower()
            if rtype in REACTION_LABEL:
                stocks_out[sym]["reaction"] = {
                    "type": rtype,
                    **{k: str(reaction.get(k, "")).strip()[:limit]
                       for k, limit in REACTION_FIELD_LIMIT.items()},
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
        reaction = stock.get("reaction")
        if reaction:
            lines.append(f"  消息背离：{REACTION_LABEL.get(reaction['type'], '')}"
                         f"｜{reaction.get('news', '')} → {reaction.get('actual', '')}")

    alerted = {a["symbol"] for a in alerts}
    others = [(sym, s["reaction"]) for sym, s in daily["stocks"].items()
              if s.get("reaction") and sym not in alerted]
    if others:
        lines += ["", "消息—股价反应背离（附加观察，未构成操作信号）："]
        for sym, reaction in others:
            lines.append(f"· {sym} —— {REACTION_LABEL.get(reaction['type'], '')}"
                         f"：{reaction.get('reading') or reaction.get('actual', '')}")

    lines += ["", "市场概览：", daily.get("market_overview", ""), "", "（股票信号监控 · 自动发送）"]

    msg = MIMEText("\n".join(lines), "plain", "utf-8")
    kinds = "/".join(sorted({SIGNAL_LABEL.get(a["type"], a["type"]) for a in alerts}))
    slot_label = SLOT_LABEL_CN.get(daily.get("slot"), "")
    msg["Subject"] = Header(f"【股票信号】{daily['date']} {slot_label}{kinds}提醒", "utf-8")
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
    apply_proxy()
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

    slot = run_slot()
    daily = {
        "date": today,
        "slot": slot,
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
        entry = {
            "name": stock["name"],
            **quotes.get(sym, {}),
            "summary": item.get("summary", ""),
            "signal": item.get("signal", "none"),
            "signal_brief": item.get("signal_brief", ""),
            "signal_reason": item.get("signal_reason", ""),
            "events": item.get("events", []),
        }
        # 可选字段，仅在模型给出时落盘：财报前瞻、消息—股价反应背离
        for key in ("earnings", "reaction"):
            if isinstance(item.get(key), dict):
                entry[key] = item[key]
        daily["stocks"][sym] = entry

    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    (DAILY_DIR / f"{today}_{slot}.json").write_text(
        json.dumps(daily, ensure_ascii=False, indent=1), encoding="utf-8")
    append_signal_events(daily)

    mail_note = send_alert_email(daily) if analysis is not None else "分析未完成，不发邮件"
    message = (f"{SLOT_LABEL_CN.get(slot, '')}更新：行情 {fetched}/{len(watchlist)} 支；"
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


# ---- 每周宏观信号重选 ----------------------------------------------------

def run_macro_update() -> dict:
    """联网重选 macro_watch：组合最相关 15 条 + 时事热点 10 条，写回 stocks.json。"""
    logging.basicConfig(level=logging.INFO)
    apply_proxy()
    started = datetime.now()
    config = json.loads((BASE_DIR / "stocks.json").read_text(encoding="utf-8"))
    watchlist = config["stocks"]

    lines = [f"- {s['symbol']}（{s['name']}，关注点：{s.get('focus') or '常规基本面与消息面'}）"
             for s in watchlist]
    prompt = (MACRO_PROMPT_TEMPLATE.read_text(encoding="utf-8")
              .replace("{{DATE}}", started.strftime("%Y-%m-%d"))
              .replace("{{COUNT}}", str(len(watchlist)))
              .replace("{{STOCK_LINES}}", "\n".join(lines))
              .replace("{{CURRENT}}", "、".join(config.get("macro_watch", []))))

    log.info("重选宏观信号清单（组合 %d 支）…", len(watchlist))
    data, reason = run_claude_analysis(prompt, kind="macro")

    ok, message = False, reason
    if data is not None:
        portfolio = [str(s).strip()[:30] for s in data.get("portfolio_signals", [])
                     if str(s).strip()][:15]
        hot = [str(s).strip()[:30] for s in data.get("hot_signals", []) if str(s).strip()][:10]
        merged = list(dict.fromkeys(portfolio + hot))
        if len(merged) >= 15:
            config["macro_watch"] = merged
            save_config(config)
            ok = True
            message = f"组合相关 {len(portfolio)} 条 + 时事热点 {len(hot)} 条，去重后共 {len(merged)} 条"
        else:
            message = f"产出过少（{len(merged)} 条），保留原清单"

    status = {
        "last_run": started.strftime("%Y-%m-%d %H:%M:%S"),
        "ok": ok,
        "message": message,
        "duration_sec": round((datetime.now() - started).total_seconds(), 1),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MACRO_STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    log.info("宏观信号重选完成：%s", message)
    return status


if __name__ == "__main__":
    if "--check-proxy" in sys.argv:
        logging.basicConfig(level=logging.INFO)
        print(detect_proxy() or "未探测到可用代理")
    elif "--macro" in sys.argv:
        run_macro_update()
    else:
        run_update(prices_only="--prices-only" in sys.argv)
