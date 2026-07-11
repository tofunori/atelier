/**
 * Shared toolbar bindings: wrap, rewrap, auto-rewrap, open, state host.
 * Does not perform HTTP save directly.
 */
(function (global) {
  "use strict";

  function ensureWrapOption(wrapSel, v) {
    if (!wrapSel.querySelector('option[value="' + v + '"]')) {
      var opt = document.createElement("option");
      opt.value = v;
      opt.textContent = "Wrap: " + v;
      var custom = wrapSel.querySelector('option[value="custom"]');
      if (custom) wrapSel.insertBefore(opt, custom);
      else wrapSel.appendChild(opt);
    }
  }

  /**
   * @param {object} els - DOM elements
   * @param {object} api - { getCm, getExt, onWrapChange, onRewrap, onRewrapAll, onOpen, status }
   */
  function bind(els, api) {
    api = api || {};
    var wrapSel = els.wrapSel;
    var wrapCustom = els.wrapCustom;
    var rewrapBtn = els.rewrapBtn;
    var autoRewrap = els.autoRewrap;
    var unsubs = [];

    function currentWrap() {
      if (global.AtelierPrefs) return global.AtelierPrefs.getWrap();
      try {
        return localStorage.getItem("cmWrap") || "win";
      } catch (_) {
        return "win";
      }
    }

    function applyWrap(v) {
      if (global.AtelierPrefs) global.AtelierPrefs.setWrap(v);
      else
        try {
          localStorage.setItem("cmWrap", v);
        } catch (_) {}
      var cm = api.getCm && api.getCm();
      if (global.AtelierCore) global.AtelierCore.setWrap(cm, v);
      if (api.onWrapChange) api.onWrapChange(v);
    }

    if (wrapSel) {
      var saved = currentWrap();
      if (/^\d+$/.test(saved)) ensureWrapOption(wrapSel, saved);
      wrapSel.value = saved;
      wrapSel.onchange = function () {
        if (wrapSel.value === "custom") {
          if (wrapCustom) {
            wrapCustom.style.display = "";
            wrapCustom.value = /^\d+$/.test(currentWrap()) ? currentWrap() : "";
            wrapCustom.focus();
            wrapCustom.select();
          }
          return;
        }
        if (wrapCustom) wrapCustom.style.display = "none";
        applyWrap(wrapSel.value);
      };
    }

    function commitWrapCustom() {
      if (!wrapCustom) return;
      var n = parseInt(wrapCustom.value, 10);
      if (n > 0 && wrapSel) {
        ensureWrapOption(wrapSel, String(n));
        wrapSel.value = String(n);
        applyWrap(String(n));
      }
      wrapCustom.style.display = "none";
    }

    if (wrapCustom) {
      wrapCustom.addEventListener("keydown", function (e) {
        if (e.key === "Enter") commitWrapCustom();
        else if (e.key === "Escape") {
          wrapCustom.style.display = "none";
          if (wrapSel) wrapSel.value = currentWrap();
        }
      });
      wrapCustom.addEventListener("blur", commitWrapCustom);
    }

    if (autoRewrap) {
      var on =
        global.AtelierPrefs
          ? global.AtelierPrefs.getAutoRewrap()
          : (function () {
              try {
                return localStorage.getItem("codeAutoRewrap") === "1";
              } catch (_) {
                return false;
              }
            })();
      autoRewrap.checked = on;
      autoRewrap.onchange = function () {
        if (global.AtelierPrefs) global.AtelierPrefs.setAutoRewrap(autoRewrap.checked);
        else
          try {
            localStorage.setItem("codeAutoRewrap", autoRewrap.checked ? "1" : "0");
          } catch (_) {}
      };
    }

    if (rewrapBtn) {
      rewrapBtn.onclick = function () {
        if (api.onRewrap) api.onRewrap({ all: false });
        var cm = api.getCm && api.getCm();
        if (cm && cm.focus) cm.focus();
      };
    }

    if (els.openFile) {
      els.openFile.onclick = function () {
        if (api.onOpen) api.onOpen();
      };
    }

    function getWrapValue() {
      return wrapSel ? wrapSel.value : currentWrap();
    }

    function isAutoRewrap() {
      return !!(autoRewrap && autoRewrap.checked);
    }

    function destroy() {
      unsubs.forEach(function (fn) {
        try {
          fn();
        } catch (_) {}
      });
      unsubs = [];
    }

    return {
      applyWrap: applyWrap,
      getWrapValue: getWrapValue,
      isAutoRewrap: isAutoRewrap,
      ensureWrapOption: function (v) {
        if (wrapSel) ensureWrapOption(wrapSel, v);
      },
      destroy: destroy,
    };
  }

  global.AtelierToolbar = { bind: bind, ensureWrapOption: ensureWrapOption };
})(typeof window !== "undefined" ? window : globalThis);
