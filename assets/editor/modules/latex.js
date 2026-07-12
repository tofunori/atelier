/**
 * LaTeX document module — full shell integration.
 * Shared pure helpers live under assets/editor/modules/latex/*.
 * latex_studio.html remains the default gallery route until soak; this module
 * powers code_editor.html?surface=latex and latex_editor.html.
 */
(function (global) {
  "use strict";

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      if (document.querySelector('script[src="' + src + '"]')) return resolve();
      var s = document.createElement("script");
      s.src = src;
      s.onload = function () {
        resolve();
      };
      s.onerror = function () {
        reject(new Error("failed " + src));
      };
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
      { g: "AtelierLatexErrors", s: base + "/errors.js" },
    ];
    for (var d = 0; d < deps.length; d++) {
      if (!global[deps[d].g]) await loadScript(deps[d].s);
    }
  }

  function create(ctx) {
    var pdfFrame = null;
    var logEl = null;
    var outlineEl = null;
    var toolbarEl = null;
    var sideEl = null;
    var dividerEl = null;
    var splitBtn = null;
    var editorBtn = null;
    var pdfTabBtn = null;
    var pdfEmptyEl = null;
    var moreBtn = null;
    var moreMenu = null;
    var onMoreOutside = null;
    var viewMode = "editor";
    var compiling = false;
    var pdfPath = null;
    var lastCompile = null;
    var annots = [];
    var annotMarks = {};
    var ghostText = "";
    var ghostMark = null;
    var helpersReady = ensureHelpers();
    var onCursor = null;
    var onInput = null;
    var onLogClick = null;
    var onOutlineClick = null;
    var onMessage = null;
    var onSplitDown = null;
    var onSplitMove = null;
    var onSplitUp = null;
    var destroyed = false;
    var keyMap = null;

    function annotRel() {
      return "tex-comments:" + (ctx.path || "");
    }

    function pdfViewerUrl() {
      if (!pdfPath) return null;
      return (
        "/.fig_thumbs/pdf_viewer.html?file=" +
        encodeURIComponent(pdfPath) +
        "&t=" +
        Date.now()
      );
    }

    function setViewMode(mode) {
      viewMode = mode === "split" ? "split" : "editor";
      try {
        localStorage.setItem("atelier.editor.v1.latexView", viewMode);
      } catch (_) {}
      var split = viewMode === "split";
      if (sideEl) sideEl.style.display = split ? "flex" : "none";
      if (dividerEl) dividerEl.style.display = split ? "block" : "none";
      if (editorBtn) editorBtn.classList.toggle("on", !split);
      if (splitBtn) splitBtn.classList.toggle("on", split);
      var ed = ctx.els && ctx.els.ed;
      if (ed) {
        var pct = parseFloat(
          localStorage.getItem("atelier.editor.v1.latexSplitPct") || "50"
        );
        ed.style.flex = split
          ? "0 0 " + Math.max(25, Math.min(75, pct)) + "%"
          : "1";
      }
      var cm = ctx.getCm && ctx.getCm();
      if (cm) cm.refresh();
    }

    async function openPdfTab() {
      if (!lastCompile || !lastCompile.ok) {
        var built = await compile();
        if (!built || !built.ok || destroyed) return;
      }
      var url = pdfViewerUrl();
      if (!url) return;
      var title = (ctx.path || "document.tex").split("/").pop() + " — PDF";
      if (window.parent !== window) {
        window.parent.postMessage(
          { type: "gallery-open-tab", url: url, title: title },
          "*"
        );
      } else {
        window.open(url, "_blank", "noopener");
      }
    }

    function rewrapColumn() {
      var wrap = ctx.toolbar ? ctx.toolbar.getWrapValue() : "win";
      return global.AtelierRewrap
        ? global.AtelierRewrap.columnFromEditor(ctx.getCm(), wrap)
        : 80;
    }

    function clearAnnotMarks() {
      Object.keys(annotMarks).forEach(function (id) {
        try {
          annotMarks[id].clear();
        } catch (_) {}
        delete annotMarks[id];
      });
    }

    function markAnnot(c) {
      var cm = ctx.getCm();
      if (!cm || !c || !c.from || !c.to) return;
      try {
        annotMarks[c.id] = cm.markText(c.from, c.to, {
          className: "texc-hl",
          attributes: { "data-texc": c.id },
        });
      } catch (_) {}
    }

    async function loadAnnots() {
      if (!ctx.path) return;
      try {
        var r = await fetch(
          "/pdfannot?rel=" + encodeURIComponent(annotRel())
        );
        var j = await r.json();
        annots = Array.isArray(j.annots) ? j.annots : [];
        clearAnnotMarks();
        annots.forEach(markAnnot);
      } catch (_) {
        annots = [];
      }
    }

    function saveAnnots() {
      if (!ctx.path) return;
      fetch("/pdfannot", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rel: annotRel(), annots: annots }),
      }).catch(function () {});
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
        clearAnnotMarks();
        annots = re.ok;
        annots.forEach(markAnnot);
        saveAnnots();
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

    function applyCompileDiagnostics(j) {
      var cm = ctx.getCm();
      if (!global.AtelierLatexErrors || !cm) return;
      global.AtelierLatexErrors.clearErrorGutters(cm);
      if (!j || j.ok) return;
      var lines = global.AtelierLatexErrors.errorLinesFromLog(
        j.log || j.error || ""
      );
      global.AtelierLatexErrors.applyErrorGutters(cm, lines);
      if (lines.length) {
        cm.setCursor({ line: lines[0] - 1, ch: 0 });
        cm.scrollIntoView({ line: lines[0] - 1, ch: 0 }, 60);
      }
    }

    async function compile() {
      // Lock immediately so a double-click during save-before-compile cannot
      // start a second save/compile chain.
      if (!ctx.path || compiling || destroyed) return null;
      compiling = true;
      try {
        await helpersReady;
        if (destroyed) return null;
        // Parity with latex_studio: flush dirty buffer before /compile (disk read).
        var pers = ctx.persistence;
        if (pers && typeof pers.isDirty === "function" && pers.isDirty()) {
          if (ctx.status) ctx.status.set("saved", "saving…");
          var saved = await pers.save();
          if (destroyed) return null;
          if (!saved) {
            if (ctx.status)
              ctx.status.set(
                "conflict",
                "sauvegarde refusée — compilation annulée"
              );
            return { ok: false, error: "save-before-compile failed" };
          }
        }
        if (destroyed) return null;
        if (ctx.status) ctx.status.set("saved", "compiling…");
        var j = await global.AtelierLatexCompile.compile(ctx.path);
        if (destroyed) return null;
        lastCompile = { log: String(j.log || j.error || ""), ok: !!j.ok };
        if (logEl) {
          logEl.innerHTML = global.AtelierLatexCompile.renderLogHtml(
            j.log || j.error || ""
          );
        }
        applyCompileDiagnostics(j);
        if (!j.ok) {
          if (ctx.status)
            ctx.status.set("conflict", j.error || "compilation failed");
          return j;
        }
        if (j.pdf) pdfPath = j.pdf;
        else pdfPath = global.AtelierLatexSynctex.siblingPdf(ctx.path);
        if (ctx.status) ctx.status.set("saved", "compiled");
        if (pdfFrame && pdfPath) {
          pdfFrame.style.display = "block";
          if (pdfEmptyEl) pdfEmptyEl.style.display = "none";
          pdfFrame.src = pdfViewerUrl();
        }
        return j;
      } catch (e) {
        if (!destroyed && ctx.status)
          ctx.status.set("conflict", String(e.message || e));
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
      if (ctx.status) ctx.status.flash("saved", "synctex → p." + j.page, 1200);
      // Ask PDF iframe (if listening) or open with page hint
      if (pdfFrame && pdfFrame.contentWindow) {
        try {
          pdfFrame.contentWindow.postMessage(
            { type: "atelier-synctex-view", page: j.page, y: j.y, x: j.x },
            "*"
          );
        } catch (_) {}
      }
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
        var ln = j.line - 1;
        cm.setCursor({ line: ln, ch: 0 });
        cm.scrollIntoView({ line: ln, ch: 0 }, 80);
        if (typeof cm.addLineClass === "function") {
          cm.addLineClass(ln, "background", "cm-syncline");
          setTimeout(function () {
            try {
              cm.removeLineClass(ln, "background", "cm-syncline");
            } catch (_) {}
          }, 1800);
        }
        cm.focus();
        if (ctx.status)
          ctx.status.flash("saved", "synctex ← l." + j.line, 1200);
      } else if (ctx.status) {
        ctx.status.set("dirty", "synctex : pas de correspondance");
      }
      return j;
    }

    function refreshOutline() {
      if (!outlineEl || !global.AtelierLatexOutline) return;
      var cm = ctx.getCm();
      var src = cm ? cm.getValue() : "";
      var items = global.AtelierLatexOutline.parseOutline(src);
      var cur = cm ? cm.getCursor().line : 0;
      outlineEl.innerHTML = global.AtelierLatexOutline.renderOutlineHtml(
        items,
        cur
      );
    }

    function ghostClear() {
      if (ghostMark) {
        try {
          ghostMark.clear();
        } catch (_) {}
        ghostMark = null;
      }
      ghostText = "";
    }

    function ghostUpdate() {
      if (destroyed || !global.AtelierLatexGhost) return;
      var cm = ctx.getCm();
      if (!cm) return;
      ghostClear();
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
      ghostClear();
      if (cm) cm.replaceRange(t, cm.getCursor(), null, "+ghost");
      return true;
    }

    /**
     * Add anchored comment for current selection (content-based storage).
     */
    function addCommentForSelection(commentText) {
      var cm = ctx.getCm();
      if (!cm || !cm.somethingSelected()) return null;
      var f = cm.getCursor("from");
      var t = cm.getCursor("to");
      var text = cm.getSelection().slice(0, 300);
      var c = {
        id: "c" + Date.now(),
        from: { line: f.line, ch: f.ch },
        to: { line: t.line, ch: t.ch },
        text: text,
        comment: commentText || "",
      };
      annots.push(c);
      markAnnot(c);
      saveAnnots();
      return c;
    }

    function mount() {
      var host = ctx.els && ctx.els.moduleHost;
      if (!host) return;
      destroyUi();
      destroyed = false;
      helpersReady.then(function () {
        if (destroyed) return;
        host.innerHTML = "";
        host.style.display = "flex";
        host.style.flex = "1";
        host.style.minHeight = "0";
        host.style.flexDirection = "column";

        toolbarEl = ctx.els && ctx.els.moduleActions;
        if (!toolbarEl) return;
        toolbarEl.innerHTML = "";
        toolbarEl.classList.add("latexActions");

        function mkBtn(label, aria, fn) {
          var b = document.createElement("button");
          b.className = "dvBtn";
          b.textContent = label;
          b.setAttribute("aria-label", aria);
          b.onclick = fn;
          return b;
        }
        var compileBtn = mkBtn("", "Compiler le document LaTeX", function () {
          compile();
        });
        compileBtn.id = "latexCompile";
        compileBtn.title = "Compiler (⌘Entrée)";
        compileBtn.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 1.75h5l3.5 3.5v9H3.5z"/><path d="M8.5 1.75v3.5H12"/><path d="m6.25 7 3.25 2-3.25 2z"/></svg>';
        toolbarEl.appendChild(compileBtn);
        editorBtn = mkBtn("Éditeur", "Afficher uniquement l’éditeur", function () {
          setViewMode("editor");
        });
        editorBtn.id = "latexEditorOnly";
        editorBtn.title = "Éditeur seulement";
        editorBtn.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linecap="round"><rect x="2" y="2.5" width="12" height="11" rx="1.5"/><path d="M5 6h6M5 8.5h6M5 11h3.5"/></svg>';
        toolbarEl.appendChild(editorBtn);
        splitBtn = mkBtn("Split", "Afficher l’éditeur et le PDF", function () {
          setViewMode("split");
        });
        splitBtn.id = "latexSplitToggle";
        splitBtn.title = "Éditeur + PDF";
        splitBtn.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.25"><rect x="1.75" y="2.5" width="12.5" height="11" rx="1.5"/><path d="M8 2.5v11"/></svg>';
        toolbarEl.appendChild(splitBtn);
        pdfTabBtn = mkBtn("PDF ↗", "Ouvrir le PDF dans un autre onglet", function () {
          openPdfTab();
        });
        pdfTabBtn.id = "latexPdfTab";
        pdfTabBtn.title = "PDF dans un autre onglet";
        pdfTabBtn.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2.5h4.5V7M13.5 2.5 8 8"/><path d="M12 9.5v3H3v-9h3"/></svg>';
        toolbarEl.appendChild(pdfTabBtn);

        moreBtn = mkBtn("", "Plus d’actions LaTeX", function () {
          if (!moreMenu) return;
          var opening = moreMenu.style.display === "none";
          moreMenu.style.display = opening ? "flex" : "none";
          moreBtn.classList.toggle("on", opening);
          if (opening) {
            var rc = moreBtn.getBoundingClientRect();
            moreMenu.style.top = rc.bottom + 7 + "px";
            moreMenu.style.left = Math.max(8, rc.right - 184) + "px";
          }
        });
        moreBtn.id = "latexMore";
        moreBtn.title = "Plus d’actions LaTeX";
        moreBtn.innerHTML = '<svg viewBox="0 0 16 16" fill="currentColor"><circle cx="3" cy="8" r="1.1"/><circle cx="8" cy="8" r="1.1"/><circle cx="13" cy="8" r="1.1"/></svg>';
        toolbarEl.appendChild(moreBtn);

        moreMenu = document.createElement("div");
        moreMenu.id = "latexMoreMenu";
        moreMenu.style.cssText =
          "display:none;position:fixed;z-index:450;width:176px;flex-direction:column;padding:5px;background:var(--card);border:1px solid rgba(255,255,255,.09);border-radius:8px;box-shadow:0 14px 40px rgba(0,0,0,.42)";
        function menuAction(label, fn) {
          var b = document.createElement("button");
          b.textContent = label;
          b.style.cssText =
            "height:30px;padding:0 9px;text-align:left;border:0;border-radius:5px;background:transparent;color:var(--txt);font:500 12px var(--ui-font);cursor:pointer";
          b.onclick = function () {
            moreMenu.style.display = "none";
            moreBtn.classList.remove("on");
            fn();
          };
          moreMenu.appendChild(b);
        }
        menuAction("SyncTeX → PDF", function () { synctexForward(); });
        menuAction("Plan du document", function () {
          if (!outlineEl) return;
          outlineEl.style.display =
            outlineEl.style.display === "none" ? "block" : "none";
          if (outlineEl.style.display !== "none") refreshOutline();
        });
        menuAction("Commenter la sélection", function () {
          var note = window.prompt("Commentaire ancré :", "");
          if (note == null) return;
          var c = addCommentForSelection(note);
          if (!c && ctx.status)
            ctx.status.flash("conflict", "sélectionne du texte d’abord", 1400);
          else if (ctx.status)
            ctx.status.flash("saved", "commentaire ancré", 900);
        });
        document.body.appendChild(moreMenu);
        onMoreOutside = function (e) {
          if (!moreMenu || moreMenu.style.display === "none") return;
          if (!moreMenu.contains(e.target) && !moreBtn.contains(e.target)) {
            moreMenu.style.display = "none";
            moreBtn.classList.remove("on");
          }
        };
        document.addEventListener("mousedown", onMoreOutside);

        var row = document.createElement("div");
        row.style.cssText =
          "display:flex;flex:1;min-height:0;position:relative";

        var ed = ctx.els.ed;
        if (ed && ed.parentNode !== row) row.appendChild(ed);
        var initialSplit = parseFloat(
          localStorage.getItem("atelier.editor.v1.latexSplitPct") || "50"
        );
        initialSplit = Math.max(25, Math.min(75, initialSplit));
        ed.style.flex = "0 0 " + initialSplit + "%";
        ed.style.minWidth = "0";
        ed.style.minHeight = "0";
        ed.style.display = "flex";
        ed.style.flexDirection = "column";

        outlineEl = document.createElement("div");
        outlineEl.id = "latexOutline";
        outlineEl.style.cssText =
          "display:none;position:absolute;top:8px;left:8px;z-index:20;width:240px;max-height:70%;overflow:auto;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:6px";
        onOutlineClick = function (e) {
          var b = e.target.closest && e.target.closest("[data-l]");
          if (!b) return;
          var cm = ctx.getCm();
          var ln = parseInt(b.getAttribute("data-l"), 10);
          if (cm) {
            cm.setCursor({ line: ln, ch: 0 });
            cm.focus();
          }
          outlineEl.style.display = "none";
        };
        outlineEl.addEventListener("click", onOutlineClick);
        row.appendChild(outlineEl);

        dividerEl = document.createElement("div");
        dividerEl.id = "latexSplitDivider";
        dividerEl.setAttribute("role", "separator");
        dividerEl.setAttribute("aria-label", "Redimensionner éditeur et PDF");
        dividerEl.style.cssText =
          "flex:0 0 5px;cursor:col-resize;background:var(--border);position:relative;z-index:5";
        onSplitMove = function (e) {
          var rect = row.getBoundingClientRect();
          if (!rect.width) return;
          var pct = Math.max(25, Math.min(75, ((e.clientX - rect.left) / rect.width) * 100));
          ed.style.flex = "0 0 " + pct + "%";
          localStorage.setItem("atelier.editor.v1.latexSplitPct", String(pct));
          var cm = ctx.getCm();
          if (cm) cm.refresh();
        };
        onSplitUp = function () {
          window.removeEventListener("mousemove", onSplitMove);
          window.removeEventListener("mouseup", onSplitUp);
          document.body.style.cursor = "";
          document.body.style.userSelect = "";
        };
        onSplitDown = function (e) {
          e.preventDefault();
          document.body.style.cursor = "col-resize";
          document.body.style.userSelect = "none";
          window.addEventListener("mousemove", onSplitMove);
          window.addEventListener("mouseup", onSplitUp);
        };
        dividerEl.addEventListener("mousedown", onSplitDown);
        row.appendChild(dividerEl);

        sideEl = document.createElement("div");
        sideEl.id = "latexSplitPreview";
        sideEl.style.cssText =
          "flex:1;min-width:0;display:flex;flex-direction:column";
        pdfFrame = document.createElement("iframe");
        pdfFrame.title = "PDF preview";
        pdfFrame.style.cssText = "display:none;flex:1;border:0;background:#111";
        pdfEmptyEl = document.createElement("div");
        pdfEmptyEl.id = "latexPdfEmpty";
        pdfEmptyEl.textContent = "Compile pour générer l’aperçu PDF";
        pdfEmptyEl.style.cssText =
          "display:flex;flex:1;align-items:center;justify-content:center;color:var(--muted);font-size:12px;text-align:center;padding:24px";
        logEl = document.createElement("pre");
        logEl.id = "latexCompileLog";
        logEl.style.cssText =
          "max-height:140px;overflow:auto;margin:0;padding:8px;font-size:11px;color:var(--muted);background:var(--card2)";
        onLogClick = function (e) {
          var t = e.target.closest && e.target.closest(".tl-jump");
          if (!t) return;
          var ln = parseInt(t.getAttribute("data-l"), 10) - 1;
          var cm = ctx.getCm();
          if (cm && ln >= 0) {
            cm.setCursor({ line: ln, ch: 0 });
            cm.focus();
          }
        };
        logEl.addEventListener("click", onLogClick);
        sideEl.appendChild(pdfEmptyEl);
        sideEl.appendChild(pdfFrame);
        sideEl.appendChild(logEl);
        row.appendChild(sideEl);
        host.appendChild(row);

        pdfPath = global.AtelierLatexSynctex.siblingPdf(ctx.path);
        var storedView = "editor";
        try {
          storedView = localStorage.getItem("atelier.editor.v1.latexView") || "editor";
        } catch (_) {}
        setViewMode(storedView);

        var cm = ctx.getCm();
        if (cm) {
          onCursor = function () {
            ghostUpdate();
          };
          onInput = function () {
            ghostUpdate();
          };
          cm.on("cursorActivity", onCursor);
          cm.on("inputRead", onInput);
          keyMap = {
            Tab: function () {
              return ghostAccept()
                ? null
                : global.CodeMirror && global.CodeMirror.Pass;
            },
          };
          cm.addKeyMap(keyMap);
        }

        // PDF → source: listen for postMessage from pdf viewer / parent
        onMessage = function (ev) {
          var d = ev && ev.data;
          if (!d || typeof d !== "object") return;
          if (d.type === "atelier-synctex-edit" || d.t === "jump") {
            synctexBackward(d.page || 1, d.x || 0, d.y || 0);
          }
          if (d.type === "atelier-synctex-edit-line" && d.line) {
            var cm2 = ctx.getCm();
            if (cm2) {
              cm2.setCursor({ line: d.line - 1, ch: 0 });
              cm2.focus();
            }
          }
        };
        window.addEventListener("message", onMessage);

        loadAnnots();
      });
    }

    function beforeSave() {
      if (ctx.toolbar && ctx.toolbar.isAutoRewrap()) {
        doRewrap(true);
      }
    }

    function destroyUi() {
      var cm = ctx.getCm && ctx.getCm();
      if (cm) {
        if (onCursor) {
          try {
            cm.off("cursorActivity", onCursor);
          } catch (_) {}
        }
        if (onInput) {
          try {
            cm.off("inputRead", onInput);
          } catch (_) {}
        }
        if (keyMap && typeof cm.removeKeyMap === "function") {
          try {
            cm.removeKeyMap(keyMap);
          } catch (_) {}
        }
        if (global.AtelierLatexErrors) {
          global.AtelierLatexErrors.clearErrorGutters(cm);
        }
      }
      onCursor = onInput = keyMap = null;
      if (logEl && onLogClick) {
        try {
          logEl.removeEventListener("click", onLogClick);
        } catch (_) {}
      }
      onLogClick = null;
      if (outlineEl && onOutlineClick) {
        try {
          outlineEl.removeEventListener("click", onOutlineClick);
        } catch (_) {}
      }
      onOutlineClick = null;
      if (onMessage) {
        window.removeEventListener("message", onMessage);
        onMessage = null;
      }
      if (onMoreOutside) {
        document.removeEventListener("mousedown", onMoreOutside);
        onMoreOutside = null;
      }
      if (moreMenu && moreMenu.parentNode) moreMenu.parentNode.removeChild(moreMenu);
      if (dividerEl && onSplitDown) {
        dividerEl.removeEventListener("mousedown", onSplitDown);
      }
      if (onSplitMove) window.removeEventListener("mousemove", onSplitMove);
      if (onSplitUp) window.removeEventListener("mouseup", onSplitUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      onSplitDown = onSplitMove = onSplitUp = null;
      ghostClear();
      clearAnnotMarks();
      pdfFrame = null;
      logEl = null;
      outlineEl = null;
      toolbarEl = null;
      sideEl = null;
      dividerEl = null;
      splitBtn = null;
      editorBtn = null;
      pdfTabBtn = null;
      pdfEmptyEl = null;
      moreBtn = null;
      moreMenu = null;
    }

    function destroy() {
      destroyed = true;
      destroyUi();
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
      loadAnnots: loadAnnots,
      saveAnnots: saveAnnots,
      addCommentForSelection: addCommentForSelection,
      getAnnots: function () {
        return annots;
      },
      setAnnots: function (a) {
        annots = a || [];
      },
      getLastCompile: function () {
        return lastCompile;
      },
      getPdfPath: function () {
        return pdfPath;
      },
      specializedCommands: [
        { id: "compile", label: "Compile", shortcut: "Mod-Enter" },
        { id: "synctex-view", label: "SyncTeX → PDF" },
        { id: "outline", label: "Plan" },
      ],
    };
  }

  global.AtelierModuleLatex = { create: create, ensureHelpers: ensureHelpers };
})(typeof window !== "undefined" ? window : globalThis);
