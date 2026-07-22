# 股票信号监控（StockMonitoring）

每天美股开盘前由 Claude 联网检索监控标的的行情与新闻，双向体检「见顶/卖出」
与「逢低买入」信号，并在**财报发布前一周**给出财报预测与风险/机会提醒。
网页以时间轴呈现每日结论（最新在上），首屏突出**今日该买什么、该卖什么**；
个股页展示价格曲线、历史信号节点与逐日事件。支持电脑与手机浏览器。

- 时间轴主页：`/apps/StockMonitoring/`
- 个股页：`/apps/StockMonitoring/stock/<symbol>`
- 管理页：`/apps/StockMonitoring/manage`（改监控清单、改更新时刻）

## 工作方式

```
每天 美东 09:00（美股开盘前半小时，默认值，可在管理页修改；夏令时自动处理）
  └─ updater/update.py（仅定时触发，页面不提供手动刷新，防恶意刷新）
       ├─ Yahoo Finance 抓取各标的近一年日线（免 key）
       ├─ claude -p <prompt> --allowedTools WebSearch WebFetch
       │    按 updater/prompt_template.md 联网检索当日新闻，输出严格 JSON：
       │    市场概览 + 各标的 summary/signal/events + 财报前瞻（距财报 ≤7 天时）
       ├─ 写 data/daily/<日期>.json（时间轴与今日信号数据）
       ├─ buy/sell/trim 信号追加到 data/stocks/<标的>.json（个股曲线打点）
       └─ （可选）出现可信信号时发送中文邮件
```

## 配置

- **监控清单 / 更新时刻**：直接在网页 `/manage` 修改。
  清单存于 `stocks.json`，调度存于 `data/settings.json`
  （`{"update_time": "09:00", "timezone": "America/New_York"}`）；
  分析提示词中的标的清单与输出示例会随清单**自动生成**，无需改模板。
- **邮件告警（可选）**：配置环境变量后，出现 buy/sell/trim 信号才发信：
  `STOCK_SMTP_HOST` `STOCK_SMTP_PORT`(默认465) `STOCK_SMTP_USER`
  `STOCK_SMTP_PASS` `STOCK_SMTP_FROM` `STOCK_SMTP_TO`(逗号分隔)。
  可写入 `project.json` 的 `runtime.env`（注意勿将密码提交到公开仓库）。
- **分析引擎**：需要本机可用的 `claude` CLI（已登录）；
  可用 `STOCK_CLAUDE_BIN` 指定路径，`STOCK_CLAUDE_TIMEOUT` 调整超时（默认 1800s）。

## 手动运维（服务器侧）

网页端无手动刷新入口；管理员可在服务器上直接执行：

```bash
cd container/StockMonitoring
python3 updater/update.py --prices-only   # 仅刷新行情曲线（秒级）
python3 updater/update.py                 # 完整更新：行情 + AI 联网分析（分钟级）
```

数据均为普通 JSON 文件（`data/`），可直接查看、备份或删除重建；
删除某日 `data/daily/<日期>.json` 即从时间轴移除该天；
从管理页移除的标的历史数据保留，个股页仍可只读访问。

## 免责声明

分析结论由 AI 自动生成，仅供研究参考，不构成投资建议。
