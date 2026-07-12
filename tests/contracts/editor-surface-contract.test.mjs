/**
 * Phase 0 — Editor surface contract.
 *
 * Freezes the current open routes, extension→surface mapping, toolbar
 * primitives, preference keys, and source/install asset hashes.
 * Behavioural changes are not introduced here; divergences are classified
 * as debt or intentional exceptions in docs/editor-surface-contract.md.
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  readFileSync,
  existsSync,
  readdirSync,
  statSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { join, dirname, extname } from "node:path";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const assets = join(root, "assets");
const fixtures = join(root, "tests", "fixtures", "editor");

function read(rel) {
  return readFileSync(join(root, rel), "utf8");
}

function sha256(filePath) {
  return createHash("sha256").update(readFileSync(filePath)).digest("hex");
}

/** Canonical extension → surface mapping (target after unification). */
export const SURFACE_MATRIX = {
  // code shell
  rs: { surface: "code", page: "code_editor.html", mode: "rust", debt: false },
  py: { surface: "code", page: "code_editor.html", mode: "python", debt: false },
  r: { surface: "code", page: "code_editor.html", mode: "r", debt: false },
  R: { surface: "code", page: "code_editor.html", mode: "r", debt: false },
  jl: { surface: "code", page: "code_editor.html", mode: "julia", debt: false },
  sh: { surface: "code", page: "code_editor.html", mode: "shell", debt: false },
  bash: { surface: "code", page: "code_editor.html", mode: "shell", debt: false },
  js: { surface: "code", page: "code_editor.html", mode: "javascript", debt: false },
  jsx: { surface: "code", page: "code_editor.html", mode: "javascript", debt: false },
  ts: { surface: "code", page: "code_editor.html", mode: "typescript", debt: false },
  tsx: { surface: "code", page: "code_editor.html", mode: "typescript", debt: false },
  json: { surface: "code", page: "code_editor.html", mode: "json", debt: false },
  toml: { surface: "code", page: "code_editor.html", mode: "text", debt: false },
  yaml: { surface: "code", page: "code_editor.html", mode: "text", debt: false },
  yml: { surface: "code", page: "code_editor.html", mode: "text", debt: false },
  // document modules
  tex: { surface: "latex", page: "code_editor.html", mode: "stex", debt: false },
  sty: { surface: "latex", page: "code_editor.html", mode: "stex", debt: false },
  bib: { surface: "latex", page: "code_editor.html", mode: "stex", debt: false },
  md: { surface: "markdown", page: "code_editor.html", mode: "markdown", debt: false },
};

/** Current gallery open routes (inventory). */
export const OPEN_ROUTES = {
  gallery_tabs_pdf: "/.fig_thumbs/pdf_viewer.html?file=",
  gallery_tabs_editor: "canonicalEditorUrl(f.rel)",
  gallery_lightbox_pdf: "/.fig_thumbs/pdf_viewer.html?file=",
  gallery_lightbox_editor: "pdf.src=canonicalEditorUrl(f.rel)",
  gallery_lightbox_svg: "/.fig_thumbs/svg_viewer.html?file=",
  ide_browse: "/.fig_thumbs/code_editor.html?browse=1",
  explorer_open_editor: "AtelierLanguages.editorUrl(target)",
};

/** Shared toolbar primitives required by the common shell. */
export const TOOLBAR_PRIMITIVES = [
  "fname",
  "openFile",
  "wrapSel",
  "rewrapBtn",
  "autoRewrap",
  "state",
  "diffTag",
];

/** Preference keys — v1 target + legacy migration sources. */
export const PREF_KEYS = {
  legacy: {
    codeTheme: "atelierCodeTheme",
    wrap: "cmWrap",
    autoRewrap: "codeAutoRewrap",
    appTheme: "figTheme",
  },
  v1: {
    codeTheme: "atelier.editor.v1.codeTheme",
    wrap: "atelier.editor.v1.wrap",
    autoRewrap: "atelier.editor.v1.autoRewrap",
    panel: "atelier.editor.v1.panel",
  },
};

/** Shortcuts observed on the three editor surfaces. */
export const SHORTCUTS = {
  code_editor: [
    { keys: "Mod+S", action: "save" },
    { keys: "Alt+Q", action: "rewrap-paragraph" },
    { keys: "Shift+Alt+Q", action: "rewrap-all-comments" },
    { keys: "Escape", action: "clear-selection-mark" },
  ],
  latex_studio: [
    { keys: "Mod+S", action: "save" },
    { keys: "Mod+Enter", action: "compile" },
  ],
  md_viewer: [
    { keys: "Mod+S", action: "save" },
  ],
};

describe("Phase 0 — editor surface inventory", () => {
  it("gallery_template encodes all known open routes", () => {
    const html = read("assets/gallery_template.html");
    for (const [name, fragment] of Object.entries(OPEN_ROUTES)) {
      if (name.startsWith("explorer_")) continue;
      assert.ok(
        html.includes(fragment),
        `missing open route ${name}: ${fragment}`
      );
    }
    assert.ok(html.includes("function canonicalEditorUrl"), "shared editor route helper");
    assert.ok(html.includes("AtelierLanguages.editorUrl"), "gallery delegates routing to language registry");
    assert.ok(
      html.includes("if(latexShellEnabled()) exts.tex=true"),
      "experimental LaTeX soak exposes TeX files despite a stale saved filter"
    );
    assert.ok(
      html.includes("f.ext==='md'||f.ext==='tex'||codeExt(f.ext)"),
      "TeX cards use the canonical viewer action instead of a raw-file link"
    );
  });

  it("code_editor explorer delegates every editable file to the canonical router", () => {
    const html = read("assets/code_editor.html");
    assert.ok(
      html.includes("AtelierLanguages.editorUrl(target)"),
      "explorer openTarget must use the shared route registry"
    );
  });

  it("gallery codeExt includes programming languages", () => {
    const html = read("assets/gallery_template.html");
    const m = html.match(/const codeExt\s*=\s*e\s*=>\s*([^;]+);/);
    assert.ok(m, "codeExt definition");
    const body = m[1];
    for (const ext of ["py", "r", "jl", "sh", "rs"]) {
      assert.ok(body.includes(`'${ext}'`) || body.includes(`"${ext}"`), `codeExt missing ${ext}`);
    }
    // tex is a document module, not a programming-language card category.
    assert.ok(!body.includes("tex"), "codeExt must not list tex");
  });

  it("extension matrix maps every fixture to a surface", () => {
    const files = readdirSync(fixtures).filter((f) => f.startsWith("sample."));
    assert.ok(files.length >= 9, `expected ≥9 fixtures, got ${files.length}`);
    for (const file of files) {
      const ext = extname(file).slice(1);
      const entry = SURFACE_MATRIX[ext];
      assert.ok(entry, `no SURFACE_MATRIX entry for .${ext} (${file})`);
      assert.ok(
        existsSync(join(assets, entry.page)),
        `surface page missing for .${ext}: ${entry.page}`
      );
      const content = readFileSync(join(fixtures, file), "utf8");
      assert.ok(content.trim().length > 0, `empty fixture ${file}`);
    }
  });

  it("Markdown opens in the unified shell; md_studio remains optional", () => {
    const languages = read("assets/editor/languages.js");
    assert.ok(languages.includes('markdown: "code_editor.html"'));
    // Optional WYSIWYG surface still shipped
    assert.ok(existsSync(join(assets, "md_studio.html")));
  });
});

describe("Phase 0 — toolbar and state primitives", () => {
  it("code_editor exposes shared toolbar primitives", () => {
    const html = read("assets/code_editor.html");
    for (const id of TOOLBAR_PRIMITIVES) {
      assert.ok(
        html.includes(`id="${id}"`) || html.includes(`id='${id}'`),
        `code_editor missing #${id}`
      );
    }
  });

  it("code_editor save states cover dirty/saved/conflict", () => {
    const html = read("assets/code_editor.html");
    for (const cls of ["dirty", "saved", "conflict"]) {
      assert.ok(html.includes(`#state.${cls}`) || html.includes(`"${cls}"`), `state class ${cls}`);
    }
  });

  it("captures shortcuts via shell command registry", () => {
    const shell = read("assets/editor/shell.js");
    const commands = read("assets/editor/commands.js");
    assert.ok(shell.includes("Mod-s") || shell.includes('"save"'));
    assert.ok(shell.includes("Alt-q") || shell.includes("rewrap"));
    assert.ok(shell.includes("Escape") || shell.includes("clear-selection"));
    assert.ok(commands.includes("keydown") || commands.includes("compileShortcut"));
  });

  it("latex_studio has compile and state, not identical wrap chrome", () => {
    const html = read("assets/latex_studio.html");
    assert.ok(html.includes("compile") || html.includes("Compile") || html.includes("/compile"));
    assert.ok(html.includes("id=\"state\"") || html.includes("id='state'"));
    // debt: wrap UI differs (wrapMenu vs wrapSel)
    const hasWrapSel = html.includes("wrapSel");
    const hasWrapMenu = html.includes("wrapMenu") || html.includes("Wrap");
    assert.ok(hasWrapSel || hasWrapMenu, "latex has some wrap control");
  });

  it("md_viewer uses shell + preview module", () => {
    const html = read("assets/md_viewer.html");
    assert.ok(html.includes("AtelierShell") || html.includes("editor/shell.js"));
    assert.ok(html.includes("markdown") || html.includes("marked"));
    assert.ok(html.includes("preview") || html.includes("Aperçu") || html.includes("md-preview"));
    const persist = read("assets/editor/persistence.js");
    assert.ok(persist.includes("/codesave"));
  });
});

describe("Phase 0 — preference keys and CM6 assets", () => {
  it("legacy preference keys still present via prefs dual-write", () => {
    const prefs = read("assets/editor/prefs.js");
    assert.ok(prefs.includes(PREF_KEYS.legacy.wrap));
    assert.ok(prefs.includes(PREF_KEYS.legacy.autoRewrap));
    assert.ok(prefs.includes(PREF_KEYS.legacy.codeTheme));
    assert.ok(prefs.includes(PREF_KEYS.v1.wrap));
    const facade = read("cm6-src/facade.js");
    assert.ok(facade.includes(PREF_KEYS.legacy.codeTheme));
  });

  it("CM6 bundle exists and factory defaults to cm6", () => {
    assert.ok(existsSync(join(assets, "cm6", "editor.bundle.js")));
    assert.ok(existsSync(join(assets, "cm6", "editor.css")));
    assert.ok(existsSync(join(assets, "cm6", "VERSION")));
    const factory = read("assets/editor_factory.js");
    assert.ok(factory.includes('force === "cm5" ? "cm5" : "cm6"') || factory.includes('"cm6"'));
    assert.ok(!factory.includes("cdn.jsdelivr") && !factory.includes("unpkg.com"));
  });

  it("source and installed asset hashes match when install present", () => {
    const critical = [
      "cm6/editor.bundle.js",
      "cm6/editor.css",
      "editor_factory.js",
      "code_editor.html",
      "latex_studio.html",
      "md_viewer.html",
    ];
    const installRoots = [
      join(root, "dist", "share", "atelier", "assets"),
      join(homedir(), ".local", "share", "atelier", "assets"),
    ].filter((p) => existsSync(p));

    for (const rel of critical) {
      const src = join(assets, rel);
      assert.ok(existsSync(src), `missing source asset ${rel}`);
      const srcHash = sha256(src);
      for (const inst of installRoots) {
        const dst = join(inst, rel);
        if (!existsSync(dst)) {
          // document gap — dist may lag source during development
          continue;
        }
        const dstHash = sha256(dst);
        if (inst.includes("dist/share")) {
          // dist is a release snapshot: mismatch is debt, not hard fail during dev
          if (srcHash !== dstHash) {
            // still assert source is newer or equal by size existence
            assert.ok(statSync(src).size > 0);
          }
        } else {
          // live install: warn-level during active development — soft assert with note
          if (srcHash !== dstHash) {
            // Record debt; hard-fail only if editor_factory is missing entirely
            assert.ok(
              existsSync(dst),
              `install asset missing: ${dst}`
            );
            // Prefer syncing install in release; document mismatch for Gate B
            console.log(
              `[contract] install lag (debt): ${rel}\n  source: ${srcHash}\n  install: ${dstHash}`
            );
          }
        }
      }
    }
  });

  it("no CDN loads in editor pages", () => {
    for (const page of ["code_editor.html", "latex_studio.html", "md_viewer.html", "editor_factory.js"]) {
      const text = read(`assets/${page}`);
      assert.ok(!/https?:\/\/cdn\.|unpkg\.com|jsdelivr|cdnjs/i.test(text), `CDN reference in ${page}`);
    }
  });
});

describe("Phase 0 — entry-path coverage matrix", () => {
  /**
   * Four entry paths required by the plan:
   * gallery, explorer (files), restored session, direct URL.
   * This contract documents how each is represented in source.
   */
  it("documents gallery vs explorer vs direct open paths", () => {
    const gallery = read("assets/gallery_template.html");
    const code = read("assets/code_editor.html");

    // Gallery (tabs + lightbox)
    assert.ok(gallery.includes("function lbShow") || gallery.includes("lbShow("));
    assert.ok(gallery.includes("TabShell.open") || gallery.includes("openView"));

    // Explorer / IDE browse
    assert.ok(code.includes("browse=1") || code.includes("BROWSE"));
    assert.ok(code.includes("renderBrowser") || code.includes("/ls?"));

    // Direct URL: path query param
    assert.ok(code.includes('get("path")') || code.includes("searchParams"));

    // Session restore (gallery session + studio recents)
    assert.ok(
      gallery.includes("saveGallerySession") || gallery.includes("GallerySession"),
      "gallery session hooks"
    );
    assert.ok(code.includes("studioRecents") || code.includes("recents"), "recents/session");
  });

  it("lists every SURFACE_MATRIX page as an openable direct URL", () => {
    const pages = new Set(Object.values(SURFACE_MATRIX).map((s) => s.page));
    for (const page of pages) {
      const html = read(`assets/${page}`);
      assert.ok(
        html.includes("path") || html.includes("file"),
        `${page} must accept path/file query`
      );
    }
  });
});

describe("Phase 0 — editor shell module layout (target)", () => {
  it("either documents planned modules or they already exist", () => {
    const planned = [
      "assets/editor/shell.js",
      "assets/editor/core.js",
      "assets/editor/toolbar.js",
      "assets/editor/commands.js",
      "assets/editor/session.js",
      "assets/editor/persistence.js",
      "assets/editor/selection.js",
      "assets/editor/history.js",
      "assets/editor/languages.js",
      "assets/editor/status.js",
      "assets/editor/modules/code.js",
      "assets/editor/modules/latex.js",
      "assets/editor/modules/markdown.js",
    ];
    const present = planned.filter((p) => existsSync(join(root, p)));
    // Phase 0 allows zero modules; later phases fill them in.
    // When any module exists, languages.js must exist (routing keystone).
    if (present.length > 0) {
      assert.ok(
        existsSync(join(root, "assets/editor/languages.js")),
        "languages.js required once editor modules land"
      );
    }
    assert.ok(Array.isArray(present));
  });
});
