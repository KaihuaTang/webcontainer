/* 时间轴主页：顶部「今日信号」提醒（买入/卖出 + 财报临近），下方按日时间轴。 */

(function () {
    "use strict";

    var timeline = document.getElementById("timeline");
    var todayBox = document.getElementById("today-signals");
    var WEEK = ["日", "一", "二", "三", "四", "五", "六"];

    function stockLink(sym) {
        return window.API + "/stock/" + encodeURIComponent(sym);
    }

    function earningsBadge(item) {
        if (!item.earnings || !item.earnings.date) return "";
        var md = item.earnings.date.slice(5).replace("-", "/");
        return '<span class="badge-earnings" title="财报临近">财报 ' + md + "</span>";
    }

    // ---- 今日信号（首屏最重要的提醒） ----

    function renderToday(day) {
        if (!day) { todayBox.hidden = true; return; }
        var stocks = day.stocks || {};
        var buys = [], sells = [], earnings = [];
        Object.keys(stocks).forEach(function (sym) {
            var s = stocks[sym];
            if (s.signal === "buy") buys.push([sym, s]);
            if (s.signal === "sell" || s.signal === "trim") sells.push([sym, s]);
            if (s.earnings && s.earnings.date) earnings.push([sym, s]);
        });

        function actionCards(list, cls, verb) {
            return list.map(function (pair) {
                var sym = pair[0], s = pair[1];
                return (
                    '<a class="action-card ' + cls + '" href="' + stockLink(sym) + '">' +
                        '<span class="action-verb">' + verb + "</span>" +
                        '<span class="action-sym">' + window.escapeHtml(sym) + "</span>" +
                        '<span class="action-name">' + window.escapeHtml(s.name || "") + "</span>" +
                        '<span class="action-reason">' +
                            window.escapeHtml(s.signal_reason || s.summary || "") + "</span>" +
                    "</a>"
                );
            }).join("");
        }

        var html = '<div class="today-head">今日信号 <span class="today-date">' +
                   window.escapeHtml(day.date) + "</span></div>";

        if (buys.length || sells.length) {
            html += '<div class="action-grid">' +
                    actionCards(buys, "act-buy", "买入") +
                    actionCards(sells, "act-sell", "卖出/减仓") + "</div>";
        } else {
            var watches = Object.keys(stocks).filter(function (sym) {
                return stocks[sym].signal === "watch";
            });
            html += '<div class="today-none">今日无买入 / 卖出信号' +
                    (watches.length
                        ? '，<b>' + watches.length + "</b> 支标的处于关注状态：" +
                          watches.map(function (sym) {
                              var s = stocks[sym];
                              // 旧数据无 signal_brief 时截取信号依据首个分句兜底
                              var brief = s.signal_brief ||
                                  (s.signal_reason || "").split(/[，。；：,;:]/)[0].slice(0, 14);
                              return '<a class="watch-chip" href="' + stockLink(sym) +
                                     '" title="' + window.escapeHtml(s.signal_reason || "") + '">' +
                                     window.escapeHtml(sym) +
                                     (brief ? '<span class="chip-why">（' +
                                              window.escapeHtml(brief) + '）</span>' : "") +
                                     "</a>";
                          }).join("")
                        : "。") + "</div>";
        }

        if (earnings.length) {
            html += '<div class="earnings-strip"><span class="strip-label">📅 财报临近</span>' +
                earnings.map(function (pair) {
                    var sym = pair[0], e = pair[1].earnings;
                    return '<a class="earnings-chip" href="' + stockLink(sym) + '" title="' +
                           window.escapeHtml(e.forecast || "") + '">' +
                           window.escapeHtml(sym) + " · " +
                           window.escapeHtml(e.date.slice(5).replace("-", "/")) + "</a>";
                }).join("") + "</div>";
        }

        todayBox.innerHTML = html;
        todayBox.hidden = false;
    }

    // ---- 时间轴 ----

    var ACTION_SIGNALS = ["buy", "sell", "trim"];

    function stockRow(sym, item) {
        var quote = '<span class="p">' + window.fmtPrice(item.price, item.currency) + "</span>" +
                    '<span class="c">' + window.fmtChg(item.change_pct) + "</span>";
        // 时间轴行只标注可操作信号（买入/卖出/减仓）；watch 状态见顶部「今日信号」与个股页
        var pill = ACTION_SIGNALS.indexOf(item.signal) !== -1 ? window.signalPill(item.signal) : "";
        return (
            '<a class="stock-row" href="' + stockLink(sym) + '">' +
                '<span class="row-sym">' + window.escapeHtml(sym) + "</span>" +
                '<span class="row-name">' + window.escapeHtml(item.name || "") + "</span>" +
                '<span class="row-quote">' + quote + "</span>" +
                pill + earningsBadge(item) +
                '<span class="row-summary">' +
                    window.escapeHtml(item.summary || item.signal_reason || "") + "</span>" +
            "</a>"
        );
    }

    function dayCard(day, isLatest) {
        var date = new Date(day.date + "T00:00:00");
        var week = isNaN(date) ? "" : "星期" + WEEK[date.getDay()];

        var alerts = (day.alerts || []).map(function (a) {
            return '<span class="alert-pill ' + a.type + '">' +
                (window.SIGNAL_LABEL[a.type] || a.type) + " " + window.escapeHtml(a.symbol) + "</span>";
        }).join("");

        var stocks = day.stocks || {};
        var syms = Object.keys(stocks);
        var signaled = syms.filter(function (s) {
            return ACTION_SIGNALS.indexOf(stocks[s].signal) !== -1;
        });
        var plain = syms.filter(function (s) { return signaled.indexOf(s) === -1; });

        var rowsHtml = signaled.map(function (s) { return stockRow(s, stocks[s]); }).join("");
        var collapsed = !isLatest && plain.length > 0;
        var plainHtml = plain.map(function (s) { return stockRow(s, stocks[s]); }).join("");

        return (
            '<div class="day">' +
                '<div class="day-head">' +
                    '<span class="day-date">' + window.escapeHtml(day.date) + "</span>" +
                    '<span class="day-week">' + week + "</span>" +
                    (isLatest ? '<span class="badge-latest">最新</span>' : "") +
                    (day.analysis_ok ? "" : '<span class="badge-noai">无 AI 分析</span>') +
                "</div>" +
                (day.market_overview
                    ? '<p class="day-overview">' + window.escapeHtml(day.market_overview) + "</p>" : "") +
                (alerts ? '<div class="alert-row">' + alerts + "</div>" : "") +
                '<div class="stock-rows">' + rowsHtml +
                    '<div class="plain-rows"' + (collapsed ? " hidden" : "") + ">" + plainHtml + "</div>" +
                    (collapsed
                        ? '<button type="button" class="toggle-rows">展开全部 ' + syms.length + " 支标的 ▾</button>"
                        : "") +
                "</div>" +
            "</div>"
        );
    }

    function render(days) {
        renderToday(days[0]);

        if (!days.length) {
            timeline.innerHTML =
                '<div class="empty">还没有任何监控数据。<br>系统每天 <b>' +
                (document.getElementById("sched-time").textContent || "美东 09:00") +
                "</b>（美股开盘前半小时）自动联网分析一次，请稍后回来查看。</div>";
            timeline.classList.remove("timeline");
            return;
        }
        timeline.classList.add("timeline");
        timeline.innerHTML = days.map(function (day, idx) {
            return dayCard(day, idx === 0);
        }).join("");

        timeline.querySelectorAll(".toggle-rows").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var hiddenRows = btn.parentElement.querySelector(".plain-rows");
                var show = hiddenRows.hidden;
                hiddenRows.hidden = !show;
                btn.textContent = show ? "收起 ▴" : "展开全部标的 ▾";
            });
        });
    }

    window.fetchJSON(window.API + "/api/timeline?limit=60").then(function (data) {
        window.updateTopStatus(data);
        render(data.days || []);
    }).catch(function (err) {
        console.error(err);
        timeline.innerHTML = '<div class="empty">时间轴加载失败，请刷新重试。</div>';
    });
})();
