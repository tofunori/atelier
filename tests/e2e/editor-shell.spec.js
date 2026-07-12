/**
 * Unified editor shell — behavioural smoke for code surface.
 */
import { test, expect } from "@playwright/test";
import { createServer } from "node:http";
import { readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const assets = join(root, "assets");
const fixtures = join(root, "tests", "fixtures", "editor");

function contentType(p) {
  if (p.endsWith(".js")) return "application/javascript; charset=utf-8";
  if (p.endsWith(".css")) return "text/css; charset=utf-8";
  if (p.endsWith(".html")) return "text/html; charset=utf-8";
  if (p.endsWith(".json")) return "application/json";
  return "application/octet-stream";
}

test.describe("Editor shell (code)", () => {
  let server;
  let baseURL;
  /** @type {Map<string, {text:string, mtime:number}>} */
  let files;
  let quotes;

  test.beforeAll(async () => {
    files = new Map();
    quotes = [];
    for (const name of ["sample.rs", "sample.py", "sample.md"]) {
      const text = readFileSync(join(fixtures, name), "utf8");
      files.set("/tmp/atelier-test/" + name, { text, mtime: 1 });
    }
    files.set("/tmp/atelier-test/jitter.rs", {
      text: readFileSync(join(fixtures, "sample.rs"), "utf8"),
      mtime: 1,
    });

    server = createServer((req, res) => {
      const url = new URL(req.url || "/", "http://127.0.0.1");
      // API stubs
      if (url.pathname === "/code") {
        const p = url.searchParams.get("path") || "";
        const f = files.get(p);
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify(f ? { text: f.text, mtime: f.mtime } : { error: "missing" }));
        return;
      }
      if (url.pathname === "/codesave" && req.method === "POST") {
        let body = "";
        req.on("data", (c) => (body += c));
        req.on("end", () => {
          const j = JSON.parse(body || "{}");
          const cur = files.get(j.path);
          if (cur && j.mtime != null && Math.abs(j.mtime - cur.mtime) > 0.001) {
            res.writeHead(200, { "Content-Type": "application/json" });
            res.end(JSON.stringify({ error: "conflit", mtime: cur.mtime, text: cur.text }));
            return;
          }
          const next = { text: j.text, mtime: (cur?.mtime || 1) + 1 };
          files.set(j.path, next);
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ ok: true, mtime: next.mtime }));
        });
        return;
      }
      if (url.pathname === "/selinfo" && req.method === "POST") {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: true }));
        return;
      }
      if (url.pathname === "/quote" && req.method === "POST") {
        let body = "";
        req.on("data", (c) => (body += c));
        req.on("end", () => {
          const quote = JSON.parse(body || "{}");
          quotes.push(quote);
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({
            ok: true,
            queuedForAgent: true,
            agentSelectionStatus: quote.held ? "staged" : "sent",
            message: "selection sent",
          }));
        });
        return;
      }
      if (url.pathname === "/agent-status") {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({
          agentHost: null,
          consumers: [],
          counts: { queued: 0, staged: quotes.filter((q) => q.held).length },
          pending: quotes.filter((q) => q.held).map((q, index) => ({
            id: String(index + 1),
            path: q.rel,
            page: q.page,
            comment: q.comment,
            selection: q.text,
            held: true,
            status: "staged",
          })),
          history: [],
        }));
        return;
      }
      if (url.pathname === "/versions") {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ versions: [] }));
        return;
      }
      if (url.pathname === "/githead") {
        const p = url.searchParams.get("path") || "";
        const reply = () => {
          const f = files.get(p);
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify(f && p.endsWith("jitter.rs")
            ? { ok: true, text: f.text, sha: "fixture-head", ts: 1 }
            : { ok: false }));
        };
        if (p.endsWith("jitter.rs")) setTimeout(reply, 500);
        else reply();
        return;
      }

      let rel = url.pathname;
      if (rel.startsWith("/.fig_thumbs/")) rel = rel.slice("/.fig_thumbs/".length);
      else if (rel.startsWith("/")) rel = rel.slice(1);
      if (!rel) rel = "code_editor.html";
      const file = join(assets, rel);
      if (!file.startsWith(assets) || !existsSync(file)) {
        res.writeHead(404);
        res.end("not found " + rel);
        return;
      }
      res.writeHead(200, { "Content-Type": contentType(file) });
      res.end(readFileSync(file));
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    baseURL = `http://127.0.0.1:${server.address().port}`;
  });

  test.afterAll(async () => {
    await new Promise((resolve) => server.close(resolve));
  });

  test("opens Rust file with CM6 shell, not CM5", async ({ page }) => {
    const path = "/tmp/atelier-test/sample.rs";
    await page.goto(
      baseURL + "/code_editor.html?path=" + encodeURIComponent(path)
    );
    await page.waitForFunction(() => window.__ATELIER_SHELL__ != null, null, {
      timeout: 15000,
    });
    const info = await page.evaluate(() => {
      const shell = window.__ATELIER_SHELL__;
      const cm = shell.cm();
      return {
        surface: shell.surface,
        ext: shell.ext,
        engine: window.AtelierEditor?.engine?.(),
        value: cm?.getValue?.() || "",
        hasCmEditor: !!document.querySelector(".cm-editor"),
        hasCm5: !!document.querySelector(".CodeMirror-code") && !document.querySelector(".cm-editor"),
        stateLabel: document.getElementById("state")?.getAttribute("aria-label") || "",
        stateVisible: getComputedStyle(document.getElementById("state")).display !== "none",
      };
    });
    expect(info.surface).toBe("code");
    expect(info.ext).toBe("rs");
    expect(info.engine).toBe("cm6");
    expect(info.hasCmEditor).toBe(true);
    expect(info.hasCm5).toBe(false);
    expect(info.value).toContain("fn main");
    expect(info.stateLabel).toBe("");
    expect(info.stateVisible).toBe(false);
  });

  test("Rust keyword token is highlighted", async ({ page }) => {
    const path = "/tmp/atelier-test/sample.rs";
    await page.goto(
      baseURL + "/code_editor.html?path=" + encodeURIComponent(path)
    );
    await page.waitForFunction(() => window.__ATELIER_SHELL__?.cm?.(), null, {
      timeout: 15000,
    });
    await page.waitForSelector(".tok-keyword, .cm-keyword", { timeout: 10000 });
    const color = await page.evaluate(() => {
      const el =
        document.querySelector(".tok-keyword") ||
        document.querySelector(".cm-keyword");
      return el ? getComputedStyle(el).color : null;
    });
    expect(color).toBeTruthy();
    expect(color).not.toBe("rgb(0, 0, 0)");
  });

  test("delayed Git gutter does not shift code after reload", async ({ page }) => {
    const path = "/tmp/atelier-test/jitter.rs";
    await page.goto(
      baseURL + "/code_editor.html?path=" + encodeURIComponent(path)
    );
    await page.waitForFunction(() => window.__ATELIER_SHELL__?.cm?.(), null, {
      timeout: 15000,
    });
    const before = await page.locator(".cm-content").evaluate((el) =>
      el.getBoundingClientRect().left
    );
    await page.waitForTimeout(800);
    const after = await page.locator(".cm-content").evaluate((el) =>
      el.getBoundingClientRect().left
    );
    expect(after).toBe(before);
    await expect(page.locator(".cm-gutter.dv-git")).toBeAttached();
  });

  test("selection composer stages or sends an annotation", async ({ page }) => {
    quotes.length = 0;
    const path = "/tmp/atelier-test/sample.rs";
    await page.goto(
      baseURL + "/code_editor.html?path=" + encodeURIComponent(path)
    );
    await page.waitForFunction(() => window.__ATELIER_SHELL__?.cm?.(), null, {
      timeout: 15000,
    });
    await expect(page.locator("#atelierAgentButton")).toBeVisible();
    await page.evaluate(() => {
      const cm = window.__ATELIER_SHELL__.cm();
      cm.setSelection({ line: 1, ch: 2 }, { line: 1, ch: 9 });
    });

    const composer = page.locator("#atelierSelectionComposer");
    await expect(composer).toBeVisible();
    const anchorBackground = await page.locator(".cm-clsel").first().evaluate((el) =>
      getComputedStyle(el).backgroundColor
    );
    expect(anchorBackground).toBe("rgba(0, 0, 0, 0)");
    const selectionLayers = await page.locator(".cm-selectionBackground").evaluateAll((els) =>
      els.map((el) => getComputedStyle(el).backgroundColor)
    );
    expect(selectionLayers.every((color) => color === "rgba(79, 126, 190, 0.46)")).toBe(true);
    await composer.locator("textarea").fill("Vérifie cette sélection");
    await composer.locator("button.stage").click();
    await expect.poll(() => quotes.length).toBe(1);
    expect(quotes[0]).toMatchObject({
      rel: path,
      comment: "Vérifie cette sélection",
      direct: false,
      held: true,
      action: "ask",
    });
    expect(quotes[0].text).toBeTruthy();

    await page.waitForTimeout(800);
    await page.evaluate(() => {
      const cm = window.__ATELIER_SHELL__.cm();
      cm.setSelection({ line: 2, ch: 2 }, { line: 2, ch: 10 });
    });
    await expect(composer).toBeVisible();
    await composer.locator("button.send").click();
    await expect.poll(() => quotes.length).toBe(2);
    expect(quotes[1]).toMatchObject({
      rel: path,
      direct: true,
      held: false,
      action: "apply",
    });
  });

  test("clicking outside clears the annotation selection and composer", async ({ page }) => {
    const path = "/tmp/atelier-test/sample.rs";
    await page.goto(
      baseURL + "/code_editor.html?path=" + encodeURIComponent(path)
    );
    await page.waitForFunction(() => window.__ATELIER_SHELL__?.cm?.(), null, {
      timeout: 15000,
    });
    await page.evaluate(() => {
      const cm = window.__ATELIER_SHELL__.cm();
      cm.setSelection({ line: 1, ch: 2 }, { line: 1, ch: 9 });
    });
    const composer = page.locator("#atelierSelectionComposer");
    await expect(composer).toBeVisible();
    await page.locator("#fname").click();
    await expect(composer).toBeHidden();
    await expect.poll(() => page.evaluate(() =>
      window.__ATELIER_SHELL__.cm().getSelection()
    )).toBe("");
  });

  test("save updates state and persists text", async ({ page }) => {
    const path = "/tmp/atelier-test/sample.py";
    await page.goto(
      baseURL + "/code_editor.html?path=" + encodeURIComponent(path)
    );
    await page.waitForFunction(() => window.__ATELIER_SHELL__?.cm?.(), null, {
      timeout: 15000,
    });
    await page.evaluate(() => {
      const cm = window.__ATELIER_SHELL__.cm();
      cm.setValue(cm.getValue() + "\n# edited\n");
    });
    await page.keyboard.press("Meta+s");
    await expect.poll(() => files.get(path)?.text || "").toContain("# edited");
    // reload
    await page.goto(
      baseURL + "/code_editor.html?path=" + encodeURIComponent(path)
    );
    await page.waitForFunction(() => window.__ATELIER_SHELL__?.cm?.(), null, {
      timeout: 15000,
    });
    const value = await page.evaluate(() => window.__ATELIER_SHELL__.cm().getValue());
    expect(value).toContain("# edited");
  });

  test("markdown shell mounts preview", async ({ page }) => {
    const path = "/tmp/atelier-test/sample.md";
    await page.goto(
      baseURL + "/md_viewer.html?path=" + encodeURIComponent(path)
    );
    await page.waitForFunction(() => window.__ATELIER_SHELL__ != null, null, {
      timeout: 15000,
    });
    const info = await page.evaluate(() => ({
      surface: window.__ATELIER_SHELL__.surface,
      hasPreview: !!document.querySelector(".md-preview"),
      hasCm: !!document.querySelector(".cm-editor"),
    }));
    expect(info.surface).toBe("markdown");
    expect(info.hasPreview).toBe(true);
    expect(info.hasCm).toBe(true);
  });

  test("shell modules exist on disk", async () => {
    for (const rel of [
      "editor/shell.js",
      "editor/languages.js",
      "editor/modules/code.js",
      "editor/modules/latex.js",
      "editor/modules/markdown.js",
      "cm6/editor.bundle.js",
      "editor_factory.js",
    ]) {
      expect(existsSync(join(assets, rel))).toBeTruthy();
    }
  });

  test("wrap modes: window, off, fixed column", async ({ page }) => {
    const path = "/tmp/atelier-test/sample.rs";
    await page.goto(
      baseURL + "/code_editor.html?path=" + encodeURIComponent(path)
    );
    await page.waitForFunction(() => window.__ATELIER_SHELL__?.cm?.(), null, {
      timeout: 15000,
    });

    // window wrap
    await page.selectOption("#wrapSel", "win");
    let wrapping = await page.evaluate(() =>
      window.__ATELIER_SHELL__.cm().getOption("lineWrapping")
    );
    expect(wrapping).toBe(true);

    // off
    await page.selectOption("#wrapSel", "off");
    wrapping = await page.evaluate(() =>
      window.__ATELIER_SHELL__.cm().getOption("lineWrapping")
    );
    expect(wrapping).toBe(false);
    const maxOff = await page.evaluate(() => {
      const w = window.__ATELIER_SHELL__.cm().getWrapperElement();
      return w.style.maxWidth || "";
    });
    expect(maxOff).toBe("");

    // fixed 80
    await page.selectOption("#wrapSel", "80");
    wrapping = await page.evaluate(() =>
      window.__ATELIER_SHELL__.cm().getOption("lineWrapping")
    );
    expect(wrapping).toBe(true);
    const max80 = await page.evaluate(() => {
      const w = window.__ATELIER_SHELL__.cm().getWrapperElement();
      return w.style.maxWidth || "";
    });
    expect(max80).toContain("80ch");
  });

  test("rewrap only mutates pure comment blocks", async ({ page }) => {
    const path = "/tmp/atelier-test/sample.py";
    files.set(path, {
      text:
        // Blank line separates comment paragraph from code so rewrap does not
        // treat the whole file as one mixed block (safety rule).
        "# this is a very long comment that should rewrap when the column is small enough for testing purposes only\n" +
        "\n" +
        "def answer() -> int:\n" +
        "    return 42\n",
      mtime: 1,
    });
    await page.goto(
      baseURL + "/code_editor.html?path=" + encodeURIComponent(path)
    );
    await page.waitForFunction(() => window.__ATELIER_SHELL__?.cm?.(), null, {
      timeout: 15000,
    });
    const after = await page.evaluate(() => {
      const sel = document.getElementById("wrapSel");
      if (!sel.querySelector('option[value="40"]')) {
        const opt = document.createElement("option");
        opt.value = "40";
        opt.textContent = "Wrap: 40";
        sel.insertBefore(opt, sel.querySelector('option[value="custom"]'));
      }
      sel.value = "40";
      sel.dispatchEvent(new Event("change", { bubbles: true }));
      const cm = window.__ATELIER_SHELL__.cm();
      cm.setCursor({ line: 0, ch: 0 });
      const result = window.__ATELIER_SHELL__.module.rewrap(false);
      return { text: cm.getValue(), result };
    });
    expect(after.result?.ok).toBe(true);
    const commentLines = after.text.split("\n").filter((l) => l.trim().startsWith("#"));
    expect(commentLines.length).toBeGreaterThan(1);
    expect(after.text).toContain("def answer() -> int:");
    expect(after.text).toContain("return 42");
  });

  test("toolbar fits at 375px without horizontal page overflow", async ({ page }) => {
    const path = "/tmp/atelier-test/sample.rs";
    await page.setViewportSize({ width: 375, height: 700 });
    await page.goto(
      baseURL + "/code_editor.html?path=" + encodeURIComponent(path)
    );
    await page.waitForFunction(() => window.__ATELIER_SHELL__ != null, null, {
      timeout: 15000,
    });
    await expect(page.locator("#wrapSel")).toBeVisible();
    await expect(page.locator("#rewrapBtn")).toBeVisible();
    await expect(page.locator("#autoRewrap")).toBeAttached();
    const overflow = await page.evaluate(() => {
      const doc = document.documentElement;
      return {
        scrollWidth: doc.scrollWidth,
        clientWidth: doc.clientWidth,
        hasCm: !!document.querySelector(".cm-editor"),
      };
    });
    expect(overflow.hasCm).toBe(true);
    // Allow 1px subpixel; no significant horizontal overflow of the page
    expect(overflow.scrollWidth - overflow.clientWidth).toBeLessThanOrEqual(2);
  });

  test("two simultaneous CM6 instances do not share volatile state", async ({ page }) => {
    // Static dual-host page using factory + core (no server load needed)
    await page.goto(baseURL + "/cm6-smoke.html");
    await page.waitForFunction(() => window.__CM6_SMOKE__ && window.__CM6_SMOKE__.done, null, {
      timeout: 15000,
    });
    const result = await page.evaluate(async () => {
      await window.AtelierEditor.ready;
      const a = document.createElement("div");
      a.id = "edA";
      a.style.cssText = "height:120px;display:flex;flex-direction:column";
      const b = document.createElement("div");
      b.id = "edB";
      b.style.cssText = "height:120px;display:flex;flex-direction:column";
      document.body.appendChild(a);
      document.body.appendChild(b);
      const cmA = window.AtelierEditor.create(a, {
        value: "fn a() {}\n",
        mode: "rust",
        lineWrapping: true,
      });
      const cmB = window.AtelierEditor.create(b, {
        value: "def b():\n  pass\n",
        mode: "python",
        lineWrapping: false,
      });
      cmA.setValue("fn a_only() { let x = 1; }\n");
      const valB = cmB.getValue();
      const valA = cmA.getValue();
      const wrapA = cmA.getOption("lineWrapping");
      const wrapB = cmB.getOption("lineWrapping");
      const modeA = cmA.getOption("mode");
      const modeB = cmB.getOption("mode");
      if (typeof cmA.destroy === "function") cmA.destroy();
      if (typeof cmB.destroy === "function") cmB.destroy();
      return { valA, valB, wrapA, wrapB, modeA, modeB };
    });
    expect(result.valA).toContain("a_only");
    expect(result.valB).toContain("def b()");
    expect(result.valB).not.toContain("a_only");
    expect(result.wrapA).toBe(true);
    expect(result.wrapB).toBe(false);
    expect(result.modeA).toMatch(/rust/i);
    expect(result.modeB).toMatch(/python/i);
  });

  test("user edit and unsaved merge use dirty state chrome", async ({ page }) => {
    const path = "/tmp/atelier-test/sample.py";
    files.set(path, {
      text: "print('base')\n",
      mtime: 10,
    });
    await page.goto(
      baseURL + "/code_editor.html?path=" + encodeURIComponent(path)
    );
    await page.waitForFunction(() => window.__ATELIER_SHELL__?.cm?.(), null, {
      timeout: 15000,
    });
    // setValue uses origin setValue (clean load path) — real edits use replaceRange
    await page.evaluate(() => {
      const cm = window.__ATELIER_SHELL__.cm();
      cm.replaceRange(
        "print('local')\n",
        { line: 0, ch: 0 },
        { line: cm.lineCount() - 1, ch: cm.getLine(cm.lineCount() - 1).length }
      );
    });
    await expect(page.locator("#state")).toHaveClass(/dirty/);
    const dirtyLabel = await page.locator("#state").getAttribute("aria-label");
    expect(dirtyLabel).toMatch(/modified/i);

    // Simulate the post-merge status path: dirty buffer + non-sauvegardé message
    await page.evaluate(() => {
      const st = window.__ATELIER_SHELL__.status;
      st.set(
        "dirty",
        "modifs de l'agent fusionnées avec les tiennes (non sauvegardées)"
      );
    });
    await expect(page.locator("#state")).toHaveClass(/dirty/);
    await expect(page.locator("#state")).not.toHaveClass(/saved/);
    const mergeLabel = await page.locator("#state").getAttribute("aria-label");
    expect(mergeLabel).toMatch(/non sauvegardées/i);
  });
});
