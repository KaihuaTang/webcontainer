/* 公共工具：API 前缀、格式化、信号标签、顶栏状态显示（无手动触发）。 */

(function () {
    "use strict";

    window.API = window.APP_ROOT || "";

    window.escapeHtml = function (text) {
        return String(text == null ? "" : text).replace(/[&<>"']/g, function (ch) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
        });
    };

    window.fetchJSON = function (url, opts) {
        return fetch(url, Object.assign({ cache: "no-store" }, opts)).then(function (resp) {
            return resp.json().catch(function () { return {}; }).then(function (data) {
                if (!resp.ok) {
                    throw new Error(data.message || (url + " -> HTTP " + resp.status));
                }
                return data;
            });
        });
    };

    window.SIGNAL_LABEL = { buy: "买入", sell: "卖出", trim: "减仓", watch: "关注" };

    window.signalPill = function (signal) {
        if (!signal || signal === "none" || !window.SIGNAL_LABEL[signal]) return "";
        return '<span class="sig ' + signal + '">' + window.SIGNAL_LABEL[signal] + "</span>";
    };

    window.fmtPrice = function (value, currency) {
        if (value == null) return "—";
        var unit = currency === "HKD" ? "HK$" : currency === "USD" ? "$" : (currency || "") + " ";
        return unit + Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 });
    };

    window.fmtChg = function (pct) {
        if (pct == null) return "";
        var sign = pct > 0 ? "+" : "";
        var cls = pct > 0 ? "up" : pct < 0 ? "down" : "";
        return '<span class="' + cls + '">' + sign + pct.toFixed(2) + "%</span>";
    };

    // ---- 顶栏状态 ----

    var statusEl = document.getElementById("top-status");
    var schedEl = document.getElementById("sched-time");
    var pollTimer = null;

    function renderStatus(data) {
        var st = data.status || {};
        if (schedEl && data.schedule) schedEl.textContent = data.schedule.label;
        if (!statusEl) return;
        if (data.running) {
            statusEl.textContent = "每日分析进行中…";
            statusEl.title = "";
        } else if (st.last_run) {
            statusEl.textContent = "最近更新 " + st.last_run + (st.ok ? "" : " ⚠");
            statusEl.title = st.message || "";
        } else {
            statusEl.textContent = "等待首次定时分析";
            statusEl.title = "";
        }
    }

    window.updateTopStatus = renderStatus;

    window.fetchJSON(window.API + "/api/status").then(function (data) {
        renderStatus(data);
        if (data.running && !pollTimer) {
            // 定时分析进行中：轮询等待，完成后自动刷新页面
            pollTimer = setInterval(function () {
                window.fetchJSON(window.API + "/api/status").then(function (d) {
                    renderStatus(d);
                    if (!d.running) {
                        clearInterval(pollTimer);
                        window.location.reload();
                    }
                }).catch(function () { /* 忽略瞬时失败 */ });
            }, 8000);
        }
    }).catch(function () { /* 首屏状态失败可忽略 */ });
})();
