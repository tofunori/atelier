/**
 * Thin adapter around DiffVersions for the shell.
 */
(function (global) {
  "use strict";

  function create(opts) {
    if (!opts || !opts.path || typeof global.DiffVersions !== "function") {
      return null;
    }
    return global.DiffVersions({
      getCm: opts.getCm,
      path: opts.path,
      notify: opts.notify,
      els: opts.els || {},
      restoreText: opts.restoreText,
    });
  }

  global.AtelierHistory = { create: create };
})(typeof window !== "undefined" ? window : globalThis);
