/**
 * Safe rewrap for comment blocks (code) and protected LaTeX paragraphs.
 * Never rewraps code; never treats Markdown headings as comments.
 */
(function (global) {
  "use strict";

  var COMMENT_PREFIX = {
    rs: /^\s*(\/\/\/|\/\/|\/\*)\s?/,
    js: /^\s*(\/\/|\/\*)\s?/,
    jsx: /^\s*(\/\/|\/\*)\s?/,
    ts: /^\s*(\/\/|\/\*)\s?/,
    tsx: /^\s*(\/\/|\/\*)\s?/,
    mjs: /^\s*(\/\/|\/\*)\s?/,
    cjs: /^\s*(\/\/|\/\*)\s?/,
    py: /^\s*(#+)\s?/,
    r: /^\s*(#+)\s?/,
    R: /^\s*(#+)\s?/,
    sh: /^\s*(#+)\s?/,
    bash: /^\s*(#+)\s?/,
    jl: /^\s*(#+)\s?/,
    toml: /^\s*(#+)\s?/,
    yaml: /^\s*(#+)\s?/,
    yml: /^\s*(#+)\s?/,
    tex: /^\s*(%)+\s?/,
    sty: /^\s*(%)+\s?/,
    bib: /^\s*(%)+\s?/,
  };

  // Markdown: never treat # headings as comments
  // (no entry for md)

  function commentPrefix(line, ext) {
    var re = COMMENT_PREFIX[ext];
    if (!re) return "";
    var match = line.match(re);
    return match ? match[1] : "";
  }

  function rewrapLines(lines, col, ext) {
    if (!lines || !lines.length) return null;
    var indent = (lines[0].match(/^\s*/) || [""])[0];
    var marker = commentPrefix(lines[0], ext);
    if (!marker) return null;
    var allComments =
      !!marker &&
      lines.every(function (line) {
        return !line.trim() || commentPrefix(line, ext) === marker;
      });
    if (!allComments) return null;

    // LaTeX safety: refuse blocks containing unescaped % mixed with code — handled by marker check
    var escaped = marker.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    var words = lines
      .map(function (line) {
        return line.replace(new RegExp("^\\s*" + escaped + "\\s?"), "").trim();
      })
      .join(" ")
      .split(/\s+/)
      .filter(Boolean);
    var prefix = indent + marker + " ";
    var out = [];
    var current = "";
    for (var i = 0; i < words.length; i++) {
      var word = words[i];
      if (!current) current = prefix + word;
      else if ((current + " " + word).length <= col) current += " " + word;
      else {
        out.push(current);
        current = prefix + word;
      }
    }
    if (current) out.push(current);
    return out;
  }

  function columnFromEditor(cm, wrapValue) {
    var selected = parseInt(wrapValue || "", 10);
    if (selected) return selected;
    if (!cm) return 80;
    var gutter = cm.getGutterElement && cm.getGutterElement();
    var gutterWidth = gutter ? gutter.offsetWidth : 0;
    var w = cm.getWrapperElement();
    var cw = cm.defaultCharWidth ? cm.defaultCharWidth() : 8;
    return Math.max(
      40,
      Math.min(120, Math.floor((w.clientWidth - gutterWidth - 16) / cw) - 2)
    );
  }

  function rewrapParagraph(cm, ext, col, showHint) {
    if (!cm) return { ok: false, reason: "no-editor" };
    var from, to;
    if (cm.somethingSelected()) {
      from = cm.getCursor("from").line;
      to = cm.getCursor("to").line;
    } else {
      var cursor = cm.getCursor().line;
      var last = cm.lineCount() - 1;
      from = cursor;
      while (from > 0 && cm.getLine(from - 1).trim() !== "") from--;
      to = cursor;
      while (to < last && cm.getLine(to + 1).trim() !== "") to++;
    }
    var lines = [];
    for (var line = from; line <= to; line++) lines.push(cm.getLine(line));
    var out = rewrapLines(lines, col, ext);
    if (!out) {
      return {
        ok: false,
        reason: "invalid-selection",
        message: "rewrap ignoré : sélectionne un bloc de commentaires",
      };
    }
    var replacement = out.join("\n");
    if (replacement === lines.join("\n")) {
      return { ok: false, reason: "noop", message: "rien à reformater" };
    }
    cm.replaceRange(
      replacement,
      { line: from, ch: 0 },
      { line: to, ch: cm.getLine(to).length }
    );
    return { ok: true, blocks: 1 };
  }

  function rewrapAllComments(cm, ext, col) {
    if (!cm) return { ok: false, blocks: 0 };
    var blocks = [];
    var start = -1;
    for (var line = 0; line <= cm.lineCount(); line++) {
      var text = line < cm.lineCount() ? cm.getLine(line) : "";
      if (commentPrefix(text, ext)) {
        if (start < 0) start = line;
      } else if (start >= 0) {
        blocks.push([start, line - 1]);
        start = -1;
      }
    }
    var changed = 0;
    cm.operation(function () {
      for (var i = blocks.length - 1; i >= 0; i--) {
        var from = blocks[i][0];
        var to = blocks[i][1];
        var lines = [];
        for (var l = from; l <= to; l++) lines.push(cm.getLine(l));
        var out = rewrapLines(lines, col, ext);
        if (out && out.join("\n") !== lines.join("\n")) {
          cm.replaceRange(
            out.join("\n"),
            { line: from, ch: 0 },
            { line: to, ch: cm.getLine(to).length }
          );
          changed++;
        }
      }
    });
    if (!changed) return { ok: false, reason: "noop", blocks: 0, message: "rien à reformater" };
    return { ok: true, blocks: changed };
  }

  global.AtelierRewrap = {
    COMMENT_PREFIX: COMMENT_PREFIX,
    commentPrefix: commentPrefix,
    rewrapLines: rewrapLines,
    columnFromEditor: columnFromEditor,
    rewrapParagraph: rewrapParagraph,
    rewrapAllComments: rewrapAllComments,
  };
})(typeof window !== "undefined" ? window : globalThis);
