/**
 * Anchored source comments — re-anchor by content after rewrap/reload.
 * Positions {from,to} are 0-based CodeMirror-style {line,ch}.
 */
(function (global) {
  "use strict";

  function posToIndex(text, pos) {
    var lines = String(text).split("\n");
    var idx = 0;
    for (var i = 0; i < pos.line && i < lines.length; i++) idx += lines[i].length + 1;
    return idx + (pos.ch || 0);
  }

  function indexToPos(text, index) {
    var lines = String(text).split("\n");
    var left = Math.max(0, Math.min(index, String(text).length));
    for (var i = 0; i < lines.length; i++) {
      if (left <= lines[i].length) return { line: i, ch: left };
      left -= lines[i].length + 1;
    }
    var last = lines.length - 1;
    return { line: Math.max(0, last), ch: (lines[last] || "").length };
  }

  function sliceRange(text, from, to) {
    return String(text).slice(posToIndex(text, from), posToIndex(text, to));
  }

  /**
   * Find first occurrence of snippet in text; returns {from,to} or null.
   * Prefer search near preferred index when provided.
   */
  function findSnippet(text, snippet, preferredIndex) {
    if (!snippet) return null;
    var src = String(text);
    var needle = String(snippet);
    var idx = -1;
    if (preferredIndex != null && preferredIndex >= 0) {
      // search window around preferred
      var windowStart = Math.max(0, preferredIndex - 400);
      var local = src.indexOf(needle, windowStart);
      if (local >= 0 && local < preferredIndex + 800) idx = local;
    }
    if (idx < 0) idx = src.indexOf(needle);
    if (idx < 0) return null;
    return {
      from: indexToPos(src, idx),
      to: indexToPos(src, idx + needle.length),
      index: idx,
    };
  }

  /**
   * Re-anchor annotations after document mutation.
   * Each annot: {id, from, to, text, comment}
   * Uses stored `text` (snippet) to find new positions in newText.
   * @returns {{ok: object[], lost: object[]}}
   */
  function reanchor(annots, oldText, newText) {
    var ok = [];
    var lost = [];
    (annots || []).forEach(function (a) {
      var snippet = a.text || (oldText ? sliceRange(oldText, a.from, a.to) : "");
      var preferred =
        oldText && a.from ? posToIndex(oldText, a.from) : null;
      var hit = findSnippet(newText, snippet, preferred);
      if (!hit) {
        lost.push(a);
        return;
      }
      ok.push(
        Object.assign({}, a, {
          from: hit.from,
          to: hit.to,
          text: snippet.slice(0, 300),
        })
      );
    });
    return { ok: ok, lost: lost };
  }

  /**
   * Safe TeX comment-only rewrap of a line range (does not touch code).
   * Delegates to AtelierRewrap when available.
   */
  function rewrapCommentBlock(lines, col) {
    if (global.AtelierRewrap && global.AtelierRewrap.rewrapLines) {
      return global.AtelierRewrap.rewrapLines(lines, col, "tex");
    }
    return null;
  }

  /**
   * Environments / constructs that must never be auto-rewrapped as prose.
   */
  function isProtectedRegion(line) {
    return (
      /\\begin\{(equation|align|gather|verbatim|lstlisting|tabular|tikzpicture)/.test(line) ||
      /\\end\{(equation|align|gather|verbatim|lstlisting|tabular|tikzpicture)/.test(line) ||
      /^\s*\\\[/.test(line) ||
      /^\s*\\\]/.test(line) ||
      /^\s*\$\$/.test(line)
    );
  }

  global.AtelierLatexComments = {
    posToIndex: posToIndex,
    indexToPos: indexToPos,
    sliceRange: sliceRange,
    findSnippet: findSnippet,
    reanchor: reanchor,
    rewrapCommentBlock: rewrapCommentBlock,
    isProtectedRegion: isProtectedRegion,
  };
})(typeof window !== "undefined" ? window : globalThis);
