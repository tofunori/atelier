/**
 * Atelier editor factory — CodeMirror 6 by default, optional CM5 fallback.
 *
 * Include only this script in editor pages (it loads the engine):
 *   <script src="/.fig_thumbs/editor_factory.js"></script>
 *   …
 *   await AtelierEditor.ready;
 *   const cm = CodeMirror(parent, options);  // or AtelierEditor.create(...)
 *
 * Diagnostic: ?editor=cm5 forces legacy assets/cm/* (migration period only).
 * No CDN. No runtime import from atelier-studio.
 */
(function (global) {
  "use strict";

  var params = new URLSearchParams(location.search || "");
  var force = (params.get("editor") || "").toLowerCase();
  var engine = force === "cm5" ? "cm5" : "cm6";
  // Production gallery: scripts under /.fig_thumbs/… ; smoke/direct: same folder as this file.
  var ASSET_BASE = (function () {
    try {
      var src = (document.currentScript && document.currentScript.src) || "";
      if (src.indexOf("/.fig_thumbs/") >= 0) return "/.fig_thumbs";
      if (src) {
        var u = new URL(src);
        var path = u.pathname.replace(/\/[^/]*$/, "");
        return path || "";
      }
    } catch (e) {}
    return "/.fig_thumbs";
  })();

  var readyResolve, readyReject;
  var ready = new Promise(function (res, rej) {
    readyResolve = res;
    readyReject = rej;
  });

  function loadCss(href) {
    return new Promise(function (resolve, reject) {
      var existing = document.querySelector('link[href="' + href + '"]');
      if (existing) return resolve();
      var link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = href;
      link.onload = function () { resolve(); };
      link.onerror = function () { reject(new Error("CSS " + href)); };
      document.head.appendChild(link);
    });
  }

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var existing = document.querySelector('script[src="' + src + '"]');
      if (existing) return resolve();
      var s = document.createElement("script");
      s.src = src;
      s.async = false;
      s.onload = function () { resolve(); };
      s.onerror = function () { reject(new Error("script " + src)); };
      document.head.appendChild(s);
    });
  }

  function sequential(srcs) {
    var chain = Promise.resolve();
    srcs.forEach(function (src) {
      chain = chain.then(function () {
        return loadScript(src).catch(function () { /* optional */ });
      });
    });
    return chain;
  }

  function loadCm6() {
    // Already installed by a preloaded sync bundle?
    if (typeof global.CodeMirror === "function" && global.CodeMirror.version === "6-facade") {
      engine = "cm6";
      return Promise.resolve();
    }
    return Promise.all([
      loadCss(ASSET_BASE + "/cm6/editor.css"),
      loadScript(ASSET_BASE + "/cm6/editor.bundle.js"),
    ]).then(function () {
      if (typeof global.CodeMirror !== "function") {
        throw new Error("CM6 bundle did not install CodeMirror facade");
      }
      engine = "cm6";
    });
  }

  function loadCm5() {
    var base = ASSET_BASE + "/cm";
    var scripts = [
      base + "/codemirror.min.js",
      base + "/mark-selection.min.js",
      base + "/stex.min.js",
      base + "/python.min.js",
      base + "/r.min.js",
      base + "/markdown.min.js",
      base + "/julia.min.js",
      base + "/shell.min.js",
      base + "/javascript.min.js",
      base + "/addon/dialog.js",
      base + "/addon/searchcursor.js",
      base + "/addon/search.js",
      base + "/addon/jump-to-line.js",
      base + "/addon/annotatescrollbar.js",
      base + "/addon/matchesonscrollbar.js",
      base + "/addon/match-highlighter.js",
      base + "/addon/matchbrackets.js",
      base + "/addon/closebrackets.js",
      base + "/addon/foldcode.js",
      base + "/addon/foldgutter.js",
      base + "/addon/brace-fold.js",
      base + "/addon/indent-fold.js",
      base + "/addon/comment-fold.js",
      base + "/addon/markdown-fold.js",
      base + "/addon/xml-fold.js",
    ];
    return Promise.all([
      loadCss(base + "/codemirror.min.css"),
      loadCss(base + "/material-darker.min.css"),
      loadCss(base + "/addon/dialog.css").catch(function () {}),
      loadCss(base + "/addon/foldgutter.css").catch(function () {}),
      loadCss(base + "/addon/matchesonscrollbar.css").catch(function () {}),
    ]).then(function () {
      return sequential(scripts);
    }).then(function () {
      if (typeof global.CodeMirror !== "function") {
        throw new Error("CM5 failed to load");
      }
      engine = "cm5";
    });
  }

  var loadPromise =
    engine === "cm5"
      ? loadCm5().catch(function (err) {
          console.warn("[editor_factory] CM5 failed, trying CM6", err);
          return loadCm6();
        })
      : loadCm6().catch(function (err) {
          console.warn("[editor_factory] CM6 failed, falling back to CM5", err);
          return loadCm5();
        });

  loadPromise.then(
    function () { readyResolve(engine); },
    readyReject
  );

  function create(parent, options) {
    if (typeof global.CodeMirror !== "function") {
      throw new Error("Editor engine not ready — await AtelierEditor.ready first");
    }
    return global.CodeMirror(parent, options || {});
  }

  global.AtelierEditor = {
    ready: ready,
    engine: function () { return engine; },
    create: create,
  };
})(typeof window !== "undefined" ? window : globalThis);
