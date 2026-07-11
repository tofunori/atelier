/**
 * LaTeX document module — shell integration with shared helpers under
 * assets/editor/modules/latex/* (outline, compile, synctex, comments, ghost).
 *
 * latex_studio.html remains the primary full surface until Gate C soak proves
 * parity; this module powers shell mounts (?surface=latex) and is the target
 * of progressive extraction.
 */
(function (global) {
  "use strict";

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      if (document.querySelector('script[src="' + src + '"]')) return resolve();
      var s = document.createElement("script");
      s.src = src;
      s.onload = function () { resolve(); };
      s.onerror = function () { reject(new Error("failed " + src)); };
      document.head.appendChild(s);
    });
  }

  async function ensureHelpers() {
    var base = "/.fig_thumbs/editor/modules/latex";
    try {
      var scripts = document.getElementsByTagName("script");
      for (var i = scripts.length - 1; i >= 0; i--) {
        var src = scripts[i].src || "";
        if (src.indexOf("/editor/") >= 0) {
          base = src.replace(/\/editor\/.*$/, "/editor/modules/latex");
          break;
        }
      }
    } catch (_) {}
    var deps = [
      { g: "AtelierLatexOutline", s: base + "/outline.js" },
      { g: "AtelierLatexCompile", s: base + "/compile.js" },
      { g: "AtelierLatexSynctex", s: base + "/synctex.js" },
      { g: "AtelierLatexComments", s: base + "/comments.js" },
      { g: "AtelierLatexGhost", s: base + "/ghost.js" },
    ];
    for (var d = 0; d < deps.length; d++) {
      if (!global[deps[d].g]) await loadScript(deps[d].s);
    }
  }

  function create(ctx) {
    var pdfFrame = null;
    var logEl = null;
    var outlineEl = null;
    var compiling = false;
    var pdfPath = null;
    var lastCompile = null;
    var annots = [];
    var ghostText = "";
    var ghostMark = null;
    var helpersReady = ensureHelpers();

    function rewrapColumn() {
      var wrap = ctx.toolbar ? ctx.toolbar.getWrapValue() : "win";
      return global.AtelierRewrap
        ? global.AtelierRewrap.columnFromEditor(ctx.getCm(), wrap)
        : 80;
    }

    function doRewrap(all) {
      var cm = ctx.getCm();
      if (!cm) return { ok: false, reason: "no-editor" };
      var col = rewrapColumn();
      var before = cm.getValue();
      var result = all
        ? global.AtelierRewrap.rewrapAllComments(cm, "tex", col)
        : global.AtelierRewrap.rewrapParagraph(cm, "tex", col, true);
      var after = cm.getValue();
      if (result.ok && global.AtelierLatexComments && annots.length) {
        var re = global.AtelierLatexComments.reanchor(annots, before, after);
        annots = re.ok;
      }
      var btn = ctx.els && ctx.els.rewrapBtn;
      if (result.ok) {
        var msg =
          "rewrap : " + result.blocks + " bloc" + (result.blocks > 1 ? "s" : "");
        if (global.AtelierStatus && btn)
          global.AtelierStatus.buttonFeedback(btn, "ok", msg, 900);
        if (ctx.status) ctx.status.flash("saved", msg, 900);
      } else if (result.reason === "noop") {
        if (global.AtelierStatus && btn)
          global.AtelierStatus.buttonFeedback(btn, "noop", "rien à reformater", 1400);
      } else {
        if (global.AtelierStatus && btn)
          global.AtelierStatus.buttonFeedback(
            btn,
            "err",
            result.message || "bloc protégé",
            1800
          );
      }
      return result;
    }

    async function compile() {
      await helpersReady;
      if (!ctx.path || compiling) return null;
      compiling = true;
      if (ctx.status) ctx.status.set("saved", "compiling…");
      try {
        var j = await global.AtelierLatexCompile.compile(ctx.path);
        lastCompile = { log: String(j.log || j.error || ""), ok: !!j.ok };
        if (logEl) {
          logEl.innerHTML = global.AtelierLatexCompile.renderLogHtml(
            j.log || j.error || ""
          );
        }
        if (!j.ok) {
          if (ctx.status) ctx.status.set("conflict", j.error || "compilation failed");
          return j;
        }
        if (j.pdf) pdfPath = j.pdf;
        else pdfPath = global.AtelierLatexSynctex.siblingPdf(ctx.path);
        if (ctx.status) ctx.status.set("saved", "compiled");
        if (pdfFrame && pdfPath) {
          pdfFrame.src =
            "/.fig_thumbs/pdf_viewer.html?file=" +
            encodeURIComponent(pdfPath) +
            "&t=" +
            Date.now();
        }
        return j;
      } catch (e) {
        if (ctx.status) ctx.status.set("conflict", String(e.message || e));
        return { ok: false, error: String(e.message || e) };
      } finally {
        compiling = false;
      }
    }

    async function synctexForward() {
      await helpersReady;
      var cm = ctx.getCm();
      if (!cm || !ctx.path) return null;
      var pdf = pdfPath || global.AtelierLatexSynctex.siblingPdf(ctx.path);
      var line = cm.getCursor().line + 1;
      var j = await global.AtelierLatexSynctex.view({
        tex: ctx.path,
        pdf: pdf,
        line: line,
      });
      if (j.error || !j.page) {
        if (ctx.status) ctx.status.set("dirty", "synctex : pas de correspondance");
        return j;
      }
      if (ctx.status)
        ctx.status.flash("saved", "synctex → p." + j.page, 1200);
      return j;
    }

    async function synctexBackward(page, x, y) {
      await helpersReady;
      var cm = ctx.getCm();
      if (!ctx.path) return null;
      var pdf = pdfPath || global.AtelierLatexSynctex.siblingPdf(ctx.path);
      var j = await global.AtelierLatexSynctex.edit({
        tex: ctx.path,
        pdf: pdf,
        page: page,
        x: x,
        y: y,
      });
      if (j.line && cm) {
        cm.setCursor({ line: j.line - 1, ch: 0 });
        cm.scrollIntoView({ line: j.line - 1, ch: 0 }, 80);
        cm.focus();
      }
      return j;
    }

    function refreshOutline() {
      if (!outlineEl || !global.AtelierLatexOutline) return;
      var cm = ctx.getCm();
      var src = cm ? cm.getValue() : "";
      var items = global.AtelierLatexOutline.parseOutline(src);
      var cur = cm ? cm.getCursor().line : 0;
      outlineEl.innerHTML = global.AtelierLatexOutline.renderOutlineHtml(items, cur);
    }

    function ghostUpdate() {
      if (!global.AtelierLatexGhost) return;
      var cm = ctx.getCm();
      if (!cm) return;
      if (ghostMark) {
        try { ghostMark.clear(); } catch (_) {}
        ghostMark = null;
      }
      ghostText = "";
      if (cm.somethingSelected()) return;
      var cur = cm.getCursor();
      var before = cm.getRange({ line: 0, ch: 0 }, cur);
      var sug = global.AtelierLatexGhost.suggestion(cm.getValue(), before);
      if (!sug) return;
      ghostText = sug;
      var span = document.createElement("span");
      span.className = "cm-ghost-suggest";
      span.textContent = sug;
      ghostMark = cm.setBookmark(cur, { widget: span, insertLeft: false });
    }

    function ghostAccept() {
      if (!ghostText) return false;
      var cm = ctx.getCm();
      var t = ghostText;
      ghostText = "";
      if (ghostMark) {
        try { ghostMark.clear(); } catch (_) {}
        ghostMark = null;
      }
      if (cm) cm.replaceRange(t, cm.getCursor(), null, "+ghost");
      return true;
    }

    function mount() {
      var host = ctx.els && ctx.els.moduleHost;
      if (!host) return;
      helpersReady.then(function () {
        host.innerHTML = "";
        host.style.display = "flex";
        host.style.flex = "1";
        host.style.minHeight = "0";
        host.style.flexDirection = "column";

        var toolbar = document.createElement("div");
        toolbar.style.cssText =
          "display:flex;gap:6px;padding:4px 8px;border-bottom:1px solid var(--border);flex:none;align-items:center";
        var compileBtn = document.createElement("button");
        compileBtn.className = "dvBtn";
        compileBtn.textContent = "Compile";
        compileBtn.setAttribute("aria-label", "Compile LaTeX");
        compileBtn.onclick = function () { compile(); };
        var syncBtn = document.createElement("button");
        syncBtn.className = "dvBtn";
        syncBtn.textContent = "SyncTeX →";
        syncBtn.setAttribute("aria-label", "SyncTeX source to PDF");
        syncBtn.onclick = function () { synctexForward(); };
        var outlineBtn = document.createElement("button");
        outlineBtn.className = "dvBtn";
        outlineBtn.textContent = "Plan";
        outlineBtn.setAttribute("aria-label", "Document outline");
        outlineBtn.onclick = function () {
          if (outlineEl) {
            outlineEl.style.display =
              outlineEl.style.display === "none" ? "block" : "none";
            if (outlineEl.style.display !== "none") refreshOutline();
          }
        };
        toolbar.appendChild(compileBtn);
        toolbar.appendChild(syncBtn);
        toolbar.appendChild(outlineBtn);
        if (ctx.els && ctx.els.moduleActions) {
          ctx.els.moduleActions.innerHTML = "";
          ctx.els.moduleActions.appendChild(compileBtn.cloneNode(true));
          ctx.els.moduleActions.lastChild.onclick = function () { compile(); };
        }
        host.appendChild(toolbar);

        var row = document.createElement("div");
        row.style.cssText = "display:flex;flex:1;min-height:0;position:relative";

        var ed = ctx.els.ed;
        if (ed && ed.parentNode !== row) row.appendChild(ed);
        ed.style.flex = "1";
        ed.style.minWidth = "0";
        ed.style.minHeight = "0";
        ed.style.display = "flex";
        ed.style.flexDirection = "column";

        outlineEl = document.createElement("div");
        outlineEl.id = "latexOutline";
        outlineEl.style.cssText =
          "display:none;position:absolute;top:8px;left:8px;z-index:20;width:240px;max-height:70%;overflow:auto;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:6px";
        outlineEl.addEventListener("click", function (e) {
          var b = e.target.closest && e.target.closest("[data-l]");
          if (!b) return;
          var cm = ctx.getCm();
          var ln = parseInt(b.getAttribute("data-l"), 10);
          if (cm) {
            cm.setCursor({ line: ln, ch: 0 });
            cm.focus();
          }
          outlineEl.style.display = "none";
        });
        row.appendChild(outlineEl);

        var side = document.createElement("div");
        side.style.cssText =
          "flex:1;min-width:0;display:flex;flex-direction:column;border-left:1px solid var(--border)";
        pdfFrame = document.createElement("iframe");
        pdfFrame.title = "PDF preview";
        pdfFrame.style.cssText = "flex:1;border:0;background:#111";
        logEl = document.createElement("pre");
        logEl.id = "latexCompileLog";
        logEl.style.cssText =
          "max-height:140px;overflow:auto;margin:0;padding:8px;font-size:11px;color:var(--muted);background:var(--card2)";
        side.appendChild(pdfFrame);
        side.appendChild(logEl);
        row.appendChild(side);
        host.appendChild(row);

        pdfPath = global.AtelierLatexSynctex.siblingPdf(ctx.path);
        var cm = ctx.getCm();
        if (cm) {
          cm.on("cursorActivity", function () {
            ghostUpdate();
          });
          cm.on("inputRead", function () {
            ghostUpdate();
          });
          cm.addKeyMap({
            Tab: function () {
              return ghostAccept() ? null : global.CodeMirror && global.CodeMirror.Pass;
            },
          });
        }
      });
    }

    function beforeSave() {
      if (ctx.toolbar && ctx.toolbar.isAutoRewrap()) {
        doRewrap(true);
      }
    }

    function destroy() {
      if (ghostMark) {
        try { ghostMark.clear(); } catch (_) {}
      }
      pdfFrame = null;
      logEl = null;
      outlineEl = null;
    }

    return {
      id: "latex",
      rewrap: doRewrap,
      beforeSave: beforeSave,
      mount: mount,
      destroy: destroy,
      compile: compile,
      synctexForward: synctexForward,
      synctexBackward: synctexBackward,
      refreshOutline: refreshOutline,
      ghostUpdate: ghostUpdate,
      ghostAccept: ghostAccept,
      getAnnots: function () { return annots; },
      setAnnots: function (a) { annots = a || []; },
      getLastCompile: function () { return lastCompile; },
      specializedCommands: [
        { id: "compile", label: "Compile", shortcut: "Mod-Enter" },
        { id: "synctex-view", label: "SyncTeX → PDF" },
      ],
    };
  }

  global.AtelierModuleLatex = { create: create, ensureHelpers: ensureHelpers };
})(typeof window !== "undefined" ? window : globalThis);
