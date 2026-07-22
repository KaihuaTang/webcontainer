/* 个股页：ECharts 价格曲线 + 买卖信号标记 + 逐日跟踪列表。 */

(function () {
    "use strict";

    var head = document.getElementById("stock-head");
    var symbol = head.dataset.symbol;
    var entriesEl = document.getElementById("entries");

    var css = getComputedStyle(document.body);
    var COLOR = {
        accent: css.getPropertyValue("--accent").trim() || "#d97757",
        text: css.getPropertyValue("--text").trim() || "#1f1e1b",
        dim: css.getPropertyValue("--text-dim").trim() || "#75716a",
        line: css.getPropertyValue("--line").trim() || "#e0dcd0",
        card: css.getPropertyValue("--card-bg").trim() || "#faf9f5",
        buy: css.getPropertyValue("--buy").trim() || "#d43d33",
        sell: css.getPropertyValue("--sell").trim() || "#0f9d6b",
    };
    var SIGNAL_CHAR = { buy: "买", sell: "卖", trim: "减" };

    function nearestClose(series, date) {
        var close = null;
        for (var i = 0; i < series.length; i++) {
            if (series[i][0] > date) break;
            close = series[i];
        }
        return close; // [date, close] 当日或之前最近交易日
    }

    function renderQuote(data) {
        var series = data.series || [];
        if (!series.length) return;
        var last = series[series.length - 1];
        var prev = series.length > 1 ? series[series.length - 2] : last;
        var pct = prev[1] ? (last[1] - prev[1]) / prev[1] * 100 : 0;
        document.getElementById("quote-price").textContent =
            window.fmtPrice(last[1], data.currency);
        var chgEl = document.getElementById("quote-chg");
        chgEl.innerHTML = window.fmtChg(Math.round(pct * 100) / 100) +
            ' <span style="color:var(--text-dim);font-weight:400">' + last[0] + "</span>";
    }

    function renderChart(data) {
        var series = data.series || [];
        var el = document.getElementById("chart");
        if (!series.length) {
            el.innerHTML = '<div class="empty">暂无行情数据，触发一次更新后即可显示。</div>';
            return;
        }
        var dates = series.map(function (p) { return p[0]; });
        var closes = series.map(function (p) { return p[1]; });

        var marks = [];
        (data.events || []).forEach(function (ev) {
            var at = nearestClose(series, ev.date);
            if (!at) return;
            var color = ev.signal === "buy" ? COLOR.buy : COLOR.sell;
            marks.push({
                name: window.SIGNAL_LABEL[ev.signal] || ev.signal,
                coord: [at[0], at[1]],
                value: SIGNAL_CHAR[ev.signal] || "?",
                reason: ev.reason || "",
                date: ev.date,
                itemStyle: { color: color },
            });
        });

        var chart = echarts.init(el);
        chart.setOption({
            grid: { left: 8, right: 14, top: 18, bottom: 44, containLabel: true },
            tooltip: {
                trigger: "axis",
                backgroundColor: COLOR.card,
                borderColor: COLOR.line,
                textStyle: { color: COLOR.text, fontSize: 12 },
                valueFormatter: function (v) { return window.fmtPrice(v, data.currency); },
            },
            xAxis: {
                type: "category",
                data: dates,
                boundaryGap: false,
                axisLine: { lineStyle: { color: COLOR.line } },
                axisLabel: { color: COLOR.dim, fontSize: 11 },
            },
            yAxis: {
                type: "value",
                scale: true,
                splitLine: { lineStyle: { color: COLOR.line, opacity: .6 } },
                axisLabel: { color: COLOR.dim, fontSize: 11 },
            },
            dataZoom: [
                { type: "inside", start: Math.max(0, 100 - 13000 / series.length), end: 100 },
                { type: "slider", height: 18, bottom: 10,
                  borderColor: COLOR.line, textStyle: { color: COLOR.dim, fontSize: 10 } },
            ],
            series: [{
                type: "line",
                data: closes,
                symbol: "none",
                lineStyle: { color: COLOR.accent, width: 2 },
                areaStyle: {
                    color: {
                        type: "linear", x: 0, y: 0, x2: 0, y2: 1,
                        colorStops: [
                            { offset: 0, color: COLOR.accent + "33" },
                            { offset: 1, color: COLOR.accent + "00" },
                        ],
                    },
                },
                markPoint: {
                    symbol: "pin",
                    symbolSize: 34,
                    label: { color: "#fff", fontSize: 12, fontWeight: 600 },
                    data: marks,
                    tooltip: {
                        trigger: "item",
                        formatter: function (params) {
                            var d = params.data || {};
                            return "<b>" + params.name + " · " + (d.date || "") + "</b><br>" +
                                window.escapeHtml(d.reason || "");
                        },
                    },
                },
            }],
        });
        window.addEventListener("resize", function () { chart.resize(); });
    }

    function renderEntries(data) {
        var entries = data.entries || [];
        if (!entries.length) {
            entriesEl.innerHTML =
                '<div class="empty">暂无逐日跟踪记录，每天定时分析后会在这里累积。</div>';
            return;
        }
        entriesEl.innerHTML = entries.map(function (e) {
            var events = (e.events || []).map(function (ev) {
                return "<li>" + window.escapeHtml(ev) + "</li>";
            }).join("");
            var earnings = "";
            if (e.earnings && e.earnings.date) {
                earnings =
                    '<div class="earnings-box">' +
                        '<div class="eb-title">📅 财报前瞻 · ' +
                            window.escapeHtml(e.earnings.date) + "</div>" +
                        (e.earnings.forecast
                            ? '<p><b>预测：</b>' + window.escapeHtml(e.earnings.forecast) + "</p>" : "") +
                        (e.earnings.opportunities
                            ? '<p class="eb-opp"><b>机会：</b>' +
                              window.escapeHtml(e.earnings.opportunities) + "</p>" : "") +
                        (e.earnings.risks
                            ? '<p class="eb-risk"><b>风险：</b>' +
                              window.escapeHtml(e.earnings.risks) + "</p>" : "") +
                    "</div>";
            }
            return (
                '<div class="entry">' +
                    '<div class="entry-head">' +
                        '<span class="entry-date">' + window.escapeHtml(e.date) + "</span>" +
                        window.signalPill(e.signal) +
                        '<span class="entry-quote">' + window.fmtPrice(e.price, e.currency) +
                            " " + window.fmtChg(e.change_pct) + "</span>" +
                    "</div>" +
                    (e.summary ? '<p class="entry-summary">' + window.escapeHtml(e.summary) + "</p>" : "") +
                    (e.signal_reason
                        ? '<p class="entry-reason">信号依据：' + window.escapeHtml(e.signal_reason) + "</p>" : "") +
                    earnings +
                    (events ? '<ul class="entry-events">' + events + "</ul>" : "") +
                "</div>"
            );
        }).join("");
    }

    window.fetchJSON(window.API + "/api/stock/" + encodeURIComponent(symbol))
        .then(function (data) {
            renderQuote(data);
            renderChart(data);
            renderEntries(data);
        })
        .catch(function (err) {
            console.error(err);
            entriesEl.innerHTML = '<div class="empty">数据加载失败，请刷新重试。</div>';
        });
})();
