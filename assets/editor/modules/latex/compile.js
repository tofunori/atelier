/**
 * Compile client for POST /compile — pure HTTP + log classification helpers.
 */
(function (global) {
  "use strict";

  /**
   * @param {string} path absolute project path to .tex
   * @returns {Promise<{ok:boolean, pdf?:string|null, root?:string, error?:string, log?:string}>}
   */
  async function compile(path) {
    var r = await fetch("/compile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: path }),
    });
    return r.json();
  }

  /**
   * Classify log lines for UI (error / warn / plain).
   * @param {string} log
   * @returns {{text:string, cls:string, jumps:number[]}[]}
   */
  function classifyLogLines(log) {
    return String(log || "")
      .split("\n")
      .map(function (line) {
        var cls = "";
        if (/^!|Fatal error|Emergency stop/.test(line)) cls = "tl-err";
        else if (/^LaTeX Warning|^Package .* Warning|Overfull|Underfull/.test(line))
          cls = "tl-warn";
        var jumps = [];
        var re = /\bl\.(\d+)/g;
        var m;
        while ((m = re.exec(line))) jumps.push(parseInt(m[1], 10));
        re = /lines? (\d+)/g;
        while ((m = re.exec(line))) jumps.push(parseInt(m[1], 10));
        return { text: line, cls: cls, jumps: jumps };
      });
  }

  /**
   * Build HTML for the compile console (escaped + jump spans).
   */
  function renderLogHtml(log) {
    function esc(t) {
      return String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;");
    }
    return classifyLogLines(log)
      .map(function (row) {
        var withJump = esc(row.text)
          .replace(/\bl\.(\d+)/g, function (m, n) {
            return '<span class="tl-jump" data-l="' + n + '">l.' + n + "</span>";
          })
          .replace(/lines? (\d+)/g, function (m, n) {
            return '<span class="tl-jump" data-l="' + n + '">' + m + "</span>";
          });
        return row.cls
          ? '<span class="' + row.cls + '">' + withJump + "</span>"
          : withJump;
      })
      .join("\n");
  }

  global.AtelierLatexCompile = {
    compile: compile,
    classifyLogLines: classifyLogLines,
    renderLogHtml: renderLogHtml,
  };
})(typeof window !== "undefined" ? window : globalThis);
