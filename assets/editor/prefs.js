/**
 * Versioned editor preferences with one-shot migration from legacy keys.
 * Keys: atelier.editor.v1.*
 */
(function (global) {
  "use strict";

  var V1 = {
    codeTheme: "atelier.editor.v1.codeTheme",
    wrap: "atelier.editor.v1.wrap",
    autoRewrap: "atelier.editor.v1.autoRewrap",
    panel: "atelier.editor.v1.panel",
  };

  var LEGACY = {
    codeTheme: "atelierCodeTheme",
    wrap: "cmWrap",
    autoRewrap: "codeAutoRewrap",
  };

  var MIGRATED = "atelier.editor.v1.migrated";

  function get(key, fallback) {
    try {
      var v = localStorage.getItem(key);
      return v == null || v === "" ? fallback : v;
    } catch (_) {
      return fallback;
    }
  }

  function set(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (_) {}
  }

  function migrateOnce() {
    if (get(MIGRATED, "") === "1") return;
    if (get(V1.codeTheme, null) == null) {
      var t = get(LEGACY.codeTheme, null);
      if (t) set(V1.codeTheme, t);
    }
    if (get(V1.wrap, null) == null) {
      var w = get(LEGACY.wrap, null);
      if (w) set(V1.wrap, w);
    }
    if (get(V1.autoRewrap, null) == null) {
      var a = get(LEGACY.autoRewrap, null);
      if (a != null) set(V1.autoRewrap, a === "1" || a === "true" ? "1" : "0");
    }
    set(MIGRATED, "1");
  }

  function getWrap() {
    migrateOnce();
    var v = get(V1.wrap, null);
    if (v != null) return v;
    return get(LEGACY.wrap, "win");
  }

  function setWrap(v) {
    set(V1.wrap, v);
    set(LEGACY.wrap, v); // dual-write during migration period
  }

  function getAutoRewrap() {
    migrateOnce();
    var v = get(V1.autoRewrap, null);
    if (v != null) return v === "1";
    return get(LEGACY.autoRewrap, "0") === "1";
  }

  function setAutoRewrap(on) {
    var s = on ? "1" : "0";
    set(V1.autoRewrap, s);
    set(LEGACY.autoRewrap, s);
  }

  function getCodeTheme() {
    migrateOnce();
    return get(V1.codeTheme, null) || get(LEGACY.codeTheme, "Atelier Dark");
  }

  function setCodeTheme(name) {
    set(V1.codeTheme, name);
    set(LEGACY.codeTheme, name);
  }

  function getPanel() {
    migrateOnce();
    return get(V1.panel, "");
  }

  function setPanel(name) {
    set(V1.panel, name || "");
  }

  global.AtelierPrefs = {
    V1: V1,
    LEGACY: LEGACY,
    migrateOnce: migrateOnce,
    getWrap: getWrap,
    setWrap: setWrap,
    getAutoRewrap: getAutoRewrap,
    setAutoRewrap: setAutoRewrap,
    getCodeTheme: getCodeTheme,
    setCodeTheme: setCodeTheme,
    getPanel: getPanel,
    setPanel: setPanel,
  };
})(typeof window !== "undefined" ? window : globalThis);
