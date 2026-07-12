/**
 * CM6 core wrapper — create, reconfigure language/theme/wrap, destroy.
 * Uses AtelierEditor factory (CM6 default). No server calls.
 */
(function (global) {
  "use strict";

  /**
   * @param {HTMLElement} parent
   * @param {{value?:string, mode?:string|null, lineWrapping?:boolean, readOnly?:boolean, gutters?:string[]}} options
   */
  async function create(parent, options) {
    options = options || {};
    if (global.AtelierEditor && global.AtelierEditor.ready) {
      await global.AtelierEditor.ready;
    }
    var factory =
      (global.AtelierEditor && global.AtelierEditor.create) ||
      global.CodeMirror ||
      global.createEditor;
    if (!factory) throw new Error("AtelierEditor not loaded");

    var cm = factory(parent, {
      value: options.value != null ? options.value : "",
      mode: options.mode || null,
      theme: "material-darker",
      lineNumbers: true,
      lineWrapping: options.lineWrapping !== false,
      viewportMargin: 50,
      styleSelectedText: true,
      readOnly: !!options.readOnly,
      gutters: Array.isArray(options.gutters) ? options.gutters : [],
    });

    // Prefer code theme from versioned prefs
    if (global.AtelierPrefs && cm.setOption) {
      try {
        cm.setOption("codeTheme", global.AtelierPrefs.getCodeTheme());
      } catch (_) {}
    }

    return cm;
  }

  function setMode(cm, mode) {
    if (cm && cm.setOption) cm.setOption("mode", mode || null);
  }

  function setWrap(cm, wrapValue) {
    if (!cm) return;
    var fixed = /^\d+$/.test(String(wrapValue));
    var w = cm.getWrapperElement();
    if (w) {
      w.style.maxWidth = fixed ? "calc(" + wrapValue + "ch + 70px)" : "";
      w.style.borderRight = fixed ? "1px solid #33384a" : "";
    }
    cm.setOption("lineWrapping", wrapValue !== "off");
    if (cm.refresh) cm.refresh();
  }

  function setReadOnly(cm, ro) {
    if (cm && cm.setOption) cm.setOption("readOnly", !!ro);
  }

  function destroy(cm) {
    if (!cm) return;
    if (typeof cm.destroy === "function") {
      cm.destroy();
      return;
    }
    // Fallback: remove host DOM if destroy not yet on facade
    try {
      var el = cm.getWrapperElement && cm.getWrapperElement();
      if (el && el.parentNode) el.parentNode.removeChild(el);
    } catch (_) {}
  }

  global.AtelierCore = {
    create: create,
    setMode: setMode,
    setWrap: setWrap,
    setReadOnly: setReadOnly,
    destroy: destroy,
  };
})(typeof window !== "undefined" ? window : globalThis);
