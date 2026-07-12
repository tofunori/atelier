/**
 * SyncTeX client — both directions against POST /synctex.
 * dir=view : source → PDF (page, x, y)
 * dir=edit : PDF → source (line, input)
 */
(function (global) {
  "use strict";

  /**
   * Source → PDF
   * @param {{tex:string, pdf:string, line:number, col?:number}} opts
   */
  async function view(opts) {
    var r = await fetch((window.AtelierRuntime&&AtelierRuntime.api)?AtelierRuntime.api("/synctex"):"/synctex", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dir: "view",
        tex: opts.tex,
        pdf: opts.pdf,
        line: opts.line,
        col: opts.col != null ? opts.col : 1,
      }),
    });
    return r.json();
  }

  /**
   * PDF → source
   * @param {{tex:string, pdf:string, page:number, x:number, y:number}} opts
   */
  async function edit(opts) {
    var r = await fetch((window.AtelierRuntime&&AtelierRuntime.api)?AtelierRuntime.api("/synctex"):"/synctex", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dir: "edit",
        tex: opts.tex,
        pdf: opts.pdf,
        page: opts.page,
        x: opts.x,
        y: opts.y,
      }),
    });
    return r.json();
  }

  /** Normalize absolute/relative pdf path returned by /compile. */
  function siblingPdf(texPath) {
    return String(texPath || "").replace(/\.tex$/i, ".pdf");
  }

  global.AtelierLatexSynctex = {
    view: view,
    edit: edit,
    siblingPdf: siblingPdf,
  };
})(typeof window !== "undefined" ? window : globalThis);
