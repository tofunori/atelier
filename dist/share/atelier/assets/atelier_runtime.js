/*!
 * Atelier frontend runtime — single source of truth for project-scoped URLs.
 * Loaded by every surface. Bootstrap JSON is injected by the daemon:
 *   <script type="application/json" id="atelier-bootstrap">...</script>
 */
(function (global) {
  "use strict";

  function readBootstrap() {
    var el = document.getElementById("atelier-bootstrap");
    if (!el) {
      // Legacy mono-server: empty base, absolute-origin routes still work.
      return {
        projectKey: "",
        basePath: "",
        apiBase: "",
        assetBase: "/assets",
        daemonInstance: "legacy",
        legacy: true,
      };
    }
    try {
      var data = JSON.parse(el.textContent || "{}");
      if (!data || typeof data !== "object") throw new Error("invalid bootstrap");
      return {
        projectKey: String(data.projectKey || ""),
        basePath: String(data.basePath || ""),
        apiBase: String(data.apiBase || ""),
        assetBase: String(data.assetBase || "/assets"),
        daemonInstance: String(data.daemonInstance || ""),
        legacy: false,
      };
    } catch (error) {
      console.error("[AtelierRuntime] bootstrap parse failed", error);
      return null;
    }
  }

  var boot = readBootstrap();
  if (!boot) {
    global.AtelierRuntime = {
      ready: false,
      error: "missing or invalid atelier-bootstrap",
    };
    return;
  }

  // Refuse to run if the URL prefix does not match the bootstrap key.
  if (boot.projectKey && boot.basePath) {
    var path = global.location && global.location.pathname ? global.location.pathname : "";
    if (path.indexOf(boot.basePath) !== 0) {
      console.error(
        "[AtelierRuntime] bootstrap projectKey/basePath mismatch for",
        path
      );
      global.AtelierRuntime = {
        ready: false,
        error: "bootstrap/path mismatch",
        projectKey: boot.projectKey,
        basePath: boot.basePath,
      };
      return;
    }
  }

  function joinBase(base, path) {
    var p = String(path || "");
    if (!p) return base || "/";
    if (p.charAt(0) !== "/") p = "/" + p;
    if (!base) return p;
    return base.replace(/\/$/, "") + p;
  }

  function api(path) {
    var p = String(path || "");
    if (p.charAt(0) !== "/") p = "/" + p;
    if (boot.legacy || !boot.apiBase) return p;
    return joinBase(boot.apiBase, p);
  }

  function asset(path) {
    var p = String(path || "");
    if (p.charAt(0) !== "/") p = "/" + p;
    if (boot.legacy) {
      // Mono-server: leave absolute asset paths alone.
      return p;
    }
    // Strip historical /.fig_thumbs prefix when mapping into /assets.
    if (p.indexOf("/.fig_thumbs/") === 0) {
      p = p.slice("/.fig_thumbs".length);
      if (p.charAt(0) !== "/") p = "/" + p;
    }
    return joinBase(boot.assetBase, p.replace(/^\//, ""));
  }

  /**
   * Rewrite absolute same-origin paths for daemon project scope.
   * - /.fig_thumbs/* → /p/{key}/.fig_thumbs/*
   * - bare project API paths → /p/{key}/...
   * Leaves /assets, /open, /healthz, /version and already-scoped /p/ alone.
   */
  function rewriteUrl(value) {
    if (boot.legacy || value == null) return value;
    if (typeof value !== "string") return value;
    var text = value;
    if (!text) return text;
    try {
      if (/^https?:\/\//i.test(text) && global.location) {
        var abs = new URL(text, global.location.origin);
        if (abs.origin !== global.location.origin) return value;
        text = abs.pathname + abs.search + abs.hash;
        var rewritten = rewritePathOnly(text);
        return abs.origin + rewritten;
      }
    } catch (_) {
      /* fall through */
    }
    if (text.charAt(0) !== "/") return value;
    return rewritePathOnly(text);
  }

  function rewritePathOnly(text) {
    if (text.indexOf(boot.basePath + "/") === 0 || text === boot.basePath) return text;
    if (text.indexOf("/assets/") === 0 || text === "/assets") return text;
    if (text.indexOf("/open/") === 0) return text;
    if (text.indexOf("/healthz") === 0 || text.indexOf("/version") === 0) return text;
    if (text.indexOf("/.fig_thumbs/") === 0) {
      return joinBase(boot.basePath, text);
    }
    // Bare project API / static under origin root.
    if (text.charAt(0) === "/" && text.indexOf("//") !== 0) {
      return api(text);
    }
    return text;
  }

  function eventsUrl() {
    if (boot.legacy || !boot.basePath) return "/events";
    return joinBase(boot.basePath, "/events");
  }

  function relativePath(value) {
    if (value == null) return "";
    var text = String(value);
    if (!text) return "";
    try {
      if (/^https?:\/\//i.test(text) && global.location) {
        var url = new URL(text, global.location.origin);
        if (url.origin === global.location.origin) {
          text = url.pathname + url.search + url.hash;
        }
      }
    } catch (_) {
      /* ignore */
    }
    if (boot.basePath && text.indexOf(boot.basePath) === 0) {
      text = text.slice(boot.basePath.length) || "/";
    }
    return text.replace(/^\/+/, "");
  }

  function openEditor(relative, surface) {
    var rel = relativePath(relative);
    var surf = surface || guessSurface(rel);
    var q = "path=" + encodeURIComponent(rel) + "&nativeFs=1";
    if (boot.projectKey) q += "&projectKey=" + encodeURIComponent(boot.projectKey);
    var page;
    switch (surf) {
      case "latex":
        page = "latex_studio.html";
        break;
      case "markdown":
        page = "md_studio.html";
        break;
      case "pdf":
        page = "pdf_viewer.html";
        break;
      case "svg":
        page = "svg_viewer.html";
        break;
      case "code":
      default:
        page = "code_editor.html";
        break;
    }
    if (boot.legacy) {
      return "/.fig_thumbs/" + page + "?" + q;
    }
    // Keep cookie Path by loading surfaces under the project prefix.
    return joinBase(boot.basePath, "/.fig_thumbs/" + page) + "?" + q;
  }

  function guessSurface(rel) {
    var lower = String(rel || "").toLowerCase();
    if (/\.tex$/.test(lower)) return "latex";
    if (/\.md$/.test(lower)) return "markdown";
    if (/\.pdf$/.test(lower)) return "pdf";
    if (/\.svg$/.test(lower)) return "svg";
    return "code";
  }

  if (!boot.legacy) {
    // fetch
    if (typeof global.fetch === "function") {
      var originalFetch = global.fetch.bind(global);
      global.fetch = function (input, init) {
        if (typeof input === "string") {
          input = rewriteUrl(input);
        } else if (input && typeof Request !== "undefined" && input instanceof Request) {
          try {
            var rewritten = rewriteUrl(input.url);
            if (rewritten !== input.url) {
              input = new Request(rewritten, input);
            }
          } catch (_) {
            /* keep original */
          }
        }
        return originalFetch(input, init);
      };
    }

    // EventSource
    if (typeof global.EventSource === "function") {
      var OrigES = global.EventSource;
      global.EventSource = function (url, config) {
        return new OrigES(rewriteUrl(String(url)), config);
      };
      global.EventSource.prototype = OrigES.prototype;
      global.EventSource.CONNECTING = OrigES.CONNECTING;
      global.EventSource.OPEN = OrigES.OPEN;
      global.EventSource.CLOSED = OrigES.CLOSED;
    }

    // Worker
    if (typeof global.Worker === "function") {
      var OrigWorker = global.Worker;
      global.Worker = function (scriptURL, options) {
        return new OrigWorker(rewriteUrl(String(scriptURL)), options);
      };
      global.Worker.prototype = OrigWorker.prototype;
    }

    // element src / href attribute assignment
    if (typeof Element !== "undefined" && Element.prototype) {
      var origSetAttribute = Element.prototype.setAttribute;
      Element.prototype.setAttribute = function (name, value) {
        var n = String(name || "").toLowerCase();
        if (
          (n === "src" ||
            n === "href" ||
            n === "data" ||
            n === "xlink:href" ||
            n === "poster") &&
          typeof value === "string"
        ) {
          value = rewriteUrl(value);
        }
        return origSetAttribute.call(this, name, value);
      };

      // Property setters for script/iframe/img/link
      [
        ["HTMLScriptElement", "src"],
        ["HTMLIFrameElement", "src"],
        ["HTMLImageElement", "src"],
        ["HTMLLinkElement", "href"],
        ["HTMLAnchorElement", "href"],
        ["HTMLSourceElement", "src"],
        ["HTMLVideoElement", "src"],
        ["HTMLAudioElement", "src"],
      ].forEach(function (pair) {
        var ctorName = pair[0];
        var prop = pair[1];
        var Ctor = global[ctorName];
        if (!Ctor || !Ctor.prototype) return;
        var desc = Object.getOwnPropertyDescriptor(Ctor.prototype, prop);
        if (!desc || !desc.set) return;
        Object.defineProperty(Ctor.prototype, prop, {
          configurable: true,
          enumerable: desc.enumerable,
          get: desc.get,
          set: function (value) {
            desc.set.call(this, typeof value === "string" ? rewriteUrl(value) : value);
          },
        });
      });
    }
  }

  global.AtelierRuntime = {
    ready: true,
    legacy: !!boot.legacy,
    projectKey: boot.projectKey,
    basePath: boot.basePath,
    apiBase: boot.apiBase,
    assetBase: boot.assetBase,
    daemonInstance: boot.daemonInstance,
    api: api,
    asset: asset,
    rewriteUrl: rewriteUrl,
    eventsUrl: eventsUrl,
    relativePath: relativePath,
    openEditor: openEditor,
    guessSurface: guessSurface,
  };
})(typeof window !== "undefined" ? window : globalThis);
