/**
 * Compact editor status + local command feedback.
 */
(function (global) {
  "use strict";

  var TIMERS = new WeakMap();

  function createStatus(el) {
    if (!el) {
      return {
        set: function () {},
        flash: function () {},
        destroy: function () {},
      };
    }

    function clearTimer() {
      var t = TIMERS.get(el);
      if (t) {
        clearTimeout(t);
        TIMERS.delete(el);
      }
    }

    function set(cls, label, opts) {
      opts = opts || {};
      clearTimer();
      el.className = cls || "";
      el.textContent = label || "";
      el.title = label || "";
      el.setAttribute("aria-label", label || "état de l’éditeur");
      if (opts.ms) {
        var prev = { cls: cls, label: label };
        TIMERS.set(
          el,
          setTimeout(function () {
            if (opts.revertTo) {
              set(opts.revertTo.cls || "", opts.revertTo.label || "");
            } else if (opts.clear) {
              el.className = "";
              el.textContent = "";
              el.removeAttribute("aria-label");
            }
          }, opts.ms)
        );
      }
      return prev;
    }

    function flash(cls, label, ms, revert) {
      return set(cls, label, { ms: ms || 900, revertTo: revert, clear: !revert });
    }

    function destroy() {
      clearTimer();
    }

    return { set: set, flash: flash, destroy: destroy, el: el };
  }

  /** Local feedback on a command button (rewrap success/nothing/error). */
  function buttonFeedback(btn, kind, label, ms) {
    if (!btn) return;
    var prevTitle = btn.title;
    var prevAria = btn.getAttribute("aria-label");
    btn.classList.remove("fb-ok", "fb-noop", "fb-err");
    var cls =
      kind === "ok" ? "fb-ok" : kind === "err" ? "fb-err" : "fb-noop";
    btn.classList.add(cls);
    if (label) {
      btn.title = label;
      btn.setAttribute("aria-label", label);
    }
    setTimeout(function () {
      btn.classList.remove("fb-ok", "fb-noop", "fb-err");
      btn.title = prevTitle;
      if (prevAria != null) btn.setAttribute("aria-label", prevAria);
      else btn.removeAttribute("aria-label");
    }, ms || 900);
  }

  global.AtelierStatus = {
    create: createStatus,
    buttonFeedback: buttonFeedback,
  };
})(typeof window !== "undefined" ? window : globalThis);
