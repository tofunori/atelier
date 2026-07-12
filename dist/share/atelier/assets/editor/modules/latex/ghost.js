/**
 * Ghost-text suggestion helpers (pure) extracted from latex_studio.
 * Tab-accept still requires CM bindings in the host page/module.
 */
(function (global) {
  "use strict";

  function cleanText(src) {
    return String(src || "")
      .replace(/\\[a-zA-Z@]+\*?/g, " ")
      .replace(/[{}%$&#_^~]/g, " ");
  }

  function tokens(src) {
    return cleanText(src).toLowerCase().match(/[a-z][a-z'-]{2,}/g) || [];
  }

  function environmentNames(src) {
    var names = [];
    var re = /\\begin\{([a-zA-Z*]+)\}/g;
    var m;
    var text = String(src || "");
    while ((m = re.exec(text))) names.push(m[1]);
    return names;
  }

  function openEnvironment(ctx) {
    var stack = [];
    var re = /\\(begin|end)\{([a-zA-Z*]+)\}/g;
    var m;
    var text = String(ctx || "");
    while ((m = re.exec(text))) {
      if (m[1] === "begin") stack.push(m[2]);
      else if (stack.length && stack[stack.length - 1] === m[2]) stack.pop();
    }
    return stack.length ? stack[stack.length - 1] : "";
  }

  function bestWord(prefix, docTokens) {
    if (!prefix || prefix.length < 2) return "";
    var score = {};
    (docTokens || []).forEach(function (t) {
      if (t.indexOf(prefix) === 0 && t !== prefix) score[t] = (score[t] || 0) + 1;
    });
    return (
      Object.keys(score).sort(function (a, b) {
        return score[b] - score[a] || a.length - b.length;
      })[0] || ""
    );
  }

  /**
   * Suggest completion text given full document and text before cursor.
   * @returns {string}
   */
  function suggestion(doc, beforeCursor) {
    var ctx = String(beforeCursor || "");
    var m;
    if ((m = ctx.match(/\\(begin|end)\{([a-zA-Z*]*)$/))) {
      var kind = m[1];
      var partial = m[2] || "";
      var envs = environmentNames(doc);
      var first = kind === "end" ? openEnvironment(ctx) : "";
      var candidates = first
        ? [first].concat(envs.filter(function (e) { return e !== first; }))
        : envs;
      var hit = candidates.find(function (e) {
        return e.indexOf(partial) === 0;
      });
      if (hit && hit !== partial) return hit.slice(partial.length) + "}";
      if (first && kind === "end" && !partial) return first + "}";
    }
    if ((m = ctx.match(/\\ref\{([a-zA-Z0-9:_-]*)$/)) || (m = ctx.match(/\\label\{([a-zA-Z0-9:_-]*)$/))) {
      var labels = [];
      var re = /\\label\{([a-zA-Z0-9:_-]+)\}/g;
      var lm;
      while ((lm = re.exec(String(doc || "")))) labels.push(lm[1]);
      var pref = m[1] || "";
      var lab = labels.find(function (l) {
        return l.indexOf(pref) === 0 && l !== pref;
      });
      if (lab) return lab.slice(pref.length) + "}";
    }
    if ((m = ctx.match(/(?:^|[^a-zA-Z])([a-zA-Z]{2,})$/))) {
      var word = bestWord(m[1].toLowerCase(), tokens(doc));
      if (word && word.length > m[1].length) return word.slice(m[1].length);
    }
    return "";
  }

  global.AtelierLatexGhost = {
    cleanText: cleanText,
    tokens: tokens,
    environmentNames: environmentNames,
    openEnvironment: openEnvironment,
    bestWord: bestWord,
    suggestion: suggestion,
  };
})(typeof window !== "undefined" ? window : globalThis);
