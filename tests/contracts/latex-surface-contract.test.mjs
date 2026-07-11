/**
 * Gate C — LaTeX surface contracts (fixtures, pure helpers, asset wiring).
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, existsSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createRequire } from "node:module";
import vm from "node:vm";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const fixtures = join(root, "tests", "fixtures", "editor", "latex");
const assets = join(root, "assets");

function read(rel) {
  return readFileSync(join(root, rel), "utf8");
}

/** Load a browser IIFE helper into a sandbox and return its global export. */
function loadHelper(rel, exportName) {
  const code = read(rel);
  const sandbox = { window: {}, globalThis: {} };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.runInNewContext(code, sandbox, { filename: rel });
  assert.ok(sandbox[exportName], `missing export ${exportName} from ${rel}`);
  return sandbox[exportName];
}

describe("Gate C — LaTeX fixtures", () => {
  it("ships compile / outline / error / comments fixtures", () => {
    for (const name of [
      "main.tex",
      "broken.tex",
      "comments.tex",
      "root.tex",
      "chapter.tex",
    ]) {
      const p = join(fixtures, name);
      assert.ok(existsSync(p), name);
      assert.ok(readFileSync(p, "utf8").trim().length > 0, name + " empty");
    }
  });

  it("main.tex has sections for outline assertions", () => {
    const src = readFileSync(join(fixtures, "main.tex"), "utf8");
    assert.match(src, /\\section\{Introduction\}/);
    assert.match(src, /\\subsection\{Details\}/);
    assert.match(src, /\\section\{Results\}/);
    assert.match(src, /\\label\{sec:intro\}/);
  });

  it("comments.tex has unique anchor phrase", () => {
    const src = readFileSync(join(fixtures, "comments.tex"), "utf8");
    assert.ok(src.includes("UNIQUE_ANCHOR_PHRASE"));
    assert.ok(src.includes("UNIQUE_CODE_LINE"));
  });
});

describe("Gate C — pure outline helper", () => {
  const Outline = loadHelper(
    "assets/editor/modules/latex/outline.js",
    "AtelierLatexOutline"
  );

  it("parses section hierarchy from main.tex", () => {
    const src = readFileSync(join(fixtures, "main.tex"), "utf8");
    const items = Outline.parseOutline(src);
    assert.equal(items.length, 3);
    assert.equal(items[0].t, "Introduction");
    assert.equal(items[0].lvl, 1);
    assert.equal(items[1].t, "Details");
    assert.equal(items[1].lvl, 2);
    assert.equal(items[2].t, "Results");
    assert.equal(items[2].lvl, 1);
  });

  it("marks active section by cursor line", () => {
    const items = [
      { lvl: 1, t: "A", line: 2 },
      { lvl: 2, t: "B", line: 5 },
      { lvl: 1, t: "C", line: 10 },
    ];
    assert.equal(Outline.activeIndex(items, 0), -1);
    assert.equal(Outline.activeIndex(items, 3), 0);
    assert.equal(Outline.activeIndex(items, 6), 1);
    assert.equal(Outline.activeIndex(items, 12), 2);
  });

  it("renders HTML without XSS on titles", () => {
    const html = Outline.renderOutlineHtml([
      { lvl: 1, t: "A <b>x</b>", line: 0 },
    ]);
    assert.ok(html.includes("&lt;b&gt;"));
    assert.ok(html.includes('data-l="0"'));
  });
});

describe("Gate C — comment reanchor helper", () => {
  const Comments = loadHelper(
    "assets/editor/modules/latex/comments.js",
    "AtelierLatexComments"
  );

  it("re-anchors comments by snippet after rewrap mutation", () => {
    const oldText = [
      "% UNIQUE_ANCHOR_PHRASE this long comment should survive rewrap",
      "% when comments are re-anchored by content rather than only by positions.",
      "\\section{Keep}",
      "Real code line with UNIQUE_CODE_LINE that must not rewrap into comment.",
    ].join("\n");
    // Simulated rewrap: same words, different line breaks
    const newText = [
      "% UNIQUE_ANCHOR_PHRASE this long comment should survive",
      "% rewrap when comments are re-anchored by content rather than only by",
      "% positions.",
      "\\section{Keep}",
      "Real code line with UNIQUE_CODE_LINE that must not rewrap into comment.",
    ].join("\n");
    const annots = [
      {
        id: "c1",
        from: { line: 0, ch: 2 },
        to: { line: 0, ch: 2 + "UNIQUE_ANCHOR_PHRASE".length },
        text: "UNIQUE_ANCHOR_PHRASE",
        comment: "note anchored",
      },
    ];
    const re = Comments.reanchor(annots, oldText, newText);
    assert.equal(re.ok.length, 1);
    assert.equal(re.lost.length, 0);
    assert.equal(re.ok[0].comment, "note anchored");
    const snippet = Comments.sliceRange(newText, re.ok[0].from, re.ok[0].to);
    assert.equal(snippet, "UNIQUE_ANCHOR_PHRASE");
  });

  it("marks lost when snippet disappears", () => {
    const re = Comments.reanchor(
      [{ id: "x", from: { line: 0, ch: 0 }, to: { line: 0, ch: 3 }, text: "ZZZ", comment: "" }],
      "aaa",
      "bbb"
    );
    assert.equal(re.ok.length, 0);
    assert.equal(re.lost.length, 1);
  });
});

describe("Gate C — ghost + log classifiers", () => {
  const Ghost = loadHelper(
    "assets/editor/modules/latex/ghost.js",
    "AtelierLatexGhost"
  );
  const Compile = loadHelper(
    "assets/editor/modules/latex/compile.js",
    "AtelierLatexCompile"
  );

  it("suggests \\end{env} from open begin", () => {
    const doc = "\\begin{itemize}\n\\item x\n";
    const sug = Ghost.suggestion(doc, doc + "\\end{");
    assert.ok(sug.startsWith("itemize"), sug);
  });

  it("classifies error and warning log lines", () => {
    const rows = Compile.classifyLogLines(
      "! Undefined control sequence.\nl.4 \\undefinedcommand\nLaTeX Warning: something\nOK line\n"
    );
    assert.ok(rows.some((r) => r.cls === "tl-err"));
    assert.ok(rows.some((r) => r.cls === "tl-warn"));
    assert.ok(rows.some((r) => r.jumps.includes(4)));
  });
});

describe("Gate C — studio wires shared helpers (no deletion of surface)", () => {
  it("latex_studio loads shared module scripts", () => {
    const html = read("assets/latex_studio.html");
    for (const f of [
      "editor/modules/latex/outline.js",
      "editor/modules/latex/compile.js",
      "editor/modules/latex/synctex.js",
      "editor/modules/latex/comments.js",
      "editor/modules/latex/ghost.js",
      "editor/rewrap.js",
    ]) {
      assert.ok(html.includes(f), "missing script " + f);
    }
    // Primary surface still present
    assert.ok(html.includes("function compile"));
    assert.ok(html.includes("function synctexView"));
    assert.ok(html.includes("function synctexEdit"));
    assert.ok(html.includes("function buildOutline"));
    assert.ok(html.includes("function rewrapPar"));
  });

  it("shared helper files exist under assets/editor/modules/latex", () => {
    const dir = join(assets, "editor", "modules", "latex");
    for (const f of ["outline.js", "compile.js", "synctex.js", "comments.js", "ghost.js"]) {
      assert.ok(existsSync(join(dir, f)), f);
    }
  });
});
