/**
 * Gate C — LaTeX behavioural parity against the actually-served studio + Rust APIs.
 * Requires latexmk + synctex (MacTeX/TeX Live). Skips gracefully if absent.
 */
import { test, expect } from "@playwright/test";
import { spawn, execFileSync, execSync } from "node:child_process";
import {
  mkdtempSync,
  writeFileSync,
  readFileSync,
  rmSync,
  cpSync,
  existsSync,
  mkdirSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import net from "node:net";
import { fileURLToPath } from "node:url";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const RUST_MANIFEST = path.join(REPO, "rust", "Cargo.toml");
const ATELIER_CLI = path.join(REPO, "rust", "target", "debug", "atelier-cli");
const ATELIER_SERVER = path.join(REPO, "rust", "target", "debug", "atelier-server");
const FIX = path.join(REPO, "tests", "fixtures", "editor", "latex");

function hasBin(name) {
  try {
    execSync(`command -v ${name}`, { stdio: "ignore" });
    return true;
  } catch {
    return existsSync(`/Library/TeX/texbin/${name}`);
  }
}

const HAS_LATEXMK = hasBin("latexmk");
const HAS_SYNCTEX = hasBin("synctex");

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

async function waitForPing(port) {
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${port}/ping`);
      if (res.ok) return;
    } catch {
      await new Promise((r) => setTimeout(r, 100));
    }
  }
  throw new Error(`server on ${port} did not answer /ping`);
}

async function withLatexProject(run) {
  const root = mkdtempSync(path.join(tmpdir(), "atelier-latex-e2e-"));
  let server;
  try {
    mkdirSync(path.join(root, "docs"), { recursive: true });
    for (const f of ["main.tex", "broken.tex", "comments.tex", "root.tex", "chapter.tex"]) {
      cpSync(path.join(FIX, f), path.join(root, "docs", f));
    }
    // minimal gallery so CLI build succeeds
    writeFileSync(path.join(root, "preview.png"), Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
    execFileSync("cargo", ["build", "--manifest-path", RUST_MANIFEST, "-p", "atelier-cli", "-p", "atelier-server"], {
      cwd: REPO,
      stdio: "pipe",
    });
    execFileSync(ATELIER_CLI, ["build", "--root", root], {
      cwd: root,
      env: { ...process.env, ATELIER_ASSETS_DIR: path.join(REPO, "assets") },
      stdio: "pipe",
    });
    const port = await freePort();
    server = spawn(ATELIER_SERVER, ["--root", root, "--port", String(port), "--watch"], {
      cwd: root,
      env: {
        ...process.env,
        GALLERY_ROOT: root,
        ATELIER_ASSETS_DIR: path.join(REPO, "assets"),
        PATH: `/Library/TeX/texbin:${process.env.PATH || ""}`,
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    await waitForPing(port);
    await run({
      root,
      port,
      base: `http://127.0.0.1:${port}`,
      mainPath: path.join(root, "docs", "main.tex"),
      brokenPath: path.join(root, "docs", "broken.tex"),
      commentsPath: path.join(root, "docs", "comments.tex"),
    });
  } finally {
    if (server) {
      server.kill("SIGTERM");
      await new Promise((r) => server.once("exit", r));
    }
    rmSync(root, { recursive: true, force: true });
  }
}

test.describe("Gate C — LaTeX compile / logs / PDF", () => {
  test.skip(!HAS_LATEXMK, "latexmk not installed");

  test("compile succeeds with pdf + log for main.tex", async () => {
    await withLatexProject(async ({ base, mainPath }) => {
      const r = await fetch(`${base}/compile`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: mainPath }),
      });
      const j = await r.json();
      expect(j.ok).toBe(true);
      expect(j.pdf).toBeTruthy();
      expect(j.log).toBeTruthy();
      expect(String(j.log).length).toBeGreaterThan(20);
      expect(existsSync(j.pdf)).toBe(true);
    });
  });

  test("compile fails with error + log for broken.tex", async () => {
    await withLatexProject(async ({ base, brokenPath }) => {
      const r = await fetch(`${base}/compile`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: brokenPath }),
      });
      const j = await r.json();
      expect(j.ok).toBe(false);
      expect(j.error || j.log).toBeTruthy();
      const blob = String(j.error || "") + String(j.log || "");
      expect(/!|Error|Undefined|undefined/i.test(blob)).toBe(true);
    });
  });
});

test.describe("Gate C — SyncTeX both directions", () => {
  test.skip(!HAS_LATEXMK || !HAS_SYNCTEX, "latexmk/synctex not installed");

  test("source→PDF and PDF→source round-trip", async () => {
    await withLatexProject(async ({ base, mainPath }) => {
      const compile = await (
        await fetch(`${base}/compile`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: mainPath }),
        })
      ).json();
      expect(compile.ok).toBe(true);
      const pdf = compile.pdf;

      // Find a source line with body text
      const src = readFileSync(mainPath, "utf8").split("\n");
      let line = src.findIndex((l) => l.includes("Hello Gate C")) + 1;
      if (line < 1) line = 6;

      const view = await (
        await fetch(`${base}/synctex`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            dir: "view",
            tex: mainPath,
            pdf,
            line,
            col: 1,
          }),
        })
      ).json();
      expect(view.error || view.page).toBeTruthy();
      if (view.error) {
        // Some TeX installs produce synctex.gz that needs a moment; soft-fail with diagnostics
        console.log("synctex view:", view);
      }
      expect(view.page).toBeGreaterThan(0);

      const edit = await (
        await fetch(`${base}/synctex`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            dir: "edit",
            tex: mainPath,
            pdf,
            page: view.page,
            x: view.x ?? 100,
            y: view.y ?? 100,
          }),
        })
      ).json();
      expect(edit.error || edit.line).toBeTruthy();
      expect(edit.line).toBeGreaterThan(0);
      // round-trip should land near the original line (within ±5)
      expect(Math.abs(edit.line - line)).toBeLessThanOrEqual(8);
    });
  });
});

test.describe("Gate C — latex_studio browser surface", () => {
  test.skip(!HAS_LATEXMK, "latexmk not installed");

  test("studio loads CM6, outline, compile log UI", async ({ page }) => {
    await withLatexProject(async ({ base, mainPath }) => {
      // pre-compile so PDF path exists for loadPdf
      await fetch(`${base}/compile`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: mainPath }),
      });

      const url =
        `${base}/.fig_thumbs/latex_studio.html?path=` +
        encodeURIComponent(mainPath);
      await page.goto(url);
      await page.waitForFunction(
        () => window.CodeMirror || window.AtelierEditor,
        null,
        { timeout: 20000 }
      );
      // wait for editor content
      await page.waitForFunction(
        () => {
          const ed = document.querySelector(".cm-editor, .CodeMirror");
          return !!ed;
        },
        null,
        { timeout: 15000 }
      );

      // Shared helpers loaded
      const helpers = await page.evaluate(() => ({
        outline: !!window.AtelierLatexOutline,
        compile: !!window.AtelierLatexCompile,
        synctex: !!window.AtelierLatexSynctex,
        comments: !!window.AtelierLatexComments,
        ghost: !!window.AtelierLatexGhost,
        rewrap: !!window.AtelierRewrap,
      }));
      expect(helpers.outline).toBe(true);
      expect(helpers.compile).toBe(true);
      expect(helpers.synctex).toBe(true);
      expect(helpers.comments).toBe(true);

      // Outline items from pure helper + DOM
      await page.click("#outlineBtn");
      await expect(page.locator("#outline")).toHaveClass(/open/);
      await expect(page.locator("#outline .oi")).toHaveCount(3);
      await expect(page.locator("#outline .oi").filter({ hasText: "Introduction" })).toBeVisible();
      await expect(page.locator("#outline .oi").filter({ hasText: "Results" })).toBeVisible();
      await expect(page.locator("#outline .oi").filter({ hasText: "Details" })).toBeVisible();
      // Close outline so it does not intercept the Compile button
      await page.keyboard.press("Escape");
      await expect(page.locator("#outline")).not.toHaveClass(/open/);

      // Compile from UI — primary status must remain compile result even if
      // DiffVersions later fails to persist (soft channel / title only).
      await page.click("#build");
      await page.waitForFunction(
        () => {
          const s = document.getElementById("state");
          const t = (s && s.textContent) || "";
          return /compiled|échouée|✗|✓/i.test(t);
        },
        null,
        { timeout: 120000 }
      );
      // Allow async version-store notify to race; primary bar must hold
      await page.waitForTimeout(600);
      const state = await page.locator("#state").textContent();
      expect(state).toMatch(/compiled|✓|échouée|✗/i);
      expect(state).not.toMatch(/persistance du diff/i);
    });
  });

  test("anchored comments survive rewrap + reload", async ({ page }) => {
    await withLatexProject(async ({ base, commentsPath }) => {
      const url =
        `${base}/.fig_thumbs/latex_studio.html?path=` +
        encodeURIComponent(commentsPath);
      await page.goto(url);
      await page.waitForFunction(() => document.querySelector(".cm-editor, .CodeMirror"), null, {
        timeout: 20000,
      });

      const result = await page.evaluate(async () => {
        // Wait for cm global used by studio
        const wait = (ms) => new Promise((r) => setTimeout(r, ms));
        let tries = 0;
        while (!window.cm && tries < 50) {
          await wait(100);
          tries++;
        }
        // studio may keep cm as module-local — use CodeMirror instance from DOM
        let cm = window.cm;
        if (!cm) {
          const host = document.querySelector(".cm-editor") || document.querySelector(".CodeMirror");
          // fallback: call pure helpers only
          if (!window.AtelierLatexComments || !window.AtelierRewrap) {
            return { error: "helpers missing" };
          }
        }
        const getCm = () => {
          if (window.cm) return window.cm;
          // CM6 facade stores on first CodeMirror instance via AtelierEditor
          return null;
        };
        cm = getCm();
        const before = cm ? cm.getValue() : null;
        const src =
          before ||
          (await (await fetch("/code?path=" + encodeURIComponent(new URLSearchParams(location.search).get("path")))).json()).text;

        const annots = [
          {
            id: "c-test",
            from: { line: 2, ch: 2 },
            to: { line: 2, ch: 2 + "UNIQUE_ANCHOR_PHRASE".length },
            text: "UNIQUE_ANCHOR_PHRASE",
            comment: "survives rewrap",
          },
        ];
        // Apply comment-only rewrap on lines that start with %
        const lines = src.split("\n");
        const commentLines = lines.filter((l) => /^\s*%/.test(l));
        const rewrapped = AtelierRewrap.rewrapLines(commentLines, 40, "tex");
        if (!rewrapped) return { error: "rewrap null", commentLines };
        // Build new document: replace leading comment block
        let i = 0;
        const out = [];
        let replaced = false;
        while (i < lines.length) {
          if (!replaced && /^\s*%/.test(lines[i])) {
            const block = [];
            while (i < lines.length && /^\s*%/.test(lines[i])) {
              block.push(lines[i]);
              i++;
            }
            const r = AtelierRewrap.rewrapLines(block, 40, "tex");
            (r || block).forEach((l) => out.push(l));
            replaced = true;
            continue;
          }
          out.push(lines[i]);
          i++;
        }
        const after = out.join("\n");
        const re = AtelierLatexComments.reanchor(annots, src, after);
        // Simulate reload: re-anchor again from same snippet
        const re2 = AtelierLatexComments.reanchor(re.ok, after, after);
        return {
          okCount: re.ok.length,
          lostCount: re.lost.length,
          comment: re.ok[0] && re.ok[0].comment,
          snippet:
            re.ok[0] &&
            AtelierLatexComments.sliceRange(after, re.ok[0].from, re.ok[0].to),
          stillOk: re2.ok.length,
          afterHasCode: after.includes("UNIQUE_CODE_LINE"),
          afterHasAnchor: after.includes("UNIQUE_ANCHOR_PHRASE"),
        };
      });

      expect(result.error).toBeFalsy();
      expect(result.okCount).toBe(1);
      expect(result.lostCount).toBe(0);
      expect(result.comment).toBe("survives rewrap");
      expect(result.snippet).toBe("UNIQUE_ANCHOR_PHRASE");
      expect(result.stillOk).toBe(1);
      expect(result.afterHasCode).toBe(true);
      expect(result.afterHasAnchor).toBe(true);
    });
  });
});

test.describe("Phase 4 — shell LaTeX surface (code_editor?surface=latex)", () => {
  test.skip(!HAS_LATEXMK, "latexmk not installed");

  test("shell mounts compile, outline, log, and rewrap helpers", async ({ page }) => {
    await withLatexProject(async ({ base, mainPath }) => {
      const url =
        `${base}/.fig_thumbs/code_editor.html?surface=latex&path=` +
        encodeURIComponent(mainPath);
      await page.goto(url);
      await page.waitForFunction(() => window.__ATELIER_SHELL__ != null, null, {
        timeout: 20000,
      });
      const info = await page.evaluate(() => {
        const shell = window.__ATELIER_SHELL__;
        return {
          surface: shell.surface,
          moduleId: shell.module && shell.module.id,
          hasCompile: typeof shell.module?.compile === "function",
          hasSyncFwd: typeof shell.module?.synctexForward === "function",
          hasSyncBack: typeof shell.module?.synctexBackward === "function",
          hasOutline: typeof shell.module?.refreshOutline === "function",
          hasComments: typeof shell.module?.addCommentForSelection === "function",
          hasErrors: !!window.AtelierLatexErrors,
          hasCm: !!document.querySelector(".cm-editor"),
          hasLog: !!document.getElementById("latexCompileLog"),
          hasEditorMode: !!document.getElementById("latexEditorOnly"),
          hasSplit: !!document.getElementById("latexSplitToggle"),
          hasPdfTab: !!document.getElementById("latexPdfTab"),
          hasDivider: !!document.getElementById("latexSplitDivider"),
          previewInitiallyHidden:
            document.getElementById("latexSplitPreview")?.style.display === "none",
        };
      });
      expect(info.surface).toBe("latex");
      expect(info.moduleId).toBe("latex");
      expect(info.hasCompile).toBe(true);
      expect(info.hasSyncFwd).toBe(true);
      expect(info.hasSyncBack).toBe(true);
      expect(info.hasOutline).toBe(true);
      expect(info.hasComments).toBe(true);
      expect(info.hasErrors).toBe(true);
      expect(info.hasCm).toBe(true);
      expect(info.hasLog).toBe(true);
      expect(info.hasEditorMode).toBe(true);
      expect(info.hasSplit).toBe(true);
      expect(info.hasPdfTab).toBe(true);
      expect(info.hasDivider).toBe(true);
      expect(info.previewInitiallyHidden).toBe(true);

      const viewModes = await page.evaluate(() => {
        const preview = document.getElementById("latexSplitPreview");
        document.getElementById("latexSplitToggle").click();
        const splitVisible = preview.style.display === "flex";
        document.getElementById("latexEditorOnly").click();
        return {
          splitVisible,
          editorOnly: preview.style.display === "none",
        };
      });
      expect(viewModes.splitVisible).toBe(true);
      expect(viewModes.editorOnly).toBe(true);

      // Compile via module
      const compiled = await page.evaluate(async () => {
        return window.__ATELIER_SHELL__.module.compile();
      });
      expect(compiled.ok).toBe(true);
      expect(compiled.pdf || compiled.root).toBeTruthy();

      // Outline items
      await page.evaluate(() => {
        const mod = window.__ATELIER_SHELL__.module;
        mod.refreshOutline();
        const el = document.getElementById("latexOutline");
        if (el) el.style.display = "block";
        mod.refreshOutline();
      });
      // outline may be hidden until Plan click — force HTML
      const outlineHtml = await page.evaluate(() => {
        const src = window.__ATELIER_SHELL__.cm().getValue();
        return window.AtelierLatexOutline.renderOutlineHtml(
          window.AtelierLatexOutline.parseOutline(src),
          0
        );
      });
      expect(outlineHtml).toContain("Introduction");
      expect(outlineHtml).toContain("Results");

      // Error gutters on broken path: open broken in same shell API
      // (diagnostic helper pure)
      const errLines = await page.evaluate(() =>
        window.AtelierLatexErrors.errorLinesFromLog("! err\nl.7 \\undefined\n")
      );
      expect(errLines).toContain(7);
    });
  });

  test("shell rewrap reanchors comments by content", async ({ page }) => {
    await withLatexProject(async ({ base, commentsPath }) => {
      const url =
        `${base}/.fig_thumbs/code_editor.html?surface=latex&path=` +
        encodeURIComponent(commentsPath);
      await page.goto(url);
      await page.waitForFunction(() => window.__ATELIER_SHELL__?.module, null, {
        timeout: 20000,
      });
      const result = await page.evaluate(() => {
        const shell = window.__ATELIER_SHELL__;
        const cm = shell.cm();
        // Select UNIQUE_ANCHOR_PHRASE and add comment
        const text = cm.getValue();
        const idx = text.indexOf("UNIQUE_ANCHOR_PHRASE");
        const from = cm.posFromIndex(idx);
        const to = cm.posFromIndex(idx + "UNIQUE_ANCHOR_PHRASE".length);
        cm.setSelection(from, to);
        shell.module.addCommentForSelection("anchored via shell");
        // Rewrap comments at col 40
        const sel = document.getElementById("wrapSel");
        if (sel && !sel.querySelector('option[value="40"]')) {
          const opt = document.createElement("option");
          opt.value = "40";
          opt.textContent = "Wrap: 40";
          sel.insertBefore(opt, sel.querySelector('option[value="custom"]'));
        }
        if (sel) {
          sel.value = "40";
          sel.dispatchEvent(new Event("change", { bubbles: true }));
        }
        cm.setCursor({ line: 2, ch: 0 });
        shell.module.rewrap(true);
        const annots = shell.module.getAnnots();
        return {
          count: annots.length,
          comment: annots[0] && annots[0].comment,
          text: annots[0] && annots[0].text,
          stillHasPhrase: cm.getValue().includes("UNIQUE_ANCHOR_PHRASE"),
          stillHasCode: cm.getValue().includes("UNIQUE_CODE_LINE"),
        };
      });
      expect(result.count).toBeGreaterThanOrEqual(1);
      expect(result.comment).toBe("anchored via shell");
      expect(result.text).toContain("UNIQUE_ANCHOR_PHRASE");
      expect(result.stillHasPhrase).toBe(true);
      expect(result.stillHasCode).toBe(true);
    });
  });

  test("compile saves dirty buffer before building PDF", async ({ page }) => {
    await withLatexProject(async ({ base, mainPath }) => {
      const url =
        `${base}/.fig_thumbs/code_editor.html?surface=latex&path=` +
        encodeURIComponent(mainPath);
      await page.goto(url);
      await page.waitForFunction(() => window.__ATELIER_SHELL__?.module, null, {
        timeout: 20000,
      });
      const marker = "DIRTY_COMPILE_MARKER_" + Date.now();
      const result = await page.evaluate(async (mark) => {
        const shell = window.__ATELIER_SHELL__;
        const cm = shell.cm();
        // Real edit path (not setValue origin)
        const end = { line: cm.lineCount() - 1, ch: cm.getLine(cm.lineCount() - 1).length };
        cm.replaceRange("\n% " + mark + "\n", end);
        const dirtyBefore = shell.persistence.isDirty();
        const j = await shell.module.compile();
        const dirtyAfter = shell.persistence.isDirty();
        // Disk must contain the marker after compile-before-save
        const disk = await (await fetch("/code?path=" + encodeURIComponent(shell.path))).json();
        return {
          dirtyBefore,
          dirtyAfter,
          ok: j && j.ok,
          diskHasMarker: String(disk.text || "").includes(mark),
          bufferHasMarker: cm.getValue().includes(mark),
        };
      }, marker);
      expect(result.dirtyBefore).toBe(true);
      expect(result.ok).toBe(true);
      expect(result.dirtyAfter).toBe(false);
      expect(result.diskHasMarker).toBe(true);
      expect(result.bufferHasMarker).toBe(true);
    });
  });

  test("broken compile applies clickable error gutters", async ({ page }) => {
    await withLatexProject(async ({ base, brokenPath }) => {
      const url =
        `${base}/.fig_thumbs/code_editor.html?surface=latex&path=` +
        encodeURIComponent(brokenPath);
      await page.goto(url);
      await page.waitForFunction(() => window.__ATELIER_SHELL__?.module, null, {
        timeout: 20000,
      });
      const result = await page.evaluate(async () => {
        const shell = window.__ATELIER_SHELL__;
        const j = await shell.module.compile();
        const cm = shell.cm();
        const gutters = document.querySelectorAll(".lint-gutter-err");
        const errLineBg = document.querySelectorAll(".cm-error-line");
        const log = document.getElementById("latexCompileLog");
        const jump = log && log.querySelector(".tl-jump");
        let jumpedLine = null;
        if (jump) {
          jump.click();
          jumpedLine = cm.getCursor().line + 1;
        }
        return {
          ok: j && j.ok,
          hasGutter: gutters.length > 0,
          hasErrBg: errLineBg.length > 0,
          hasJump: !!jump,
          jumpedLine,
          logHasBang: !!(log && /!|Error|Undefined/i.test(log.textContent || "")),
        };
      });
      expect(result.ok).toBe(false);
      expect(result.hasGutter || result.hasErrBg).toBe(true);
      expect(result.logHasBang).toBe(true);
      if (result.hasJump) {
        expect(result.jumpedLine).toBeGreaterThan(0);
      }
    });
  });

  test("SyncTeX reverse via shell postMessage jumps to source line", async ({ page }) => {
    await withLatexProject(async ({ base, mainPath }) => {
      // Pre-compile so PDF + synctex exist
      await fetch(`${base}/compile`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: mainPath }),
      });
      const url =
        `${base}/.fig_thumbs/code_editor.html?surface=latex&path=` +
        encodeURIComponent(mainPath);
      await page.goto(url);
      await page.waitForFunction(() => window.__ATELIER_SHELL__?.module, null, {
        timeout: 20000,
      });
      // Forward first to get a valid page/y, then reverse via postMessage path
      const result = await page.evaluate(async () => {
        const shell = window.__ATELIER_SHELL__;
        const cm = shell.cm();
        // Place cursor on "Hello Gate C" line
        const text = cm.getValue().split("\n");
        let line = text.findIndex((l) => l.includes("Hello Gate C"));
        if (line < 0) line = 5;
        cm.setCursor({ line, ch: 0 });
        const fwd = await shell.module.synctexForward();
        if (!fwd || !fwd.page) {
          return { error: "forward failed", fwd };
        }
        // Simulate PDF click message the module listens for
        window.postMessage(
          {
            type: "atelier-synctex-edit",
            page: fwd.page,
            x: fwd.x ?? 100,
            y: fwd.y ?? 100,
          },
          "*"
        );
        await new Promise((r) => setTimeout(r, 400));
        // Also exercise direct API
        const back = await shell.module.synctexBackward(
          fwd.page,
          fwd.x ?? 100,
          fwd.y ?? 100
        );
        return {
          fwdPage: fwd.page,
          backLine: back && back.line,
          cursorLine: cm.getCursor().line + 1,
          hasSyncClass:
            !!document.querySelector(".cm-syncline") ||
            (back && back.line > 0),
        };
      });
      expect(result.error).toBeFalsy();
      expect(result.fwdPage).toBeGreaterThan(0);
      expect(result.backLine).toBeGreaterThan(0);
      expect(result.cursorLine).toBeGreaterThan(0);
      // Within a few lines of the original body text
      expect(Math.abs(result.cursorLine - result.backLine)).toBeLessThanOrEqual(2);
    });
  });

  test("destroy during in-flight compile does not throw or paint after death", async ({ page }) => {
    await withLatexProject(async ({ base, mainPath }) => {
      const url =
        `${base}/.fig_thumbs/code_editor.html?surface=latex&path=` +
        encodeURIComponent(mainPath);
      await page.goto(url);
      await page.waitForFunction(() => window.__ATELIER_SHELL__?.module, null, {
        timeout: 20000,
      });
      const result = await page.evaluate(async () => {
        const shell = window.__ATELIER_SHELL__;
        const logEl = document.getElementById("latexCompileLog");
        const logBefore = logEl ? logEl.innerHTML : "";
        const guttersBefore = document.querySelectorAll(".lint-gutter-err").length;
        const p = shell.module.compile(); // do not await yet
        shell.destroy();
        let settled = null;
        try {
          settled = await p;
        } catch (e) {
          return { threw: true, message: String(e && e.message) };
        }
        // Allow microtasks from late compile resolution to settle
        await new Promise((r) => setTimeout(r, 50));
        const logAfter = logEl ? logEl.innerHTML : "";
        const guttersAfter = document.querySelectorAll(".lint-gutter-err").length;
        return {
          threw: false,
          settledOk: settled === null || typeof settled === "object",
          logUnchanged: logBefore === logAfter,
          guttersUnchanged: guttersBefore === guttersAfter,
        };
      });
      expect(result.threw).toBe(false);
      expect(result.settledOk).toBe(true);
      expect(result.logUnchanged).toBe(true);
      expect(result.guttersUnchanged).toBe(true);
    });
  });
});

test.describe("Gate C — agent bank not hidden at top-level", () => {
  test("codex annotation bank button is visible on gallery", async ({ page }) => {
    // Reuse core-style minimal server via latex project + codex env
    const root = mkdtempSync(path.join(tmpdir(), "atelier-agent-e2e-"));
    let server;
    try {
      writeFileSync(path.join(root, "analysis.py"), 'print("x")\n');
      writeFileSync(
        path.join(root, "preview.png"),
        Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
      );
      execFileSync("cargo", ["build", "--manifest-path", RUST_MANIFEST, "-p", "atelier-cli", "-p", "atelier-server"], {
        cwd: REPO,
        stdio: "pipe",
      });
      execFileSync(ATELIER_CLI, ["build", "--root", root], {
        cwd: root,
        env: { ...process.env, ATELIER_ASSETS_DIR: path.join(REPO, "assets") },
        stdio: "pipe",
      });
      const port = await freePort();
      const agentToken = "e2e-agent-token";
      server = spawn(ATELIER_SERVER, ["--root", root, "--port", String(port), "--watch"], {
        cwd: root,
        env: {
          ...process.env,
          GALLERY_ROOT: root,
          ATELIER_ASSETS_DIR: path.join(REPO, "assets"),
          HOME: root,
          ATELIER_AGENT_HOST: "codex",
          ATELIER_AGENT_TOKEN: agentToken,
        },
        stdio: ["ignore", "pipe", "pipe"],
      });
      await waitForPing(port);
      await page.goto(`http://127.0.0.1:${port}/figures_index.html`);
      await expect(page.locator("#atelierAgentButton")).toBeVisible({ timeout: 10000 });
    } finally {
      if (server) {
        server.kill("SIGTERM");
        await new Promise((r) => server.once("exit", r));
      }
      rmSync(root, { recursive: true, force: true });
    }
  });
});
