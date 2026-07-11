#!/usr/bin/env node
/**
 * Bundle CM6 + CM5-compatible facade → assets/cm6/
 * Run: npm run build  (from cm6-src/)
 */
import * as esbuild from "esbuild";
import { mkdirSync, writeFileSync, copyFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const outDir = join(__dirname, "..", "assets", "cm6");
mkdirSync(outDir, { recursive: true });

// Entry that installs globals for script-tag usage
const entry = join(__dirname, "entry.js");
writeFileSync(
  entry,
  `import { installGlobals, createEditor, countColumn, highlightCode, CODE_THEMES } from "./facade.js";
installGlobals();
export { createEditor, countColumn, highlightCode, CODE_THEMES, installGlobals };
`
);

await esbuild.build({
  entryPoints: [entry],
  bundle: true,
  minify: true,
  format: "iife",
  globalName: "AtelierCM6",
  outfile: join(outDir, "editor.bundle.js"),
  target: ["es2020"],
  legalComments: "none",
  logLevel: "info",
});

// Minimal CSS bridge so existing .CodeMirror rules still apply to the host
const css = `/* CodeMirror 6 host — material-darker bridge for Atelier */
.cm-editor {
  height: 100%;
  font-size: 13px;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
}
.cm-editor.cm-focused { outline: none; }
.cm-scroller {
  font-family: inherit;
  line-height: 1.55;
  overflow: auto !important;
}
.cm-gutters {
  border: none;
}
.cm-clsel { background: rgba(91,157,255,.28); border-radius: 2px; }
.dAddM { background: rgba(76,175,80,.28); }
.cm-line-flash { background: rgba(91,157,255,.22) !important; }
/* Named gutters (diff versions etc.) */
.cm-gutter.dv-git { width: 6px; min-width: 6px; }
`;

writeFileSync(join(outDir, "editor.css"), css);

// Version stamp for cache-busting / diagnostics
writeFileSync(
  join(outDir, "VERSION"),
  `cm6-facade\nbuilt=${new Date().toISOString()}\n`
);

console.log("Wrote", outDir);
