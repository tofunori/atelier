/**
 * Extension → language module mapping for the unified IDE shell.
 * Unknown extensions fall back to plain text (safe).
 */
(function (global) {
  "use strict";

  /** @type {Record<string, {mode: string, surface: 'code'|'latex'|'markdown', comment: string|null}>} */
  var MAP = {
    rs: { mode: "rust", surface: "code", comment: "//" },
    py: { mode: "python", surface: "code", comment: "#" },
    r: { mode: "r", surface: "code", comment: "#" },
    R: { mode: "r", surface: "code", comment: "#" },
    jl: { mode: "julia", surface: "code", comment: "#" },
    sh: { mode: "shell", surface: "code", comment: "#" },
    bash: { mode: "shell", surface: "code", comment: "#" },
    js: { mode: "javascript", surface: "code", comment: "//" },
    jsx: { mode: "javascript", surface: "code", comment: "//" },
    ts: { mode: "typescript", surface: "code", comment: "//" },
    tsx: { mode: "typescript", surface: "code", comment: "//" },
    mjs: { mode: "javascript", surface: "code", comment: "//" },
    cjs: { mode: "javascript", surface: "code", comment: "//" },
    json: { mode: "json", surface: "code", comment: null },
    toml: { mode: null, surface: "code", comment: "#" },
    yaml: { mode: null, surface: "code", comment: "#" },
    yml: { mode: null, surface: "code", comment: "#" },
    txt: { mode: null, surface: "code", comment: null },
    csv: { mode: null, surface: "code", comment: null },
    tex: { mode: "stex", surface: "latex", comment: "%" },
    sty: { mode: "stex", surface: "latex", comment: "%" },
    bib: { mode: "stex", surface: "latex", comment: "%" },
    cls: { mode: "stex", surface: "latex", comment: "%" },
    md: { mode: "markdown", surface: "markdown", comment: null },
    markdown: { mode: "markdown", surface: "markdown", comment: null },
  };

  var PAGE = {
    code: "code_editor.html",
    latex: "latex_studio.html",
    markdown: "md_viewer.html",
  };

  function extOf(path) {
    if (!path) return "";
    var base = String(path).split(/[?#]/)[0];
    var name = base.split("/").pop() || "";
    var i = name.lastIndexOf(".");
    return i >= 0 ? name.slice(i + 1) : "";
  }

  function resolve(pathOrExt) {
    var ext = pathOrExt && pathOrExt.indexOf(".") >= 0 && pathOrExt.indexOf("/") >= 0
      ? extOf(pathOrExt)
      : pathOrExt && pathOrExt.charAt(0) === "."
        ? pathOrExt.slice(1)
        : (pathOrExt && pathOrExt.indexOf(".") >= 0 && pathOrExt.indexOf("/") < 0
            ? extOf(pathOrExt)
            : String(pathOrExt || ""));
    // also handle bare filenames like sample.rs
    if (ext.indexOf(".") >= 0) ext = extOf(ext);
    var entry = MAP[ext] || { mode: null, surface: "code", comment: null };
    return {
      ext: ext,
      mode: entry.mode,
      surface: entry.surface,
      comment: entry.comment,
      page: PAGE[entry.surface] || PAGE.code,
    };
  }

  function surfacePage(surface) {
    return PAGE[surface] || PAGE.code;
  }

  function editorUrl(absPath, opts) {
    opts = opts || {};
    var info = resolve(absPath);
    var page = opts.page || info.page;
    var q = "path=" + encodeURIComponent(absPath);
    if (opts.v) q += "&v=" + encodeURIComponent(opts.v);
    if (opts.file) q += "&file=" + encodeURIComponent(opts.file);
    if (opts.extra) q += "&" + opts.extra;
    return "/.fig_thumbs/" + page + "?" + q;
  }

  function isCodeExt(ext) {
    var e = resolve(ext);
    return e.surface === "code";
  }

  function isLatexExt(ext) {
    return resolve(ext).surface === "latex";
  }

  function isMarkdownExt(ext) {
    return resolve(ext).surface === "markdown";
  }

  /** Extensions the gallery should treat as openable code (excludes tex). */
  function galleryCodeExt(ext) {
    var e = String(ext || "").toLowerCase();
    return (
      e === "py" ||
      e === "r" ||
      e === "jl" ||
      e === "sh" ||
      e === "bash" ||
      e === "rs" ||
      e === "js" ||
      e === "jsx" ||
      e === "ts" ||
      e === "tsx" ||
      e === "json" ||
      e === "toml" ||
      e === "yaml" ||
      e === "yml"
    );
  }

  global.AtelierLanguages = {
    MAP: MAP,
    PAGE: PAGE,
    extOf: extOf,
    resolve: resolve,
    surfacePage: surfacePage,
    editorUrl: editorUrl,
    isCodeExt: isCodeExt,
    isLatexExt: isLatexExt,
    isMarkdownExt: isMarkdownExt,
    galleryCodeExt: galleryCodeExt,
  };
})(typeof window !== "undefined" ? window : globalThis);
