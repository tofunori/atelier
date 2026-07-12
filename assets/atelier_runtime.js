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
    // Allow callers to pass "/code" or "code" or full "files/code".
    return joinBase(boot.apiBase, p);
  }

  function asset(path) {
    var p = String(path || "");
    if (p.charAt(0) !== "/") p = "/" + p;
    if (boot.legacy) {
      // Historical assets lived at the origin root (/gallery_template… /cm/…).
      return p;
    }
    return joinBase(boot.assetBase, p);
  }

  function eventsUrl() {
    // Prefer the short project path; daemon also mounts /api/v1/events.
    if (boot.legacy || !boot.basePath) return "/events";
    return joinBase(boot.basePath, "/events");
  }

  function relativePath(value) {
    if (value == null) return "";
    var text = String(value);
    if (!text) return "";
    // Strip absolute origin if present.
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
        page = "/assets/latex_studio.html";
        break;
      case "markdown":
        page = "/assets/md_studio.html";
        break;
      case "pdf":
        page = "/assets/pdf_viewer.html";
        break;
      case "svg":
        page = "/assets/svg_viewer.html";
        break;
      case "code":
      default:
        page = "/assets/code_editor.html";
        break;
    }
    // Prefer asset() so hashed assets work; fall back to root assets in legacy.
    var href = boot.legacy
      ? page.replace(/^\/assets/, "") + "?" + q
      : asset(page.replace(/^\/assets\//, "")) + "?" + q;
    // When assets are shared under /assets/{hash}, the studio pages live there;
    // project-relative open still goes through basePath for same-origin cookies.
    if (!boot.legacy && boot.basePath) {
      // Keep cookie path by opening under project base when the surface is a project page.
      // Shared asset pages read bootstrap from parent / postMessage later.
      href = asset(page.replace(/^\/assets\//, "")) + "?" + q;
    }
    return href;
  }

  function guessSurface(rel) {
    var lower = String(rel || "").toLowerCase();
    if (/\.tex$/.test(lower)) return "latex";
    if (/\.md$/.test(lower)) return "markdown";
    if (/\.pdf$/.test(lower)) return "pdf";
    if (/\.svg$/.test(lower)) return "svg";
    return "code";
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
    eventsUrl: eventsUrl,
    relativePath: relativePath,
    openEditor: openEditor,
    guessSurface: guessSurface,
  };
})(typeof window !== "undefined" ? window : globalThis);
