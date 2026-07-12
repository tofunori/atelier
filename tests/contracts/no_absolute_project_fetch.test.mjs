/**
 * Ban bare absolute project API fetches in first-party assets.
 * Allowed: AtelierRuntime.api(...), /assets/*, vendored bundles.
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(fileURLToPath(new URL(".", import.meta.url)), "../../assets");
const SKIP_DIR = new Set([
  "cm",
  "cm6",
  "pdfjs",
  "toastui",
  "notes",
  "whiteboard",
]);
const SKIP_FILE = new Set([
  "diff.min.js",
  "marked.min.js",
  "purify.min.js",
  "texvisual.min.js",
]);

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (name.startsWith(".")) continue;
    const path = join(dir, name);
    const st = statSync(path);
    if (st.isDirectory()) {
      if (SKIP_DIR.has(name)) continue;
      walk(path, out);
    } else if (/\.(js|html)$/.test(name) && !SKIP_FILE.has(name)) {
      out.push(path);
    }
  }
  return out;
}

// Bare fetch('/api...') without AtelierRuntime.api on the same call.
const BARE = /(?<!AtelierRuntime\.api\()(?<!projectUrl\()(?<!eventsUrl\()\bfetch\(\s*(['"`])\/(?!assets\/|p\/|open\/|healthz|version)/g;

test("no bare absolute project fetch() in first-party assets", () => {
  const offenders = [];
  for (const file of walk(root)) {
    const rel = relative(root, file);
    if (rel === "atelier_runtime.js" || rel === "atelier_events.js") continue;
    const text = readFileSync(file, "utf8");
    // Reset lastIndex for global regex
    BARE.lastIndex = 0;
    let m;
    while ((m = BARE.exec(text))) {
      const line = text.slice(0, m.index).split("\n").length;
      // Allow lines that already route through AtelierRuntime nearby on same line
      const lineText = text.split("\n")[line - 1] || "";
      if (
        lineText.includes("AtelierRuntime.api") ||
        lineText.includes("projectUrl(") ||
        lineText.includes("eventsUrl")
      ) {
        continue;
      }
      offenders.push(`${rel}:${line}: ${lineText.trim().slice(0, 120)}`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    `absolute project fetch() must use AtelierRuntime.api():\n${offenders.join("\n")}`
  );
});

test("atelier_runtime.js patches fetch and rewrites fig_thumbs in daemon mode", () => {
  const src = readFileSync(join(root, "atelier_runtime.js"), "utf8");
  assert.match(src, /global\.fetch\s*=\s*function/);
  assert.match(src, /function rewriteUrl/);
  assert.match(src, /\/\.fig_thumbs\//);
  assert.match(src, /HTMLScriptElement/);
  assert.match(src, /HTMLIFrameElement/);
});

test("dynamic query values are assembled before AtelierRuntime.api()", () => {
  const offenders = [];
  const incompleteDynamicQuery =
    /AtelierRuntime\.api\(\s*(["'`])\/(?:code\?path|snippet\?path|ls\?dir|pdfannot\?rel|git(?:log|show|head)\?path|versions\?path|lint\?path|texroot\?path)=\1\s*\)/g;
  for (const file of walk(root)) {
    const rel = relative(root, file);
    const text = readFileSync(file, "utf8");
    incompleteDynamicQuery.lastIndex = 0;
    let match;
    while ((match = incompleteDynamicQuery.exec(text))) {
      const line = text.slice(0, match.index).split("\n").length;
      offenders.push(`${rel}:${line}`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    `AtelierRuntime.api() received a query name without its dynamic value:\n${offenders.join("\n")}`
  );
});
