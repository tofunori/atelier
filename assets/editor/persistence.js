/**
 * Load / save / external reload / conflict handling via Rust HTTP contracts.
 */
(function (global) {
  "use strict";

  /**
   * @param {object} opts
   * @param {string} opts.path
   * @param {function} opts.getCm
   * @param {function} opts.status - AtelierStatus instance
   * @param {function} [opts.onLoaded]
   * @param {function} [opts.onSaved]
   * @param {function} [opts.beforeSave] - may mutate buffer (auto-rewrap)
   * @param {object} [opts.diffVersions] - DiffVersions handle
   */
  function create(opts) {
    var path = opts.path;
    var diskMtime = 0;
    var dirty = false;
    var lastSavedText = null;
    var pollTimer = null;
    var mergeConflictAt = 0;
    var destroyed = false;

    function status() {
      return opts.status;
    }

    function setDirty(on, label) {
      dirty = !!on;
      if (status()) status().set(on ? "dirty" : "saved", label || (on ? "modified" : "saved"));
    }

    async function load() {
      if (!path) return null;
      var r = await fetch((window.AtelierRuntime&&AtelierRuntime.api)?AtelierRuntime.api("/code?path="):"/code?path=" + encodeURIComponent(path));
      var j = await r.json();
      if (j.error) {
        if (status()) status().set("conflict", j.error);
        return null;
      }
      diskMtime = j.mtime;
      lastSavedText = j.text;
      dirty = false;
      if (status()) status().set("saved", "saved");
      if (opts.onLoaded) opts.onLoaded(j);
      return j;
    }

    async function save() {
      var cm = opts.getCm && opts.getCm();
      if (!cm || !path) return false;
      if (opts.beforeSave) opts.beforeSave();
      if (status()) status().set("saved", "saving");
      var text = cm.getValue();
      var r = await fetch((window.AtelierRuntime&&AtelierRuntime.api)?AtelierRuntime.api("/codesave"):"/codesave", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: path, text: text, mtime: diskMtime }),
      });
      var j = await r.json();
      if (j.error === "conflit") {
        if (status())
          status().set(
            "conflict",
            "conflict: the file changed on disk (Claude?) — reload or re-save to overwrite"
          );
        diskMtime = j.mtime;
        return false;
      }
      if (j.error) {
        if (status()) status().set("conflict", j.error);
        return false;
      }
      diskMtime = j.mtime;
      dirty = false;
      if (status())
        status().set("saved", "saved " + new Date().toLocaleTimeString());
      var savedNow = cm.getValue();
      if (opts.diffVersions && lastSavedText !== null) {
        opts.diffVersions.push(lastSavedText, savedNow, {
          source: "user-save",
          status: "applied",
        });
      }
      lastSavedText = savedNow;
      if (opts.onSaved) opts.onSaved(j);
      return true;
    }

    function effectivelyClean() {
      if (!dirty) return true;
      var cm = opts.getCm && opts.getCm();
      if (typeof lastSavedText !== "string" || !cm || !opts.diffVersions) return false;
      return opts.diffVersions.isEquivalent(cm.getValue(), lastSavedText);
    }

    async function pollExternal() {
      if (destroyed || !path) return;
      var cm = opts.getCm && opts.getCm();
      if (!cm) return;
      if (opts.diffVersions && opts.diffVersions.isBusy && opts.diffVersions.isBusy()) return;
      try {
        var r = await fetch((window.AtelierRuntime&&AtelierRuntime.api)?AtelierRuntime.api("/code?path="):"/code?path=" + encodeURIComponent(path));
        var j = await r.json();
        if (!(j.mtime && Math.abs(j.mtime - diskMtime) > 0.001)) return;
        var diskText = j.text;
        var cur = cm.getCursor();
        var scroll = cm.getScrollInfo();
        if (effectivelyClean()) {
          diskMtime = j.mtime;
          var before = typeof lastSavedText === "string" ? lastSavedText : cm.getValue();
          if (before === diskText && cm.getValue() === diskText) {
            lastSavedText = diskText;
            dirty = false;
            return;
          }
          cm.setValue(diskText);
          cm.setCursor(cur);
          cm.scrollTo(scroll.left, scroll.top);
          dirty = false;
          if (status())
            status().set(
              "saved",
              "reloaded (modified on disk) " + new Date().toLocaleTimeString()
            );
          if (opts.diffVersions)
            opts.diffVersions.push(before, diskText, {
              source: "external-reload",
              status: "applied",
            });
          lastSavedText = diskText;
          return;
        }
        var base = typeof lastSavedText === "string" ? lastSavedText : null;
        var merged =
          base === null
            ? false
            : global.Diff &&
              Diff.applyPatch(
                cm.getValue(),
                Diff.structuredPatch("a", "b", base, diskText, "", ""),
                { fuzzFactor: 2 }
              );
        if (merged === false || typeof merged !== "string") {
          if (j.mtime !== mergeConflictAt) {
            mergeConflictAt = j.mtime;
            if (base != null && opts.diffVersions && base !== diskText) {
              opts.diffVersions.push(base, diskText, {
                source: "external-conflict",
                status: "pending-conflict",
              });
            }
            if (status())
              status().set(
                "conflict",
                "modifs superposées avec l'agent — ⌘S affichera le conflit · timeline : intervention enregistrée"
              );
          }
          return;
        }
        diskMtime = j.mtime;
        var beforePush = base != null ? base : cm.getValue();
        cm.setValue(merged);
        cm.setCursor(cur);
        cm.scrollTo(scroll.left, scroll.top);
        // Merged result is in-memory only until the user saves — keep dirty chrome.
        dirty = true;
        if (status())
          status().set(
            "dirty",
            "modifs de l'agent fusionnées avec les tiennes (non sauvegardées)"
          );
        if (opts.diffVersions)
          opts.diffVersions.push(beforePush, diskText, {
            source: "external-merge",
            status: "applied",
          });
        lastSavedText = diskText;
      } catch (_) {}
    }

    function startPolling(ms) {
      stopPolling();
      pollTimer = setInterval(pollExternal, ms || 2000);
    }

    function stopPolling() {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }

    function markDirty() {
      setDirty(true, "modified");
    }

    function getDiskMtime() {
      return diskMtime;
    }

    function setDiskMtime(m) {
      diskMtime = m;
    }

    function getLastSaved() {
      return lastSavedText;
    }

    function setLastSaved(t) {
      lastSavedText = t;
    }

    function isDirty() {
      return dirty;
    }

    function destroy() {
      destroyed = true;
      stopPolling();
    }

    return {
      load: load,
      save: save,
      startPolling: startPolling,
      stopPolling: stopPolling,
      markDirty: markDirty,
      setDirty: setDirty,
      getDiskMtime: getDiskMtime,
      setDiskMtime: setDiskMtime,
      getLastSaved: getLastSaved,
      setLastSaved: setLastSaved,
      isDirty: isDirty,
      destroy: destroy,
    };
  }

  global.AtelierPersistence = { create: create };
})(typeof window !== "undefined" ? window : globalThis);
