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
        soft: function () {},
        flash: function () {},
        destroy: function () {},
      };
    }

    var primaryUntil = 0;

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
      // A clean editor is the neutral state, not a status worth occupying
      // toolbar space. Keep only actionable/transient states visible.
      if (cls === "saved" && /^saved(?:\s|$)/i.test(String(label || ""))) {
        cls = "";
        label = "";
      }
      el.className = cls || "";
      el.textContent = label || "";
      el.title = label || "";
      if (label) el.setAttribute("aria-label", label);
      else el.removeAttribute("aria-label");
      // Protect compile/save/conflict chrome from async version-store noise
      if (cls === "saved" || cls === "conflict" || cls === "dirty") {
        primaryUntil = Date.now() + 4000;
      }
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

    /** Secondary note (diff versions, etc.) — never clobber a recent primary status. */
    function soft(cls, label) {
      if (label == null || label === "") return;
      var cur = el.textContent || "";
      var hold =
        Date.now() < primaryUntil ||
        /compiled|compiling|saving|saved|✓|✗|échouée|conflit|conflict|modified|sauvegarde/i.test(
          cur
        );
      if (hold) {
        el.title = (cur ? cur + " · " : "") + label;
        return;
      }
      set(cls || "saved", label);
    }

    function flash(cls, label, ms, revert) {
      return set(cls, label, { ms: ms || 900, revertTo: revert, clear: !revert });
    }

    function destroy() {
      clearTimer();
    }

    return { set: set, soft: soft, flash: flash, destroy: destroy, el: el };
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
