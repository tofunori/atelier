/**
 * Markdown document module — source via CM6 shell + optional preview pane.
 */
(function (global) {
  "use strict";

  function create(ctx) {
    var previewEl = null;
    var splitEl = null;
    var showPreview = true;
    var ro = null;
    var onChange = null;
    var onMouseDown = null;
    var onMouseUp = null;
    var onMouseMove = null;
    var dragging = false;
    var destroyed = false;

    function renderPreview(text) {
      if (!previewEl) return;
      if (global.marked && global.DOMPurify) {
        previewEl.innerHTML = global.DOMPurify.sanitize(global.marked.parse(text || ""));
      } else if (global.marked) {
        previewEl.innerHTML = global.marked.parse(text || "");
      } else {
        previewEl.textContent = text || "";
      }
    }

    function syncFromEditor() {
      var cm = ctx.getCm();
      if (cm) renderPreview(cm.getValue());
    }

    function mount() {
      var host = ctx.els && ctx.els.moduleHost;
      if (!host) return;
      // Remount: drop previous listeners first
      destroyListeners();
      destroyed = false;

      host.innerHTML = "";
      host.style.display = "flex";
      host.style.flex = "1";
      host.style.minHeight = "0";
      host.style.flexDirection = "row";

      var ed = ctx.els.ed;
      if (ed && ed.parentNode !== host) {
        host.appendChild(ed);
      }
      ed.style.flex = "1";
      ed.style.minWidth = "0";
      ed.style.minHeight = "0";
      ed.style.display = "flex";
      ed.style.flexDirection = "column";

      splitEl = document.createElement("div");
      splitEl.className = "md-split";
      splitEl.style.cssText =
        "width:4px;cursor:col-resize;background:var(--border);flex:none";

      previewEl = document.createElement("div");
      previewEl.className = "md-preview";
      previewEl.id = "mdPreview";
      previewEl.style.cssText =
        "flex:1;min-width:0;overflow:auto;padding:16px 22px;font-size:14px;line-height:1.55;border-left:1px solid var(--border)";

      host.appendChild(splitEl);
      host.appendChild(previewEl);

      var cm = ctx.getCm();
      if (cm) {
        onChange = function () {
          if (!destroyed) syncFromEditor();
        };
        cm.on("change", onChange);
        syncFromEditor();
      }

      dragging = false;
      onMouseDown = function (e) {
        dragging = true;
        e.preventDefault();
      };
      onMouseUp = function () {
        dragging = false;
      };
      onMouseMove = function (e) {
        if (!dragging || destroyed || !host || !previewEl || !ed) return;
        var rect = host.getBoundingClientRect();
        var ratio = (e.clientX - rect.left) / rect.width;
        ratio = Math.max(0.25, Math.min(0.75, ratio));
        ed.style.flex = String(ratio);
        previewEl.style.flex = String(1 - ratio);
      };
      splitEl.addEventListener("mousedown", onMouseDown);
      window.addEventListener("mouseup", onMouseUp);
      window.addEventListener("mousemove", onMouseMove);

      if (ctx.prefs && ctx.prefs.getPanel) {
        var p = ctx.prefs.getPanel();
        if (p === "source") setPreview(false);
      }
    }

    function setPreview(on) {
      showPreview = !!on;
      if (previewEl) previewEl.style.display = showPreview ? "" : "none";
      if (splitEl) splitEl.style.display = showPreview ? "" : "none";
      if (ctx.prefs && ctx.prefs.setPanel)
        ctx.prefs.setPanel(showPreview ? "preview" : "source");
    }

    function togglePreview() {
      setPreview(!showPreview);
    }

    function beforeSave() {
      /* markdown: no auto comment rewrap by default */
    }

    function rewrap() {
      return { ok: false, reason: "noop", message: "rewrap non applicable au markdown source" };
    }

    function destroyListeners() {
      var cm = ctx.getCm && ctx.getCm();
      if (cm && onChange) {
        try {
          cm.off("change", onChange);
        } catch (_) {}
      }
      onChange = null;
      if (splitEl && onMouseDown) {
        try {
          splitEl.removeEventListener("mousedown", onMouseDown);
        } catch (_) {}
      }
      onMouseDown = null;
      if (onMouseUp) {
        window.removeEventListener("mouseup", onMouseUp);
        onMouseUp = null;
      }
      if (onMouseMove) {
        window.removeEventListener("mousemove", onMouseMove);
        onMouseMove = null;
      }
      dragging = false;
    }

    function destroy() {
      destroyed = true;
      destroyListeners();
      if (ro) {
        try {
          ro.disconnect();
        } catch (_) {}
        ro = null;
      }
      previewEl = null;
      splitEl = null;
    }

    return {
      id: "markdown",
      rewrap: rewrap,
      beforeSave: beforeSave,
      mount: mount,
      destroy: destroy,
      togglePreview: togglePreview,
      setPreview: setPreview,
      syncFromEditor: syncFromEditor,
      specializedCommands: [
        { id: "togglePreview", label: "Aperçu", shortcut: null },
      ],
    };
  }

  global.AtelierModuleMarkdown = { create: create };
})(typeof window !== "undefined" ? window : globalThis);
