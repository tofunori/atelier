/**
 * Session helpers: recents, scroll/cursor restore hooks.
 */
(function (global) {
  "use strict";

  var RECENTS_KEY = "studioRecents";

  function recentsGet() {
    try {
      return JSON.parse(localStorage.getItem(RECENTS_KEY) || "[]");
    } catch (_) {
      return [];
    }
  }

  function recentsAdd(p) {
    if (!p) return;
    var r = recentsGet().filter(function (x) {
      return x !== p;
    });
    r.unshift(p);
    r = r.slice(0, 10);
    try {
      localStorage.setItem(RECENTS_KEY, JSON.stringify(r));
    } catch (_) {}
  }

  function captureView(cm) {
    if (!cm) return null;
    return {
      cursor: cm.getCursor(),
      scroll: cm.getScrollInfo(),
    };
  }

  function restoreView(cm, snap) {
    if (!cm || !snap) return;
    try {
      if (snap.cursor) cm.setCursor(snap.cursor);
      if (snap.scroll) cm.scrollTo(snap.scroll.left, snap.scroll.top);
    } catch (_) {}
  }

  global.AtelierSession = {
    recentsGet: recentsGet,
    recentsAdd: recentsAdd,
    captureView: captureView,
    restoreView: restoreView,
  };
})(typeof window !== "undefined" ? window : globalThis);
