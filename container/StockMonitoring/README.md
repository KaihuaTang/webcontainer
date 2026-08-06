# 股票信号监控（StockMonitoring）

每天美股开盘前由 Claude 联网检索监控标的的行情与新闻，双向体检「见顶/卖出」
与「逢低买入」信号，附加一项「消息—股价反应背离」体检，
并在**财报发布前一周**给出财报预测与风险/机会提醒。
网页以时间轴呈现结论（最新在上，每天盘前、盘后各一张卡片），
首屏突出**今日该买什么、该卖什么**；
个股页展示价格曲线、历史信号节点与逐次跟踪记录。支持电脑与手机浏览器。

- 时间轴主页：`/apps/StockMonitoring/`
- 个股页：`/apps/StockMonitoring/stock/<symbol>`
- 管理页：`/apps/StockMonitoring/manage`（只读查看监控清单与更新计划）

## 工作方式

```
每天两次：美东 08:30（盘前）与 20:30（盘后复盘）
（默认值，改 stocks.json 的 schedule 段；夏令时自动处理）
  └─ updater/update.py（仅定时触发，页面不提供手动刷新，防恶意刷新）
       ├─ Yahoo Finance 抓取各标的近一年日线（免 key）
       ├─ claude -p <prompt> --allowedTools WebSearch WebFetch
       │    按 updater/prompt_template.md 联网检索当日新闻，输出严格 JSON：
       │    市场概览 + 各标的 summary/signal/events + 财报前瞻（距财报 ≤7 天时）
       │    + 消息—股价反应背离 reaction（出现背离时才有该字段）
       ├─ 写 data/daily/<日期>_<时段>.json（时段为 premarket/postmarket，
       │    盘前、盘后各一份，时间轴据此每天展示两张卡片）
       ├─ buy/sell/trim 信号追加到 data/stocks/<标的>.json（个股曲线打点）
       └─ （可选）出现可信信号时发送中文邮件
```

### 判断维度

1. **见顶 / 卖出体检**：行情、估值、宏观地缘与公司消息面是否出现可信的见顶 / 减仓信号；
2. **逢低买入体检**：超跌 + 基本面未变坏 + 催化剂可辨识；
3. **消息—股价反应背离**（附加判断点，不改变前两项原有标准）：回溯最近 1–10 个交易日的
   重大消息，**扣除同期大盘 / 板块涨跌**后，比较「按常理应有的反应」与「实际反应」：
   - 利好落地却涨幅明显低于预期、横盘甚至下跌 → `good_news_weak`（利好不涨）：
     买盘透支、利好已被提前定价，上行空间有限，**偏减仓 / 卖出**；
   - 利空落地却跌幅有限、甚至逆势上涨 → `bad_news_resilient`（利空抗跌）：
     卖压枯竭、估值触底，**偏逢低买入**。

   背离与前两项结论同向时可加强信号（如 watch → buy / trim / sell）；仅有背离这一条
   证据时最多给 watch。结果写入 daily JSON 的 `stocks.<代码>.reaction`
   （消息 / 观察窗口 / 预期反应 / 实际表现 / 解读），首页「今日信号」以「⚖️ 消息背离」
   一条展示、时间轴行加同名标签，个股页逐日跟踪中展开全部四项；邮件提醒中一并附上。

## 配置

- **监控清单 / 更新时刻 / 宏观信号**：全部集中在 `stocks.json` 一个文件，
  网页管理页只读展示，**不支持在线修改**，需在服务器上直接编辑：
  - `schedule`：`{"update_times": ["08:30", "20:30"], "timezone": "America/New_York"}`，
    每天可配多个更新时刻，保存后 30 秒内生效，无需重启；
  - `stocks`：标的数组（symbol / yahoo / name / market / focus），
    增删自下一次定时分析起生效，新标的的行情曲线届时自动抓取；
  - `macro_watch`：AI 每日必查的宏观 / 行业信号清单。
    **每周自动重选**（默认周一美东 08:00，需早于当日盘前分析，`schedule.macro_update` 可调）：
    由 Claude 联网挑选与组合整体最相关的 15 条 + 时事热点 10 条并写回此数组。

  分析提示词中的标的清单与输出示例会随清单**自动生成**，无需改模板。
- **邮件告警（可选）**：配置环境变量后，出现 buy/sell/trim 信号才发信：
  `STOCK_SMTP_HOST` `STOCK_SMTP_PORT`(默认465) `STOCK_SMTP_USER`
  `STOCK_SMTP_PASS` `STOCK_SMTP_FROM` `STOCK_SMTP_TO`(逗号分隔)。
  可写入 `project.json` 的 `runtime.env`（注意勿将密码提交到公开仓库）。
- **联网出海**：本机访问 Anthropic API 与 Yahoo 行情都必须经本地代理（当前
  `127.0.0.1:7895`），否则 `claude` 报 `403 Request not allowed`、行情抓取报 403。
  代理变量固化在 `project.json` 的 `runtime.env` 里，**不依赖网关是从哪个 shell
  启动的**——systemd 启动的服务不继承登录环境，早先靠继承能跑通纯属侥幸。
  - **端口自动探测**：代理端口会跟着代理软件配置漂移（mihomo 的 mixed-port 就从
    7892 换到过 7895），写死的端口一失效就是 `ConnectionRefused`，且只在定时任务里
    炸、往往隔半天才发现。因此每次运行前会实测一遍：环境变量里的端口能用就用，
    不能用再按候选端口逐个探（CONNECT 握手判定，几十毫秒），探到就当场改用并
    打一条 WARNING 日志。`STOCK_PROXY` 可强制指定（设 `none` 表示直连、不探测），
    `STOCK_PROXY_PORTS` 可改候选端口（默认 `7895,7890,7891,7892,7893,7897`）。
    排查用 `python3 updater/update.py --check-proxy`，只打印探测结果不跑分析。
    自动探测是兜底，端口真变了仍建议同步改 `runtime.env`，省掉每次探测。
- **分析引擎**：需要本机可用的 `claude` CLI（已登录）。
  默认固定 `claude-opus-5` + `xhigh` 思考力度，不跟随本机全局配置；
  可用 `STOCK_CLAUDE_MODEL` / `STOCK_CLAUDE_EFFORT` 覆盖，
  `STOCK_CLAUDE_BIN` 指定路径，`STOCK_CLAUDE_TIMEOUT` 调整超时（默认 1800s），
  `STOCK_CLAUDE_ATTEMPTS` 调整失败重试次数（默认 2，设 1 关闭重试）。

## 手动运维（服务器侧）

网页端无手动刷新入口；管理员可在服务器上直接执行：

```bash
cd container/StockMonitoring
python3 updater/update.py --check-proxy   # 只探测本机代理端口（排查「连不上 API」用）
python3 updater/update.py --prices-only   # 仅刷新行情曲线（秒级）
python3 updater/update.py                 # 完整更新：行情 + AI 联网分析（分钟级）
python3 updater/update.py --macro         # 重选宏观信号清单（组合相关 15 + 热点 10）
```

注意：手动完整更新写入的槽位按**当时的美东时钟**判定（<09:30 盘前 / <16:00 盘中 / 其余盘后），
所以补跑一次失败的盘前分析，很可能落到 `intraday` 槽而不是覆盖原来的 `premarket` 文件，
时间轴上会同时出现两条。要真正替换，补跑后手工删掉 `data/daily/<日期>_<原槽位>.json`。

## 分析失败了怎么查

首页出现「今日 AI 分析未完成：…」时：

1. 每次 Claude 调用的**原始输出**都落在 `data/last_claude_analysis.txt`
   （自动重试的那次在 `data/last_claude_analysis_retry1.txt`，宏观任务是 `last_claude_macro*.txt`），
   文件头记录了时间、退出码、模型与输出字符数——先看这个，它是唯一的现场；
2. `data/status.json` 的 `message` 里带了输出长度与结尾片段；
3. 日志见 `logs/StockMonitoring.log`。

分析失败**不会**污染时间轴的信号打点（没有 buy/sell/trim 就不写事件），
只是当日该槽位的文件被写成空壳、首页标「无 AI 分析」。

已知偶发：2026-07-28 盘前出现过一次「跑满 14 分钟、退出码 0、但输出里没有 JSON」，
同样的提示词一小时后重跑即成功，未能复现。现已加自动重试（默认 2 次）兜住这类抖动。

数据均为普通 JSON 文件（`data/`），可直接查看、备份或删除重建；
删除某份 `data/daily/<日期>_<时段>.json` 即从时间轴移除对应卡片；
从清单移除的标的历史数据保留，个股页仍可只读访问。

## 免责声明

分析结论由 AI 自动生成，仅供研究参考，不构成投资建议。
