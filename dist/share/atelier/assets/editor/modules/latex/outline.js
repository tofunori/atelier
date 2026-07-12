/**
 * Pure outline builder for LaTeX sources (section / subsection / subsubsection).
 * Shared by latex_studio and AtelierModuleLatex — no DOM required for parse.
 */
(function (global) {
  "use strict";

  var RE = /^\s*\\(section|subsection|subsubsection)\*?\{([^{}]*)\}/;
  var LVL = { section: 1, subsection: 2, subsubsection: 3 };

  /**
   * @param {string} source
   * @returns {{lvl:number, t:string, line:number}[]}
   */
  function parseOutline(source) {
    var lines = String(source || "").split("\n");
    var items = [];
    for (var i = 0; i < lines.length; i++) {
      var m = RE.exec(lines[i]);
      if (m) items.push({ lvl: LVL[m[1]], t: m[2], line: i });
    }
    return items;
  }

  /**
   * @param {{lvl:number, t:string, line:number}[]} items
   * @param {number} cursorLine 0-based
   * @returns {number} active index or -1
   */
  function activeIndex(items, cursorLine) {
    var active = -1;
    for (var k = 0; k < items.length; k++) {
      if (items[k].line <= cursorLine) active = k;
    }
    return active;
  }

  /**
   * Render outline HTML fragment (Plan header + buttons).
   * @param {{lvl:number, t:string, line:number}[]} items
   * @param {number} [cursorLine]
   */
  function renderOutlineHtml(items, cursorLine) {
    var active = typeof cursorLine === "number" ? activeIndex(items, cursorLine) : -1;
    function esc(t) {
      return String(t).replace(/[&<>]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
      });
    }
    if (!items.length) {
      return '<div class="oh">Plan</div><div class="oi" style="cursor:default">aucune section</div>';
    }
    return (
      '<div class="oh">Plan</div>' +
      items
        .map(function (it, k) {
          return (
            '<button class="oi l' +
            it.lvl +
            (k === active ? " on" : "") +
            '" data-l="' +
            it.line +
            '">' +
            esc(it.t) +
            "</button>"
          );
        })
        .join("")
    );
  }

  global.AtelierLatexOutline = {
    parseOutline: parseOutline,
    activeIndex: activeIndex,
    renderOutlineHtml: renderOutlineHtml,
    RE: RE,
  };
})(typeof window !== "undefined" ? window : globalThis);
