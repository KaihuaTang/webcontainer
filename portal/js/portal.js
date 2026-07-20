/* 门户逻辑：加载站点文案与项目列表，渲染卡片，支持搜索与类型筛选。
   标题渲染规则：按中文/英文逗号断行；"AI" 一词以主题橙强调；末行缀星芒。 */

(function () {
    "use strict";

    var grid = document.getElementById("grid");
    var message = document.getElementById("message");
    var toolbar = document.getElementById("toolbar");
    var searchInput = document.getElementById("search-input");
    var typeChips = document.getElementById("type-chips");

    var state = {
        projects: [],
        keyword: "",
        type: "全部",
    };

    var STATUS_TEXT = {
        running: "运行中",
        static: "在线",
        link: "站外项目",
        starting: "启动中",
        stopped: "未运行",
        error: "异常",
    };

    var STAR_SVG =
        '<svg class="hero-star" viewBox="0 0 40 40" aria-hidden="true" fill="none" ' +
        'stroke="#d97757" stroke-width="3.2" stroke-linecap="round"><path ' +
        'd="M20 17 V4 M22.1 17.9 28.5 11.5 M23 20 H35 M22.1 22.1 27.8 27.8 ' +
        'M20 23 V34 M17.9 22.1 11.5 28.5 M17 20 H5 M17.9 17.9 12.9 12.9"/></svg>';

    // ---- 工具 ----

    function escapeHtml(text) {
        return String(text == null ? "" : text).replace(/[&<>"']/g, function (ch) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
        });
    }

    function fetchJSON(url) {
        return fetch(url, { cache: "no-store" }).then(function (resp) {
            if (!resp.ok) throw new Error(url + " -> HTTP " + resp.status);
            return resp.json();
        });
    }

    /* 根据项目 id 生成稳定的暖色系字母头像底色 */
    function avatarColor(id) {
        var hash = 0;
        for (var i = 0; i < id.length; i++) {
            hash = (hash * 31 + id.charCodeAt(i)) >>> 0;
        }
        return "hsl(" + (hash % 360) + ", 32%, 46%)";
    }

    // ---- 站点文案 ----

    function renderTitle(title) {
        var html = escapeHtml(String(title).trim()).replace(/AI/i, function (m) {
            return '<em class="title-accent">' + m + "</em>";
        });
        document.getElementById("site-title").innerHTML = html + STAR_SVG;
    }

    function applySite(site) {
        var set = function (id, text) {
            var el = document.getElementById(id);
            if (el && text) el.textContent = text;
        };
        set("site-org", site.org);
        set("site-subtitle", site.subtitle);
        set("site-footer", site.footer);
        if (site.title) renderTitle(site.title);
        if (site.title && site.org) {
            document.title = site.title + " · " + site.org;
        }
    }

    // ---- 渲染 ----

    function cardHTML(p) {
        var icon = p.icon
            ? '<img class="card-icon" src="' + escapeHtml(p.icon) + '" alt="" loading="lazy" ' +
              'onerror="this.hidden=true;if(this.nextElementSibling)this.nextElementSibling.hidden=false;">'
            : "";
        var avatar = '<span class="card-avatar" style="background:' + avatarColor(p.id) + '"' +
            (p.icon ? ' hidden' : '') + '>' +
            escapeHtml((p.name || p.id).charAt(0).toUpperCase()) + "</span>";

        var statusKey = STATUS_TEXT[p.status] ? p.status : "stopped";
        var desc = p.error
            ? '<p class="card-error">配置错误：' + escapeHtml(p.error) + "</p>"
            : '<p class="card-desc">' + escapeHtml(p.description || "（暂无简介）") + "</p>";

        return (
            '<div class="card-head">' + icon + avatar +
                '<div class="card-title-wrap">' +
                    '<h2 class="card-name">' + escapeHtml(p.name) + "</h2>" +
                    '<span class="card-type">' + escapeHtml(p.type || "未分类") + "</span>" +
                "</div>" +
                (p.error ? "" : '<span class="card-open" aria-hidden="true">↗</span>') +
            "</div>" +
            desc +
            '<div class="card-foot">' +
                '<span class="card-author">作者：' + escapeHtml(p.author || "未署名") + "</span>" +
                '<span class="status-pill ' + statusKey + '"><span class="dot ' + statusKey +
                    '"></span>' + STATUS_TEXT[statusKey] + "</span>" +
            "</div>"
        );
    }

    function render() {
        var keyword = state.keyword.trim().toLowerCase();
        var list = state.projects.filter(function (p) {
            if (state.type !== "全部" && (p.type || "未分类") !== state.type) return false;
            if (!keyword) return true;
            var haystack = [p.name, p.id, p.description, p.author, (p.tags || []).join(" ")]
                .join(" ").toLowerCase();
            return haystack.indexOf(keyword) !== -1;
        });

        grid.innerHTML = "";
        message.hidden = true;

        if (!state.projects.length) {
            showMessage(
                "container/ 目录下还没有可展示的项目。<br>" +
                "在项目目录中添加 <code>project.json</code> 后刷新本页即可接入，详见仓库 README。"
            );
            return;
        }
        if (!list.length) {
            showMessage("没有匹配的项目，换个关键词试试。");
            return;
        }

        list.forEach(function (p, idx) {
            var clickable = !p.error;
            var el = document.createElement(clickable ? "a" : "div");
            el.className = "card" + (clickable ? "" : " disabled");
            el.style.animationDelay = Math.min(idx * 45, 360) + "ms";
            if (clickable) {
                el.href = p.url;
                el.target = "_blank";
                el.rel = "noopener";
                el.setAttribute("aria-label", "打开项目 " + p.name);
            }
            el.innerHTML = cardHTML(p);
            grid.appendChild(el);
        });
    }

    function showMessage(html, withRetry) {
        message.innerHTML = html +
            (withRetry ? '<br><button type="button" class="btn-retry" id="btn-retry">重试</button>' : "");
        message.hidden = false;
        if (withRetry) {
            document.getElementById("btn-retry").addEventListener("click", function () {
                renderSkeleton();
                message.hidden = true;
                boot();
            });
        }
    }

    function renderSkeleton() {
        grid.innerHTML = "";
        for (var i = 0; i < 6; i++) {
            var sk = document.createElement("div");
            sk.className = "skeleton";
            grid.appendChild(sk);
        }
    }

    // ---- 筛选控件 ----

    function buildChips() {
        var types = ["全部"];
        state.projects.forEach(function (p) {
            var t = p.type || "未分类";
            if (types.indexOf(t) === -1) types.push(t);
        });
        typeChips.innerHTML = "";
        types.forEach(function (t) {
            var chip = document.createElement("button");
            chip.type = "button";
            chip.className = "chip" + (t === state.type ? " active" : "");
            chip.textContent = t;
            chip.addEventListener("click", function () {
                state.type = t;
                typeChips.querySelectorAll(".chip").forEach(function (c) {
                    c.classList.toggle("active", c.textContent === t);
                });
                render();
            });
            typeChips.appendChild(chip);
        });
    }

    function updateStats() {
        var running = state.projects.filter(function (p) {
            return p.status === "running" || p.status === "static" || p.status === "link";
        }).length;
        document.getElementById("stat-total").textContent = state.projects.length;
        document.getElementById("stat-running").textContent = running;
        document.getElementById("hero-stats").hidden = false;
    }

    // ---- 数据加载 ----

    function loadProjects(initial) {
        return fetchJSON("/api/projects").then(function (data) {
            state.projects = data.projects || [];
            updateStats();
            if (initial) {
                toolbar.hidden = state.projects.length === 0;
                buildChips();
            }
            render();
        });
    }

    // ---- 交互 ----

    searchInput.addEventListener("input", function () {
        state.keyword = searchInput.value;
        render();
    });

    searchInput.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
            searchInput.value = "";
            state.keyword = "";
            render();
            searchInput.blur();
        }
    });

    /* 桌面端按 / 聚焦搜索框 */
    document.addEventListener("keydown", function (event) {
        var tag = (event.target.tagName || "").toLowerCase();
        if (event.key === "/" && tag !== "input" && tag !== "textarea") {
            event.preventDefault();
            searchInput.focus();
        }
    });

    // ---- 启动 ----

    function boot() {
        fetchJSON("/api/site").then(applySite).catch(function () { /* 使用内置文案 */ });
        loadProjects(true).catch(function (err) {
            console.error(err);
            grid.innerHTML = "";
            showMessage("项目列表加载失败，请稍后重试。", true);
        });
    }

    renderSkeleton();
    boot();

    // 周期刷新运行状态（不打断用户当前的筛选）
    setInterval(function () {
        loadProjects(false).catch(function () { /* 静默忽略瞬时失败 */ });
    }, 30000);
})();
