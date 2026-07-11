/**
 * Selection → /selinfo + persistent agent mark.
 */
(function (global) {
  "use strict";

  function bind(cm, opts) {
    opts = opts || {};
    var path = opts.path || "";
    var selT = null;
    var mark = null;
    var destroyed = false;

    function clearMark() {
      if (mark) {
        try {
          mark.clear();
        } catch (_) {}
        mark = null;
      }
      if (global._clMark === mark) global._clMark = null;
    }

    function pushSel() {
      if (destroyed || !cm) return;
      var sel = cm.getSelection();
      if (sel && sel.trim()) {
        var f = cm.getCursor("from");
        var t = cm.getCursor("to");
        clearMark();
        mark = cm.markText(f, t, { className: "cm-clsel" });
        global._clMark = mark;
        fetch("/selinfo", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            lines: t.line - f.line + 1,
            words: sel.trim().split(/\s+/).length,
            text: sel,
            rel: path,
            name: path.split("/").pop(),
            page: "L" + (f.line + 1) + "-" + (t.line + 1),
          }),
        }).catch(function () {});
      }
    }

    function onCursor() {
      clearTimeout(selT);
      selT = setTimeout(pushSel, 200);
    }

    function clearAgentSelection() {
      if (cm) cm.setCursor(cm.getCursor());
      clearMark();
      fetch("/selinfo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lines: 0, words: 0 }),
      }).catch(function () {});
    }

    cm.on("cursorActivity", onCursor);

    function destroy() {
      destroyed = true;
      clearTimeout(selT);
      try {
        cm.off("cursorActivity", onCursor);
      } catch (_) {}
      clearMark();
    }

    return { clearAgentSelection: clearAgentSelection, destroy: destroy, pushSel: pushSel };
  }

  global.AtelierSelection = { bind: bind };
})(typeof window !== "undefined" ? window : globalThis);
