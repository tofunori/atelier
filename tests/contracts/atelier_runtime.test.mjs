import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const root = join(dirname(fileURLToPath(import.meta.url)), "../..");
const runtimeSrc = readFileSync(join(root, "assets/atelier_runtime.js"), "utf8");

function loadRuntime({ bootstrap = null, pathname = "/" } = {}) {
  const document = {
    getElementById(id) {
      if (id !== "atelier-bootstrap" || !bootstrap) return null;
      return { textContent: JSON.stringify(bootstrap) };
    },
  };
  const window = {
    document,
    location: {
      pathname,
      origin: "http://127.0.0.1:9359",
    },
    console,
  };
  window.window = window;
  vm.runInNewContext(runtimeSrc, window, { filename: "atelier_runtime.js" });
  return window.AtelierRuntime;
}

test("legacy mode exposes empty basePath and absolute-style api paths", () => {
  const rt = loadRuntime();
  assert.equal(rt.ready, true);
  assert.equal(rt.legacy, true);
  assert.equal(rt.basePath, "");
  assert.equal(rt.api("/code"), "/code");
  assert.equal(rt.asset("/cm/codemirror.min.js"), "/cm/codemirror.min.js");
});

test("daemon bootstrap prefixes api and assets", () => {
  const key = "93d2d746e45091146c34dc75";
  const rt = loadRuntime({
    bootstrap: {
      projectKey: key,
      basePath: `/p/${key}`,
      apiBase: `/p/${key}`,
      assetBase: "/assets",
      daemonInstance: "inst-1",
    },
    pathname: `/p/${key}/figures_index.html`,
  });
  assert.equal(rt.ready, true);
  assert.equal(rt.legacy, false);
  assert.equal(rt.projectKey, key);
  assert.equal(rt.api("/code"), `/p/${key}/code`);
  assert.equal(rt.asset("gallery_template.html"), "/assets/gallery_template.html");
  assert.equal(rt.rewriteUrl("/.fig_thumbs/agent_bridge_ui.js"), `/p/${key}/.fig_thumbs/agent_bridge_ui.js`);
  assert.equal(rt.relativePath(`/p/${key}/notes.md`), "notes.md");
  assert.match(rt.openEditor("scripts/a.py", "code"), new RegExp(`/p/${key}/\\.fig_thumbs/code_editor`));
  assert.match(rt.openEditor("scripts/a.py", "code"), /path=scripts/);
});

test("bootstrap/path mismatch refuses mutations", () => {
  const rt = loadRuntime({
    bootstrap: {
      projectKey: "aaaaaaaaaaaaaaaaaaaaaaaa",
      basePath: "/p/aaaaaaaaaaaaaaaaaaaaaaaa",
      apiBase: "/p/aaaaaaaaaaaaaaaaaaaaaaaa/api/v1",
      assetBase: "/assets/x",
      daemonInstance: "i",
    },
    pathname: "/p/bbbbbbbbbbbbbbbbbbbbbbbb/figures_index.html",
  });
  assert.equal(rt.ready, false);
  assert.match(rt.error, /mismatch/i);
});
