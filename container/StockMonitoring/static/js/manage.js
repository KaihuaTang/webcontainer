/* 管理页：只读展示 stocks.json 中的配置（修改需直接编辑该文件）。 */

(function () {
    "use strict";

    function setText(id, text) {
        document.getElementById(id).textContent = text;
    }

    window.fetchJSON(window.API + "/api/settings").then(function (data) {
        var sched = data.schedule || {};
        setText("schedule-view", "每天 " + (sched.label || "—") + " 自动联网分析" +
            (sched.timezone ? "（" + sched.timezone + " " + (sched.update_time || "") + "）" : ""));

        var list = data.watchlist || [];
        setText("stock-count", "（" + list.length + " 支）");
        document.getElementById("stock-table").innerHTML = list.map(function (s) {
            return (
                '<div class="stock-tr">' +
                    '<span class="td-sym">' + window.escapeHtml(s.symbol) + "</span>" +
                    '<span class="td-name">' + window.escapeHtml(s.name || "") + "</span>" +
                    '<span class="td-focus">' + window.escapeHtml(s.focus || "") + "</span>" +
                "</div>"
            );
        }).join("");

        var macro = data.macro_watch || [];
        setText("macro-count", "（" + macro.length + " 项）");
        document.getElementById("macro-list").innerHTML = macro.map(function (m) {
            return '<span class="macro-item">' + window.escapeHtml(m) + "</span>";
        }).join("");
    }).catch(function (err) {
        document.getElementById("stock-table").innerHTML = '<div class="empty">设置加载失败：' +
            window.escapeHtml(err.message) + "</div>";
    });
})();
