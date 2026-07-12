/**
 * Compile-log → source error lines (for gutters / jump).
 */
(function (global) {
  "use strict";

  /**
   * Extract unique 1-based line numbers from a TeX log / error excerpt.
   * @param {string} log
   * @returns {number[]}
   */
  function errorLinesFromLog(log) {
    var set = {};
    var text = String(log || "");
    var re = /\bl\.(\d+)\b/g;
    var m;
    while ((m = re.exec(text))) set[parseInt(m[1], 10)] = true;
    re = /lines? (\d+)/gi;
    while ((m = re.exec(text))) set[parseInt(m[1], 10)] = true;
    return Object.keys(set)
      .map(function (k) {
        return parseInt(k, 10);
      })
      .filter(function (n) {
        return n > 0;
      })
      .sort(function (a, b) {
        return a - b;
      });
  }

  /**
   * Apply / clear lint gutters on a CM5-facade editor.
   * @param {object} cm
   * @param {number[]} lines1 1-based
   * @param {string} [gutterName]
   */
  function applyErrorGutters(cm, lines1, gutterName) {
    if (!cm) return;
    var name = gutterName || "CodeMirror-lint-markers";
    if (typeof cm.setOption === "function") {
      try {
        var guts = cm.getOption("gutters") || [];
        if (guts.indexOf(name) < 0) {
          cm.setOption("gutters", guts.concat([name]));
        }
      } catch (_) {}
    }
    if (typeof cm.clearGutter === "function") {
      try {
        cm.clearGutter(name);
      } catch (_) {}
    }
    (lines1 || []).forEach(function (line1) {
      var line0 = line1 - 1;
      if (line0 < 0 || line0 >= cm.lineCount()) return;
      var mark = document.createElement("div");
      mark.className = "lint-gutter-err";
      mark.title = "error l." + line1;
      mark.textContent = "●";
      mark.style.color = "#e06c75";
      mark.style.fontSize = "10px";
      mark.style.lineHeight = "1";
      if (typeof cm.setGutterMarker === "function") {
        cm.setGutterMarker(line0, name, mark);
      }
      if (typeof cm.addLineClass === "function") {
        cm.addLineClass(line0, "background", "cm-error-line");
      }
    });
  }

  function clearErrorGutters(cm, gutterName) {
    if (!cm) return;
    var name = gutterName || "CodeMirror-lint-markers";
    if (typeof cm.clearGutter === "function") {
      try {
        cm.clearGutter(name);
      } catch (_) {}
    }
    if (typeof cm.lineCount === "function" && typeof cm.removeLineClass === "function") {
      for (var i = 0; i < cm.lineCount(); i++) {
        try {
          cm.removeLineClass(i, "background", "cm-error-line");
        } catch (_) {}
      }
    }
  }

  global.AtelierLatexErrors = {
    errorLinesFromLog: errorLinesFromLog,
    applyErrorGutters: applyErrorGutters,
    clearErrorGutters: clearErrorGutters,
  };
})(typeof window !== "undefined" ? window : globalThis);
