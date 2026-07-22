/* 管理页：监控清单增删 + 每日更新时刻设置。 */

(function () {
    "use strict";

    var tableEl = document.getElementById("stock-table");
    var countEl = document.getElementById("stock-count");
    var tzSelect = document.getElementById("tz-select");
    var schedInput = document.getElementById("sched-input");

    var TZ_NAMES = { "America/New_York": "美东（纽约）", "Asia/Shanghai": "北京", "UTC": "UTC" };

    function flash(id, text, isError) {
        var el = document.getElementById(id);
        el.textContent = text;
        el.className = "form-msg" + (isError ? " err" : " ok");
        if (text) setTimeout(function () { el.textContent = ""; }, 6000);
    }

    function renderTable(list) {
        countEl.textContent = "（" + list.length + " 支）";
        tableEl.innerHTML = list.map(function (s) {
            return (
                '<div class="stock-tr">' +
                    '<span class="td-sym">' + window.escapeHtml(s.symbol) + "</span>" +
                    '<span class="td-name">' + window.escapeHtml(s.name || "") + "</span>" +
                    '<span class="td-focus">' + window.escapeHtml(s.focus || "") + "</span>" +
                    '<button type="button" class="btn-del" data-sym="' +
                        window.escapeHtml(s.symbol) + '">移除</button>' +
                "</div>"
            );
        }).join("");

        tableEl.querySelectorAll(".btn-del").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var sym = btn.dataset.sym;
                if (!confirm("确定把 " + sym + " 移出监控列表？历史数据会保留。")) return;
                window.fetchJSON(window.API + "/api/watchlist/remove", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ symbol: sym }),
                }).then(function (data) {
                    renderTable(data.watchlist || []);
                    flash("add-msg", data.message || "已移除", false);
                }).catch(function (err) { flash("add-msg", err.message, true); });
            });
        });
    }

    // ---- 初始化 ----

    window.fetchJSON(window.API + "/api/settings").then(function (data) {
        renderTable(data.watchlist || []);
        (data.timezones || []).forEach(function (tz) {
            var opt = document.createElement("option");
            opt.value = tz;
            opt.textContent = TZ_NAMES[tz] || tz;
            tzSelect.appendChild(opt);
        });
        if (data.schedule) {
            schedInput.value = data.schedule.update_time;
            tzSelect.value = data.schedule.timezone;
        }
    }).catch(function (err) {
        tableEl.innerHTML = '<div class="empty">设置加载失败：' +
            window.escapeHtml(err.message) + "</div>";
    });

    // ---- 修改更新时刻 ----

    document.getElementById("schedule-form").addEventListener("submit", function (event) {
        event.preventDefault();
        window.fetchJSON(window.API + "/api/settings/schedule", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ update_time: schedInput.value, timezone: tzSelect.value }),
        }).then(function (data) {
            flash("schedule-msg", "已保存：每天 " + data.schedule.label + " 自动更新", false);
            var sched = document.getElementById("sched-time");
            if (sched) sched.textContent = data.schedule.label;
        }).catch(function (err) { flash("schedule-msg", err.message, true); });
    });

    // ---- 添加标的 ----

    document.getElementById("add-form").addEventListener("submit", function (event) {
        event.preventDefault();
        var btn = event.target.querySelector("button");
        btn.disabled = true;
        flash("add-msg", "正在校验行情代码…", false);
        window.fetchJSON(window.API + "/api/watchlist/add", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                symbol: document.getElementById("add-symbol").value,
                yahoo: document.getElementById("add-yahoo").value,
                name: document.getElementById("add-name").value,
                focus: document.getElementById("add-focus").value,
            }),
        }).then(function (data) {
            renderTable(data.watchlist || []);
            flash("add-msg", data.message || "已添加", false);
            event.target.reset();
        }).catch(function (err) {
            flash("add-msg", err.message, true);
        }).finally(function () { btn.disabled = false; });
    });
})();
