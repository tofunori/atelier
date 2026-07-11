/**
 * Code document module — languages, safe comment rewrap.
 */
(function (global) {
  "use strict";

  function create(ctx) {
    var ext = ctx.ext || "";

    function rewrapColumn() {
      var wrap = ctx.toolbar ? ctx.toolbar.getWrapValue() : "win";
      return global.AtelierRewrap.columnFromEditor(ctx.getCm(), wrap);
    }

    function doRewrap(all) {
      var cm = ctx.getCm();
      var col = rewrapColumn();
      var result = all
        ? global.AtelierRewrap.rewrapAllComments(cm, ext, col)
        : global.AtelierRewrap.rewrapParagraph(cm, ext, col, true);
      var btn = ctx.els && ctx.els.rewrapBtn;
      if (result.ok) {
        var msg =
          "rewrap : " +
          result.blocks +
          " bloc" +
          (result.blocks > 1 ? "s" : "");
        if (global.AtelierStatus && btn)
          global.AtelierStatus.buttonFeedback(btn, "ok", msg, 900);
        if (ctx.status) ctx.status.flash("saved", msg, 900);
      } else if (result.reason === "noop") {
        if (global.AtelierStatus && btn)
          global.AtelierStatus.buttonFeedback(btn, "noop", "rien à reformater", 1400);
        if (ctx.status) ctx.status.flash("dirty", "rien à reformater", 1400);
      } else {
        if (global.AtelierStatus && btn)
          global.AtelierStatus.buttonFeedback(
            btn,
            "err",
            result.message || "sélection invalide",
            1800
          );
        if (ctx.status)
          ctx.status.flash("conflict", result.message || "sélection invalide", 1800);
      }
      return result;
    }

    function beforeSave() {
      if (ctx.toolbar && ctx.toolbar.isAutoRewrap()) {
        var col = rewrapColumn();
        global.AtelierRewrap.rewrapAllComments(ctx.getCm(), ext, col);
      }
    }

    function mount() {
      /* no extra panels for code */
    }

    function destroy() {}

    return {
      id: "code",
      rewrap: doRewrap,
      beforeSave: beforeSave,
      mount: mount,
      destroy: destroy,
      specializedCommands: [],
    };
  }

  global.AtelierModuleCode = { create: create };
})(typeof window !== "undefined" ? window : globalThis);
