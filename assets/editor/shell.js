/**
 * Unified IDE shell — composes toolbar, CM6 core, persistence, selection,
 * history and a document module (code | latex | markdown).
 *
 * Usage (bootstrap page):
 *   await AtelierShell.mount({ path, surface?, els });
 */
(function (global) {
  "use strict";

  /**
   * Resolve asset base for script tags (same rules as editor_factory).
   */
  function assetBase() {
    try {
      var scripts = document.getElementsByTagName("script");
      for (var i = scripts.length - 1; i >= 0; i--) {
        var src = scripts[i].src || "";
        if (src.indexOf("/editor/") >= 0 || src.indexOf("shell.js") >= 0) {
          var u = new URL(src);
          return u.pathname.replace(/\/editor\/[^/]*$/, "").replace(/\/$/, "") || "/.fig_thumbs";
        }
      }
    } catch (_) {}
    return "/.fig_thumbs";
  }

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement("script");
      s.src = src;
      s.onload = function () {
        resolve();
      };
      s.onerror = function () {
        reject(new Error("failed to load " + src));
      };
      document.head.appendChild(s);
    });
  }

  /** Load shell dependencies in order if not already present. */
  async function ensureDeps() {
    var base = assetBase();
    var files = [
      { global: "AtelierPrefs", src: base + "/editor/prefs.js" },
      { global: "AtelierLanguages", src: base + "/editor/languages.js" },
      { global: "AtelierStatus", src: base + "/editor/status.js" },
      { global: "AtelierCommands", src: base + "/editor/commands.js" },
      { global: "AtelierRewrap", src: base + "/editor/rewrap.js" },
      { global: "AtelierCore", src: base + "/editor/core.js" },
      { global: "AtelierToolbar", src: base + "/editor/toolbar.js" },
      { global: "AtelierSession", src: base + "/editor/session.js" },
      { global: "AtelierPersistence", src: base + "/editor/persistence.js" },
      { global: "AtelierSelection", src: base + "/editor/selection.js" },
      { global: "AtelierHistory", src: base + "/editor/history.js" },
      { global: "AtelierModuleCode", src: base + "/editor/modules/code.js" },
      { global: "AtelierModuleLatex", src: base + "/editor/modules/latex.js" },
      { global: "AtelierModuleMarkdown", src: base + "/editor/modules/markdown.js" },
    ];
    for (var i = 0; i < files.length; i++) {
      if (!global[files[i].global]) {
        await loadScript(files[i].src);
      }
    }
  }

  /**
   * @param {object} options
   * @param {string} [options.path]
   * @param {'code'|'latex'|'markdown'} [options.surface]
   * @param {object} options.els - { fname, openFile, wrapSel, wrapCustom, rewrapBtn, autoRewrap, state, ed, moduleHost, moduleActions, diff* }
   * @param {boolean} [options.browse]
   */
  async function mount(options) {
    options = options || {};
    await ensureDeps();
    if (global.AtelierPrefs) global.AtelierPrefs.migrateOnce();

    var path = options.path || null;
    var lang = global.AtelierLanguages.resolve(path || "");
    var surface = options.surface || lang.surface || "code";
    var ext = lang.ext;
    var mode = lang.mode;

    var els = options.els || {};
    if (els.fname) {
      els.fname.textContent = path ? path.split("/").pop() : options.browse ? "IDE — fichiers" : "(no file)";
    }
    if (path) document.title = path.split("/").pop();
    else if (options.browse) document.title = "IDE";

    var status = global.AtelierStatus.create(els.state);
    var commands = global.AtelierCommands.create();
    var cm = null;
    var module = null;
    var selection = null;
    var persistence = null;
    var toolbar = null;
    var dv = null;
    var resizeObserver = null;
    var destroyed = false;

    function getCm() {
      return cm;
    }

    // Diff versions
    if (path && global.DiffVersions && els.diffTag) {
      dv = global.AtelierHistory.create({
        getCm: getCm,
        path: path,
        notify: function (m) {
          // Version persistence is secondary — never replace compile/save status
          if (status.soft) status.soft("saved", m);
          else status.set("saved", m);
        },
        els: {
          group: els.diffGrp,
          tag: els.diffTag,
          prev: els.diffPrev,
          next: els.diffNext,
          restore: els.diffRestore,
        },
        restoreText: async function (text) {
          if (!persistence) return false;
          var r = await fetch((window.AtelierRuntime&&AtelierRuntime.api)?AtelierRuntime.api("/codesave"):"/codesave", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              path: path,
              text: text,
              mtime: persistence.getDiskMtime(),
            }),
          });
          var j = await r.json();
          if (j.error) return false;
          persistence.setDiskMtime(j.mtime);
          if (cm) cm.setValue(text);
          persistence.setLastSaved(text);
          persistence.setDirty(false, "version restaurée");
          return true;
        },
      });
      if (els.diffGrp) els.diffGrp.style.display = "";
    }

    var modFactory =
      surface === "latex"
        ? global.AtelierModuleLatex
        : surface === "markdown"
          ? global.AtelierModuleMarkdown
          : global.AtelierModuleCode;

    var ctx = {
      path: path,
      ext: ext,
      surface: surface,
      els: els,
      status: status,
      prefs: global.AtelierPrefs,
      getCm: getCm,
      toolbar: null,
    };

    module = modFactory.create(ctx);

    toolbar = global.AtelierToolbar.bind(els, {
      getCm: getCm,
      getExt: function () {
        return ext;
      },
      onRewrap: function (o) {
        if (module && module.rewrap) module.rewrap(!!(o && o.all));
      },
      onOpen: function () {
        if (options.onOpen) options.onOpen();
        else if (global.AtelierShell._defaultOpen) global.AtelierShell._defaultOpen();
      },
    });
    ctx.toolbar = toolbar;

    persistence = global.AtelierPersistence.create({
      path: path,
      getCm: getCm,
      status: status,
      diffVersions: dv,
      beforeSave: function () {
        if (module && module.beforeSave) module.beforeSave();
      },
      onLoaded: function () {},
    });
    // Modules (LaTeX compile, etc.) may need to flush the dirty buffer before
    // privileged server ops that read the file on disk.
    ctx.persistence = persistence;

    async function initEditor(text) {
      if (!els.ed) throw new Error("shell: missing #ed");
      cm = await global.AtelierCore.create(els.ed, {
        value: text || "",
        mode: mode,
        lineWrapping: toolbar.getWrapValue() !== "off",
        // DiffVersions populates this gutter after the asynchronous HEAD
        // request. Mount it from the first CM6 frame so adding markers never
        // shifts the code horizontally after a reload.
        gutters: dv ? ["CodeMirror-linenumbers", "dv-git"] : [],
      });
      toolbar.applyWrap(toolbar.getWrapValue());
      cm.on("change", function (_, ch) {
        if (ch && ch.origin !== "setValue") persistence.markDirty();
      });
      selection = global.AtelierSelection.bind(cm, { path: path });
      if (typeof ResizeObserver !== "undefined") {
        resizeObserver = new ResizeObserver(function () {
          if (cm) {
            cm.refresh();
            if (toolbar.getWrapValue() === "win") toolbar.applyWrap("win");
          }
        });
        resizeObserver.observe(els.ed);
      }
      if (module && module.mount) module.mount();
    }

    // Commands
    commands.register(
      "save",
      function () {
        return persistence.save();
      },
      { label: "Save", shortcut: "Mod-s" }
    );
    commands.register(
      "rewrap",
      function () {
        if (module) module.rewrap(false);
      },
      { label: "Rewrap", shortcut: "Alt-q" }
    );
    commands.register(
      "rewrap-all",
      function () {
        if (module) module.rewrap(true);
      },
      { label: "Rewrap all", shortcut: "Shift-Alt-q" }
    );
    commands.register(
      "clear-selection",
      function () {
        // Don't steal Escape from open file picker / menus
        var picker = document.getElementById("picker");
        if (picker && picker.classList.contains("show")) return false;
        if (selection) selection.clearAgentSelection();
      },
      { label: "Clear selection", shortcut: "Escape" }
    );
    if (module && module.compile) {
      commands.register(
        "compile",
        function () {
          return module.compile();
        },
        { label: "Compile", shortcut: "Mod-Enter" }
      );
    }
    if (module && module.togglePreview) {
      commands.register(
        "togglePreview",
        function () {
          module.togglePreview();
        },
        { label: "Toggle preview" }
      );
    }
    commands.attach(window);

    if (path) {
      if (global.AtelierSession) global.AtelierSession.recentsAdd(path);
      var loaded = await persistence.load();
      if (loaded) await initEditor(loaded.text);
      else await initEditor("");
      persistence.startPolling(2000);
    } else if (options.browse) {
      document.body.classList.add("browse");
    }

    function destroy() {
      if (destroyed) return;
      destroyed = true;
      commands.destroy();
      if (selection) selection.destroy();
      if (persistence) persistence.destroy();
      if (toolbar) toolbar.destroy();
      if (module) module.destroy();
      if (resizeObserver) resizeObserver.disconnect();
      global.AtelierCore.destroy(cm);
      status.destroy();
      cm = null;
    }

    return {
      cm: getCm,
      path: path,
      surface: surface,
      ext: ext,
      module: module,
      status: status,
      toolbar: toolbar,
      persistence: persistence,
      commands: commands,
      save: function () {
        return persistence.save();
      },
      destroy: destroy,
    };
  }

  global.AtelierShell = {
    mount: mount,
    ensureDeps: ensureDeps,
    assetBase: assetBase,
  };
})(typeof window !== "undefined" ? window : globalThis);
