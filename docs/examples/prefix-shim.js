/**
 * 路径前缀补丁：当应用经网关挂载在子路径（如 /apps/KnowledgeIndex）下时，
 * 自动为代码中以 "/" 开头的同源请求地址补上前缀，使旧代码无需逐处修改。
 *
 * 依赖 base.html 先注入 window.APP_ROOT = {{ request.script_root|tojson }}。
 * 覆盖 fetch 与 XMLHttpRequest；页面跳转（location.href）与拼接进 HTML 的
 * 链接无法在此拦截，需在业务代码中使用 window.APP_ROOT 显式拼接。
 */
(function () {
    "use strict";
    var root = window.APP_ROOT || "";
    if (!root || root === "/") return;

    function rebase(url) {
        if (typeof url === "string" && url.charAt(0) === "/" && url.charAt(1) !== "/" &&
            url.indexOf(root + "/") !== 0 && url !== root) {
            return root + url;
        }
        return url;
    }

    var originFetch = window.fetch;
    if (originFetch) {
        window.fetch = function (input, init) {
            if (typeof input === "string") {
                input = rebase(input);
            } else if (input instanceof Request && input.url) {
                var parsed = new URL(input.url, window.location.origin);
                if (parsed.origin === window.location.origin) {
                    var rebased = rebase(parsed.pathname) + parsed.search + parsed.hash;
                    if (rebased !== parsed.pathname + parsed.search + parsed.hash) {
                        input = new Request(rebased, input);
                    }
                }
            }
            return originFetch.call(this, input, init);
        };
    }

    var originOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
        var args = Array.prototype.slice.call(arguments);
        args[1] = rebase(url);
        return originOpen.apply(this, args);
    };

    if (window.EventSource) {
        var OriginES = window.EventSource;
        window.EventSource = function (url, config) {
            return new OriginES(rebase(url), config);
        };
        window.EventSource.prototype = OriginES.prototype;
    }
})();
