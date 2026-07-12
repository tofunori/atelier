/**
 * Command registry + keyboard shortcuts for the editor shell.
 */
(function (global) {
  "use strict";

  function createRegistry() {
    /** @type {Map<string, {id:string, run:Function, label?:string, shortcut?:string}>} */
    var commands = new Map();
    /** @type {Array<{test:Function, id:string}>} */
    var shortcuts = [];
    var keyHandler = null;

    function register(id, run, meta) {
      meta = meta || {};
      commands.set(id, {
        id: id,
        run: run,
        label: meta.label || id,
        shortcut: meta.shortcut || null,
      });
      if (meta.shortcut) {
        shortcuts.push({
          id: id,
          test: compileShortcut(meta.shortcut),
        });
      }
      return function unregister() {
        commands.delete(id);
        shortcuts = shortcuts.filter(function (s) {
          return s.id !== id;
        });
      };
    }

    function run(id, payload) {
      var c = commands.get(id);
      if (!c) return false;
      return c.run(payload);
    }

    function list() {
      return Array.from(commands.values());
    }

    function compileShortcut(spec) {
      // e.g. "Mod-s", "Alt-q", "Shift-Alt-q"
      var parts = String(spec).toLowerCase().split(/[+-]/);
      var needMod = parts.includes("mod") || parts.includes("cmd") || parts.includes("ctrl");
      var needAlt = parts.includes("alt") || parts.includes("option");
      var needShift = parts.includes("shift");
      var key = parts[parts.length - 1];
      return function (e) {
        var mod = e.metaKey || e.ctrlKey;
        if (needMod !== mod) return false;
        if (needAlt !== !!e.altKey) return false;
        if (needShift !== !!e.shiftKey) return false;
        if (needAlt && (e.metaKey || e.ctrlKey)) return false; // Alt+Q pure
        var k = (e.key || "").toLowerCase();
        var code = e.code || "";
        if (key === "s" && k === "s") return true;
        if (key === "q" && (k === "q" || code === "KeyQ")) return true;
        if (key === "escape" && k === "escape") return true;
        if (key === "enter" && k === "enter") return true;
        return k === key;
      };
    }

    function attach(target) {
      target = target || window;
      if (keyHandler) detach(target);
      keyHandler = function (e) {
        for (var i = 0; i < shortcuts.length; i++) {
          if (shortcuts[i].test(e)) {
            e.preventDefault();
            e.stopPropagation();
            run(shortcuts[i].id, { event: e });
            return;
          }
        }
      };
      target.addEventListener("keydown", keyHandler);
    }

    function detach(target) {
      target = target || window;
      if (keyHandler) {
        target.removeEventListener("keydown", keyHandler);
        keyHandler = null;
      }
    }

    function destroy() {
      detach(window);
      commands.clear();
      shortcuts = [];
    }

    return {
      register: register,
      run: run,
      list: list,
      attach: attach,
      detach: detach,
      destroy: destroy,
    };
  }

  global.AtelierCommands = { create: createRegistry };
})(typeof window !== "undefined" ? window : globalThis);
