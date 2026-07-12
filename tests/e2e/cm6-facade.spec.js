/**
 * CM6 facade smoke — proves default engine is CM6 and CM5-compat API works.
 */
import { test, expect } from "@playwright/test";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { readFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const assets = join(root, "assets");

function contentType(p) {
  if (p.endsWith(".js")) return "application/javascript; charset=utf-8";
  if (p.endsWith(".css")) return "text/css; charset=utf-8";
  if (p.endsWith(".html")) return "text/html; charset=utf-8";
  return "application/octet-stream";
}

test.describe("CM6 editor factory", () => {
  /** @type {import('node:http').Server} */
  let server;
  let baseURL;

  test.beforeAll(async () => {
    expect(existsSync(join(assets, "cm6", "editor.bundle.js"))).toBeTruthy();
    expect(existsSync(join(assets, "editor_factory.js"))).toBeTruthy();

    server = createServer((req, res) => {
      const url = new URL(req.url || "/", "http://127.0.0.1");
      let rel = url.pathname;
      if (rel.startsWith("/.fig_thumbs/")) rel = rel.slice("/.fig_thumbs/".length);
      else if (rel.startsWith("/")) rel = rel.slice(1);
      if (!rel || rel === "") rel = "code_editor.html";
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
    const { port } = server.address();
    baseURL = `http://127.0.0.1:${port}`;
  });

  test.afterAll(async () => {
    await new Promise((resolve) => server.close(resolve));
  });

  test("factory loads CM6 by default and facade API works", async ({ page }) => {
    await page.goto(baseURL + "/cm6-smoke.html");
    await page.waitForFunction(() => window.__CM6_SMOKE__ && window.__CM6_SMOKE__.done);
    const result = await page.evaluate(() => window.__CM6_SMOKE__);
    expect(result.error).toBeFalsy();
    expect(result.engine).toBe("cm6");
    expect(result.version).toBe("6-facade");
    expect(result.getValue).toBe("hello\nworld");
    expect(result.lineCount).toBe(2);
    expect(result.cursor.line).toBe(1);
    expect(result.markOk).toBe(true);
    expect(result.selection).toBe("world");
    expect(result.scrollApi).toBe(true);
  });

  test("Rust highlighting and code theme reconfiguration work", async ({ page }) => {
    await page.goto(baseURL + "/cm6-smoke.html");
    await page.waitForFunction(() => window.__CM6_SMOKE__ && window.__CM6_SMOKE__.done);
    const result = await page.evaluate(() => {
      const cm = window.__CM6_EDITOR__;
      cm.setOption("mode", "rust");
      cm.setValue('fn main() { let answer: i32 = 42; println!("{answer}"); }');
      cm.setOption("codeTheme", "Dracula");
      const keyword = cm.getWrapperElement().querySelector(".tok-keyword");
      return {
        selected: localStorage.getItem("atelierCodeTheme"),
        background: getComputedStyle(cm.getWrapperElement()).backgroundColor,
        keyword: keyword ? getComputedStyle(keyword).color : null,
        highlighted: AtelierCM6.highlightCode("fn main() { let n = 42; }", "rust"),
      };
    });
    expect(result.selected).toBe("Dracula");
    expect(result.background).toBe("rgb(40, 42, 54)");
    expect(result.keyword).toBe("rgb(255, 121, 198)");
    expect(result.highlighted).toContain("tok-keyword");
  });

  test("gallery exposes separate app and code theme controls", async () => {
    const html = readFileSync(join(assets, "gallery_template.html"), "utf8");
    expect(html).toContain("Thème Atelier");
    expect(html).toContain("Thème du code");
    expect(html).toContain("atelierCodeTheme");
    expect(html).toContain('data-ext="${escA(f.ext)}"');
    expect(html).toContain("function canonicalEditorUrl");
    expect(html).toContain("AtelierLanguages.editorUrl");
    expect(html).toContain("else u=canonicalEditorUrl(f.rel)");
  });

  test("code_editor.html is a thin shell bootstrap (not CM5)", async () => {
    const html = readFileSync(join(assets, "code_editor.html"), "utf8");
    expect(html).toContain("editor_factory.js");
    expect(html).toContain("editor/shell.js");
    expect(html).toContain("AtelierShell");
    expect(html).not.toMatch(/cm\/codemirror\.min\.js/);
    expect(html).toContain('id="rewrapBtn"');
    expect(html).toContain('id="autoRewrap"');
    expect(html).toContain('aria-label="Rewrap"');
    expect(html).toContain('aria-label="Rewrap automatique"');
    // language map lives in languages.js
    const langs = readFileSync(join(assets, "editor", "languages.js"), "utf8");
    expect(langs).toMatch(/rs:\s*\{\s*mode:\s*["']rust["']/);
    // shortcuts live in shell command registry
    const shell = readFileSync(join(assets, "editor", "shell.js"), "utf8");
    expect(shell).toContain("Mod-s");
    expect(shell).toContain("Alt-q");
  });

  test("md_viewer and latex_studio use factory", async () => {
    for (const f of ["md_viewer.html", "latex_studio.html"]) {
      const html = readFileSync(join(assets, f), "utf8");
      expect(html).toContain("editor_factory.js");
      expect(html).not.toMatch(/cm\/codemirror\.min\.js/);
    }
  });
});
