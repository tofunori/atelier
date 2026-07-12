/**
 * Selection → /selinfo + persistent agent mark.
 */
(function (global) {
  "use strict";

  function bind(cm, opts) {
    opts = opts || {};
    var path = opts.path || "";
    var selT = null;
    var mark = null;
    var destroyed = false;
    var lastSel = null;
    var pill = null;
    var textarea = null;

    function ensureComposer() {
      if (pill) return pill;
      if (!document.getElementById("atelierSelectionComposerStyles")) {
        var style = document.createElement("style");
        style.id = "atelierSelectionComposerStyles";
        style.textContent =
          "#atelierSelectionComposer{position:fixed;z-index:500;display:none;align-items:center;gap:5px;width:min(330px,calc(100vw - 24px));box-sizing:border-box;padding:6px 7px 6px 12px;background:var(--card);border:1px solid rgba(255,255,255,.1);border-radius:18px;box-shadow:0 12px 34px rgba(0,0,0,.42)}" +
          "#atelierSelectionComposer textarea{flex:1;min-width:0;height:24px;max-height:84px;padding:3px 0;border:0;outline:0;resize:none;background:transparent;color:var(--txt);font:13px/1.4 var(--ui-font)}" +
          "#atelierSelectionComposer textarea::placeholder{color:var(--muted)}" +
          "#atelierSelectionComposer button{box-sizing:border-box;width:28px;height:28px;display:grid;place-items:center;flex:none;padding:0;border:0;border-radius:50%;background:transparent;color:var(--muted);cursor:pointer;transition:transform 90ms ease-out,background-color 120ms ease-out,color 120ms ease-out}" +
          "#atelierSelectionComposer button svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.45;stroke-linecap:round;stroke-linejoin:round}" +
          "#atelierSelectionComposer button:hover{background:rgba(255,255,255,.06);color:var(--txt)}" +
          "#atelierSelectionComposer button:active{transform:scale(.94)}" +
          "#atelierSelectionComposer .send{background:#aeb5c3;color:#292e38}" +
          "#atelierSelectionComposer .send:hover{background:#c1c7d2;color:#20242c}" +
          "#atelierSelectionComposer button.sending{opacity:.65;pointer-events:none}" +
          "#atelierSelectionComposer button.sent{background:#739b80;color:#17221b}" +
          "#atelierSelectionComposer button.failed{background:#a96f68;color:#281714}";
        document.head.appendChild(style);
      }
      pill = document.createElement("div");
      pill.id = "atelierSelectionComposer";
      pill.innerHTML =
        '<textarea rows="1" placeholder="Ajouter un commentaire…" aria-label="Commentaire de l’annotation"></textarea>' +
        '<button class="stage" type="button" aria-label="Ajouter aux annotations" title="Ajouter aux annotations"><svg viewBox="0 0 24 24"><path d="M5 5h14v14H5z"/><path d="M8 12h2l1.3 2h1.4L14 12h2"/></svg></button>' +
        '<button class="send" type="button" aria-label="Envoyer directement au chat" title="Envoyer directement au chat"><svg viewBox="0 0 24 24"><path d="m6 12 4 4 8-9"/></svg></button>';
      document.body.appendChild(pill);
      textarea = pill.querySelector("textarea");
      pill.addEventListener("mousedown", function (e) {
        if (e.target !== textarea) e.preventDefault();
      });
      pill.querySelector(".stage").onclick = function (e) {
        sendSelection("annotations", e.currentTarget);
      };
      pill.querySelector(".send").onclick = function (e) {
        sendSelection("direct", e.currentTarget);
      };
      textarea.addEventListener("input", function () {
        textarea.style.height = "24px";
        textarea.style.height = Math.min(84, textarea.scrollHeight) + "px";
      });
      textarea.addEventListener("keydown", function (e) {
        e.stopPropagation();
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          sendSelection("annotations", pill.querySelector(".stage"));
        } else if (e.key === "Escape") {
          e.preventDefault();
          clearAgentSelection();
        }
      });
      return pill;
    }

    function target() {
      try {
        return JSON.parse(localStorage.getItem("claudeTargetV1") || "null");
      } catch (_) {
        return null;
      }
    }

    function placeComposer(to) {
      var el = ensureComposer();
      var c = cm.charCoords(to, "window");
      el.style.display = "flex";
      var w = el.offsetWidth;
      var h = el.offsetHeight;
      var x = Math.min(Math.max(8, c.left - w / 2), innerWidth - w - 8);
      var y = c.bottom + 9;
      if (y + h > innerHeight - 8) y = c.top - h - 9;
      el.style.left = x + "px";
      el.style.top = Math.max(8, y) + "px";
    }

    function hideComposer() {
      if (pill) pill.style.display = "none";
    }

    function sendSelection(delivery, button) {
      if (destroyed || !lastSel || !button) return;
      var direct = delivery === "direct";
      var old = button.innerHTML;
      button.classList.remove("sent", "failed");
      button.classList.add("sending");
      button.textContent = "…";
      fetch((window.AtelierRuntime&&AtelierRuntime.api)?AtelierRuntime.api("/quote"):"/quote", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          rel: path,
          page: lastSel.page,
          text: lastSel.text,
          comment: textarea ? textarea.value.trim() : "",
          direct: direct,
          action: direct ? "apply" : "ask",
          held: !direct,
          deliveryMode: delivery,
          target: target(),
          embed: window.self !== window.top,
        }),
      })
        .then(function (r) {
          return r.json().then(function (j) {
            if (!r.ok || j.error) throw new Error(j.error || "HTTP " + r.status);
            return j;
          });
        })
        .then(function (j) {
          if (destroyed) return;
          if (direct && j && j.message && window.parent !== window) {
            window.parent.postMessage(
              { type: "atelier-add-to-chat", text: j.message },
              "*"
            );
          }
          button.classList.remove("sending");
          button.classList.add("sent");
          button.textContent = "✓";
          setTimeout(function () {
            if (destroyed) return;
            button.classList.remove("sent");
            button.innerHTML = old;
            if (textarea) textarea.value = "";
            hideComposer();
            clearMark();
          }, 700);
        })
        .catch(function () {
          if (destroyed) return;
          button.classList.remove("sending");
          button.classList.add("failed");
          button.textContent = "!";
          setTimeout(function () {
            if (destroyed) return;
            button.classList.remove("failed");
            button.innerHTML = old;
          }, 1400);
        });
    }

    function clearMark() {
      var previous = mark;
      if (mark) {
        try {
          mark.clear();
        } catch (_) {}
        mark = null;
      }
      if (global._clMark === previous) global._clMark = null;
    }

    function pushSel() {
      if (destroyed || !cm) return;
      var sel = cm.getSelection();
      if (sel && sel.trim()) {
        var f = cm.getCursor("from");
        var t = cm.getCursor("to");
        clearMark();
        mark = cm.markText(f, t, { className: "cm-clsel" });
        global._clMark = mark;
        lastSel = {
          text: sel,
          page: "L" + (f.line + 1) + "-" + (t.line + 1),
          from: f,
          to: t,
        };
        if (!(pill && document.activeElement === textarea && textarea.value)) {
          placeComposer(t);
        }
        fetch((window.AtelierRuntime&&AtelierRuntime.api)?AtelierRuntime.api("/selinfo"):"/selinfo", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            lines: t.line - f.line + 1,
            words: sel.trim().split(/\s+/).length,
            text: sel,
            rel: path,
            name: path.split("/").pop(),
            page: lastSel.page,
          }),
        }).catch(function () {});
      } else {
        lastSel = null;
        hideComposer();
      }
    }

    function onCursor() {
      clearTimeout(selT);
      selT = setTimeout(pushSel, 200);
    }

    function onDocumentPointerDown(e) {
      if (destroyed || (pill && pill.contains(e.target))) return;
      // Keep the mark only while the user is actively composing an annotation.
      // A click anywhere else cancels the annotation selection. Delay until the
      // click has completed so CodeMirror can place its own cursor normally.
      setTimeout(function () {
        if (destroyed) return;
        clearAgentSelection();
      }, 0);
    }

    function clearAgentSelection() {
      if (cm) cm.setCursor(cm.getCursor());
      clearMark();
      lastSel = null;
      if (textarea) textarea.value = "";
      hideComposer();
      fetch((window.AtelierRuntime&&AtelierRuntime.api)?AtelierRuntime.api("/selinfo"):"/selinfo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lines: 0, words: 0 }),
      }).catch(function () {});
    }

    cm.on("cursorActivity", onCursor);
    document.addEventListener("pointerdown", onDocumentPointerDown, true);

    function destroy() {
      destroyed = true;
      clearTimeout(selT);
      try {
        cm.off("cursorActivity", onCursor);
      } catch (_) {}
      document.removeEventListener("pointerdown", onDocumentPointerDown, true);
      clearMark();
      if (pill && pill.parentNode) pill.parentNode.removeChild(pill);
      pill = null;
      textarea = null;
    }

    return { clearAgentSelection: clearAgentSelection, destroy: destroy, pushSel: pushSel };
  }

  global.AtelierSelection = { bind: bind };
})(typeof window !== "undefined" ? window : globalThis);
