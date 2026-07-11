/**
 * CM5-compatible facade over CodeMirror 6 for Atelier editors.
 * Exposes createEditor(parent, options) → facade with getValue/setValue/markText/…
 */
import {
  EditorView,
  keymap,
  highlightSpecialChars,
  drawSelection,
  dropCursor,
  rectangularSelection,
  crosshairCursor,
  lineNumbers,
  highlightActiveLineGutter,
  highlightActiveLine,
  gutter,
  GutterMarker,
  Decoration,
  WidgetType,
} from "@codemirror/view";
import {
  EditorState,
  Compartment,
  EditorSelection,
  StateEffect,
  StateField,
  RangeSetBuilder,
  Annotation,
} from "@codemirror/state";
import {
  HighlightStyle,
  syntaxHighlighting,
  indentOnInput,
  bracketMatching,
  foldGutter,
  foldKeymap,
  StreamLanguage,
  LanguageSupport,
} from "@codemirror/language";
import { highlightTree, tags } from "@lezer/highlight";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { searchKeymap, highlightSelectionMatches } from "@codemirror/search";
import { autocompletion, closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { python } from "@codemirror/lang-python";
import { markdown } from "@codemirror/lang-markdown";
import { javascript } from "@codemirror/lang-javascript";
import { json } from "@codemirror/lang-json";
import { rust } from "@codemirror/lang-rust";
import { stex } from "@codemirror/legacy-modes/mode/stex";
import { r } from "@codemirror/legacy-modes/mode/r";
import { shell } from "@codemirror/legacy-modes/mode/shell";
import { julia } from "@codemirror/legacy-modes/mode/julia";

const SetValue = Annotation.define();
const GhostEdit = Annotation.define();
const CODE_THEME_KEY = "atelierCodeTheme";

export const CODE_THEMES = {
  "Atelier Dark": {
    bg: "#1e2126", fg: "#d8d3c8", gutter: "#565e6b", accent: "#5b9dff",
    selection: "rgba(91,157,255,.38)", active: "rgba(255,255,255,.035)",
    keyword: "#e07a5f", function: "#61afef", string: "#86c991", number: "#d19a66",
    comment: "#707985", type: "#c678dd", operator: "#abb2bf", property: "#e6c07b",
  },
  "GitHub Dark": {
    bg: "#0d1117", fg: "#c9d1d9", gutter: "#6e7681", accent: "#58a6ff",
    selection: "rgba(56,139,253,.34)", active: "rgba(110,118,129,.10)",
    keyword: "#ff7b72", function: "#d2a8ff", string: "#a5d6ff", number: "#79c0ff",
    comment: "#8b949e", type: "#ffa657", operator: "#ff7b72", property: "#7ee787",
  },
  "Tokyo Night": {
    bg: "#1a1b26", fg: "#c0caf5", gutter: "#565f89", accent: "#7aa2f7",
    selection: "rgba(122,162,247,.28)", active: "rgba(122,162,247,.07)",
    keyword: "#bb9af7", function: "#7aa2f7", string: "#9ece6a", number: "#ff9e64",
    comment: "#565f89", type: "#2ac3de", operator: "#89ddff", property: "#73daca",
  },
  Dracula: {
    bg: "#282a36", fg: "#f8f8f2", gutter: "#6272a4", accent: "#bd93f9",
    selection: "rgba(189,147,249,.30)", active: "rgba(255,255,255,.045)",
    keyword: "#ff79c6", function: "#50fa7b", string: "#f1fa8c", number: "#bd93f9",
    comment: "#6272a4", type: "#8be9fd", operator: "#ff79c6", property: "#66d9ef",
  },
};

function normalizeCodeTheme(name) {
  return Object.prototype.hasOwnProperty.call(CODE_THEMES, name) ? name : "Atelier Dark";
}

function savedCodeTheme() {
  try {
    return normalizeCodeTheme(localStorage.getItem(CODE_THEME_KEY));
  } catch (_) {
    return "Atelier Dark";
  }
}

const classHighlightStyle = HighlightStyle.define([
  { tag: [tags.keyword, tags.controlKeyword, tags.operatorKeyword], class: "tok-keyword" },
  { tag: tags.function(tags.variableName), class: "tok-function" },
  { tag: [tags.typeName, tags.className, tags.namespace], class: "tok-type" },
  { tag: [tags.propertyName, tags.attributeName, tags.labelName], class: "tok-property" },
  { tag: [tags.string, tags.special(tags.string), tags.regexp], class: "tok-string" },
  { tag: [tags.number, tags.bool, tags.null], class: "tok-number" },
  { tag: [tags.comment, tags.lineComment, tags.blockComment, tags.docComment], class: "tok-comment" },
  { tag: [tags.operator, tags.punctuation, tags.bracket], class: "tok-operator" },
  { tag: tags.invalid, class: "tok-invalid" },
]);

function codeThemeExtension(name) {
  const p = CODE_THEMES[normalizeCodeTheme(name)];
  return EditorView.theme(
    {
      "&": { backgroundColor: p.bg, color: p.fg },
      ".cm-content": { caretColor: p.accent },
      ".cm-cursor, .cm-dropCursor": { borderLeftColor: p.accent },
      "&.cm-focused .cm-selectionBackground, .cm-selectionBackground": { backgroundColor: p.selection },
      ".cm-activeLine": { backgroundColor: p.active },
      ".cm-activeLineGutter": { color: p.fg },
      ".cm-gutters": { backgroundColor: p.bg, color: p.gutter },
      ".cm-matchingBracket": { color: `${p.accent} !important`, outlineColor: p.accent },
      ".tok-keyword": { color: p.keyword },
      ".tok-function": { color: p.function },
      ".tok-string": { color: p.string },
      ".tok-number": { color: p.number },
      ".tok-comment": { color: p.comment, fontStyle: "italic" },
      ".tok-type": { color: p.type },
      ".tok-operator": { color: p.operator },
      ".tok-property": { color: p.property },
      ".tok-invalid": { color: "#ff6b6b", textDecoration: "underline wavy" },
    },
    { dark: true }
  );
}

function languageForMode(mode) {
  if (!mode) return null;
  const name = String(mode).toLowerCase();
  if (name === "python" || name.includes("python")) return python().language;
  if (name === "markdown" || name === "gfm") return markdown().language;
  if (name === "javascript" || name === "jsx") return javascript().language;
  if (name === "typescript" || name === "tsx" || name === "ts")
    return javascript({ typescript: true }).language;
  if (name === "json") return json().language;
  if (name === "rust" || name === "rs") return rust().language;
  if (name === "stex" || name === "latex" || name === "tex" || name.includes("stex"))
    return StreamLanguage.define(stex);
  if (name === "r" || name.includes("rsrc")) return StreamLanguage.define(r);
  if (name === "shell" || name === "bash" || name === "sh") return StreamLanguage.define(shell);
  if (name === "julia") return StreamLanguage.define(julia);
  return null;
}

function languageExtension(mode) {
  const language = languageForMode(mode);
  return language ? [new LanguageSupport(language)] : [];
}

const setMarks = StateEffect.define();
const marksField = StateField.define({
  create: () => Decoration.none,
  update(value, tr) {
    value = value.map(tr.changes);
    for (const e of tr.effects) if (e.is(setMarks)) value = e.value;
    return value;
  },
  provide: (f) => EditorView.decorations.from(f),
});

// Line-level decorations (background/wrap classes like CM5 addLineClass)
const setLineMarks = StateEffect.define();
const lineMarksField = StateField.define({
  create: () => Decoration.none,
  update(value, tr) {
    value = value.map(tr.changes);
    for (const e of tr.effects) if (e.is(setLineMarks)) value = e.value;
    return value;
  },
  provide: (f) => EditorView.decorations.from(f),
});

class DomWidget extends WidgetType {
  constructor(dom) {
    super();
    this._dom = dom;
  }
  eq(other) {
    return other._dom === this._dom;
  }
  toDOM() {
    return this._dom;
  }
  ignoreEvent() {
    return true;
  }
}

const setGutterMap = StateEffect.define(); // { name, map: Map }

function createNamedGutter(name, onClick) {
  const field = StateField.define({
    create: () => new Map(),
    update(map, tr) {
      for (const e of tr.effects) {
        if (e.is(setGutterMap) && e.value.name === name) return e.value.map;
      }
      return map;
    },
  });
  class Marker extends GutterMarker {
    constructor(dom) {
      super();
      this.dom = dom;
    }
    eq(o) {
      return o.dom === this.dom;
    }
    toDOM() {
      return this.dom;
    }
  }
  const g = gutter({
    class: name,
    markers(view) {
      const map = view.state.field(field);
      const b = new RangeSetBuilder();
      [...map.entries()]
        .sort((a, b) => a[0] - b[0])
        .forEach(([line, dom]) => {
          if (!dom || line < 0 || line >= view.state.doc.lines) return;
          const pos = view.state.doc.line(line + 1).from;
          b.add(pos, pos, new Marker(dom));
        });
      return b.finish();
    },
    domEventHandlers: {
      click(view, line) {
        if (!onClick) return false;
        onClick(view.state.doc.lineAt(line.from).number - 1);
        return true;
      },
    },
  });
  return { field, gutter: g };
}

const baseTheme = EditorView.theme(
  {
    "&": {
      height: "100%",
      fontSize: "13px",
      fontFamily: "ui-monospace, SF Mono, Menlo, monospace",
    },
    ".cm-content": { padding: "10px 0", minHeight: "100%" },
    ".cm-cursor, .cm-dropCursor": { borderLeftWidth: "2px" },
    ".cm-activeLineGutter": { backgroundColor: "transparent" },
    ".cm-gutters": {
      border: "none",
    },
    ".cm-lineNumbers .cm-gutterElement": { padding: "0 14px 0 10px", minWidth: "2.6em" },
    ".cm-matchingBracket": {
      fontWeight: "600",
      backgroundColor: "transparent",
      outlineWidth: "1px",
      outlineStyle: "solid",
    },
    ".cm-clsel": { backgroundColor: "rgba(91,157,255,.28)", borderRadius: "2px" },
    ".dAddM": { backgroundColor: "rgba(76,175,80,.28)" },
    ".cm-activeline": { backgroundColor: "rgba(255,255,255,.035)" },
    ".cm-line-flash": { backgroundColor: "rgba(91,157,255,.22)" },
    ".cm-scroller": { overflow: "auto", fontFamily: "inherit", lineHeight: "1.55" },
  },
  { dark: true }
);

export function countColumn(string, end, tabSize) {
  if (end == null) {
    const m = string.search(/[^\s\u00a0]/);
    end = m === -1 ? string.length : m;
  }
  let n = 0;
  for (let i = 0; i < end; i++) {
    n += string.charCodeAt(i) === 9 ? tabSize - (n % tabSize) : 1;
  }
  return n;
}

export function createEditor(parent, options = {}) {
  const langComp = new Compartment();
  const wrapComp = new Compartment();
  const roComp = new Compartment();
  const extraGutterComp = new Compartment();
  const keymapComp = new Compartment();
  const codeThemeComp = new Compartment();

  const listeners = {
    change: [],
    cursorActivity: [],
    focus: [],
    blur: [],
    inputRead: [],
    gutterClick: [],
    renderLine: [],
  };

  /** @type {Map<number, any>} */
  const markStore = new Map();
  let markId = 1;
  /** @type {Map<string, {line:number, where:string, cls:string}>} */
  const lineClassStore = new Map();
  /** @type {Map<string, Map<number, HTMLElement>>} */
  const gutterData = new Map();
  /** @type {Map<string, ReturnType<typeof createNamedGutter>>} */
  const gutterReg = new Map();
  /** @type {Array<Record<string, Function>>} */
  const keyMaps = [];
  let lineWrapping = !!options.lineWrapping;
  let tabSize = options.tabSize || 4;
  let modeName = options.mode || null;

  const extraGutterNames = (options.gutters || []).filter(
    (g) => g !== "CodeMirror-linenumbers" && g !== "CodeMirror-foldgutter"
  );

  function ensureGutter(name) {
    if (gutterReg.has(name)) return gutterReg.get(name);
    const made = createNamedGutter(name, (line) => {
      for (const fn of listeners.gutterClick) fn(facade, line, name);
    });
    gutterReg.set(name, made);
    gutterData.set(name, new Map());
    return made;
  }

  for (const n of extraGutterNames) ensureGutter(n);

  function gutterExtensions() {
    const exts = [];
    for (const made of gutterReg.values()) {
      exts.push(made.field, made.gutter);
    }
    return exts;
  }

  function pushMarks() {
    const items = [...markStore.values()]
      .filter((m) => m.from != null && !m._lineClass)
      .sort((a, b) => a.from - b.from || (a.to || 0) - (b.to || 0));
    const b = new RangeSetBuilder();
    for (const m of items) {
      try {
        if (m.widget) {
          b.add(
            m.from,
            m.from,
            Decoration.widget({
              widget: new DomWidget(m.widget),
              side: m.insertLeft ? -1 : 1,
            })
          );
        } else if (m.className && m.to > m.from) {
          b.add(m.from, m.to, Decoration.mark({ class: m.className }));
        } else if (m.className && m.to === m.from) {
          // zero-width mark: still useful for empty selections
          b.add(m.from, m.to, Decoration.mark({ class: m.className }));
        }
      } catch (_) {}
    }
    const lineB = new RangeSetBuilder();
    const lineItems = [...lineClassStore.values()].sort((a, b) => a.line - b.line);
    for (const lc of lineItems) {
      try {
        if (lc.line < 0 || lc.line >= view.state.doc.lines) continue;
        const row = view.state.doc.line(lc.line + 1);
        lineB.add(row.from, row.from, Decoration.line({ class: lc.cls }));
      } catch (_) {}
    }
    view.dispatch({
      effects: [setMarks.of(b.finish()), setLineMarks.of(lineB.finish())],
    });
  }

  function rebuildKeymap() {
    const bindings = [];
    for (const map of keyMaps) {
      for (const [key, handler] of Object.entries(map)) {
        bindings.push({
          key: normalizeCm5Key(key),
          run: () => {
            const r = handler(facade);
            // CM5: return CodeMirror.Pass to fall through; null/undefined = handled
            if (r === window.CodeMirror?.Pass || r === facade.constructor?.Pass) return false;
            return r !== false;
          },
        });
      }
    }
    view.dispatch({ effects: keymapComp.reconfigure(keymap.of(bindings)) });
  }

  function normalizeCm5Key(key) {
    // CM5 "Tab"/"Esc" → CM6 "Tab"/"Escape"; "Ctrl-F" stays similar
    if (key === "Esc") return "Escape";
    return key.replace(/Ctrl-/g, "Ctrl-").replace(/Cmd-/g, "Mod-").replace(/Alt-/g, "Alt-");
  }

  function toPos(line, ch) {
    const doc = view.state.doc;
    const ln = Math.max(0, Math.min(doc.lines - 1, line | 0));
    const row = doc.line(ln + 1);
    return row.from + Math.max(0, Math.min(row.length, ch | 0));
  }

  function fromPos(pos) {
    const line = view.state.doc.lineAt(pos);
    return { line: line.number - 1, ch: pos - line.from };
  }

  const baseExt = [
    options.lineNumbers === false ? [] : lineNumbers(),
    highlightActiveLineGutter(),
    highlightSpecialChars(),
    history(),
    drawSelection(),
    dropCursor(),
    EditorState.allowMultipleSelections.of(true),
    indentOnInput(),
    syntaxHighlighting(classHighlightStyle, { fallback: true }),
    bracketMatching(),
    closeBrackets(),
    autocompletion(),
    rectangularSelection(),
    crosshairCursor(),
    highlightActiveLine(),
    highlightSelectionMatches({ minChars: 3 }),
    options.foldGutter ? foldGutter() : [],
    keymap.of([
      ...closeBracketsKeymap,
      ...defaultKeymap,
      ...searchKeymap,
      ...historyKeymap,
      ...foldKeymap,
      indentWithTab,
    ]),
    marksField,
    lineMarksField,
    langComp.of(languageExtension(options.mode)),
    wrapComp.of(lineWrapping ? EditorView.lineWrapping : []),
    roComp.of(EditorState.readOnly.of(!!options.readOnly)),
    extraGutterComp.of(gutterExtensions()),
    keymapComp.of([]),
    baseTheme,
    codeThemeComp.of(codeThemeExtension(savedCodeTheme())),
    EditorState.tabSize.of(tabSize),
    EditorView.updateListener.of((update) => {
      if (update.docChanged) {
        let origin = "+input";
        for (const tr of update.transactions) {
          if (tr.annotation(SetValue)) origin = "setValue";
          if (tr.annotation(GhostEdit)) origin = "+ghost";
        }
        const ch = { origin };
        for (const fn of listeners.change) fn(facade, ch);
        if (origin === "+input") for (const fn of listeners.inputRead) fn(facade, ch);
      }
      if (update.selectionSet) for (const fn of listeners.cursorActivity) fn(facade);
      if (update.focusChanged) {
        if (update.view.hasFocus) for (const fn of listeners.focus) fn(facade);
        else for (const fn of listeners.blur) fn(facade);
      }
    }),
    EditorView.editorAttributes.of({ class: "cm-s-material-darker CodeMirror" }),
  ].flat();

  const startDoc = options.value != null ? String(options.value) : "";
  const view = new EditorView({
    parent,
    state: EditorState.create({ doc: startDoc, extensions: baseExt }),
  });

  const onCodeThemeStorage = (event) => {
    if (event.key !== CODE_THEME_KEY) return;
    view.dispatch({ effects: codeThemeComp.reconfigure(codeThemeExtension(event.newValue)) });
  };
  window.addEventListener("storage", onCodeThemeStorage);
  let destroyed = false;

  // Fill parent
  parent.style.display = "flex";
  parent.style.flexDirection = "column";
  parent.style.minHeight = "0";
  parent.style.flex = parent.style.flex || "1";
  view.dom.style.flex = "1";
  view.dom.style.minHeight = "0";
  view.dom.style.height = "100%";

  const facade = {
    _engine: "cm6",
    _view: view,
    /** Tear down view, listeners, and mark stores (tab close / shell destroy). */
    destroy() {
      if (destroyed) return;
      destroyed = true;
      window.removeEventListener("storage", onCodeThemeStorage);
      for (const k of Object.keys(listeners)) listeners[k].length = 0;
      markStore.clear();
      lineClassStore.clear();
      gutterData.clear();
      keyMaps.length = 0;
      try {
        view.destroy();
      } catch (_) {}
    },
    getValue() {
      return view.state.doc.toString();
    },
    setValue(text) {
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: text ?? "" },
        annotations: SetValue.of(true),
      });
    },
    getSelection() {
      const s = view.state.selection.main;
      return view.state.sliceDoc(s.from, s.to);
    },
    replaceSelection(text) {
      view.dispatch(view.state.replaceSelection(text ?? ""));
    },
    replaceRange(text, from, to, origin) {
      const f = typeof from === "number" ? from : toPos(from.line, from.ch);
      const t =
        to == null ? f : typeof to === "number" ? to : toPos(to.line, to.ch);
      const ann = origin === "+ghost" ? GhostEdit.of(true) : origin === "setValue" ? SetValue.of(true) : null;
      view.dispatch({
        changes: { from: f, to: t, insert: text ?? "" },
        annotations: ann ? [ann] : [],
      });
    },
    getCursor(start) {
      const s = view.state.selection.main;
      if (start === "from") return fromPos(s.from);
      if (start === "to" || start === "head" || start === "anchor") {
        if (start === "to") return fromPos(s.to);
        if (start === "head") return fromPos(s.head);
        return fromPos(s.anchor);
      }
      return fromPos(s.head);
    },
    setCursor(pos) {
      const p = typeof pos === "number" ? pos : toPos(pos.line, pos.ch);
      view.dispatch({ selection: EditorSelection.cursor(p), scrollIntoView: true });
    },
    getLine(n) {
      const doc = view.state.doc;
      if (n < 0 || n >= doc.lines) return "";
      return doc.line(n + 1).text;
    },
    lineCount() {
      return view.state.doc.lines;
    },
    getRange(from, to) {
      return view.state.sliceDoc(toPos(from.line, from.ch), toPos(to.line, to.ch));
    },
    posFromIndex(index) {
      return fromPos(Math.max(0, Math.min(view.state.doc.length, index | 0)));
    },
    indexFromPos(pos) {
      return toPos(pos.line, pos.ch);
    },
    focus() {
      view.focus();
    },
    refresh() {
      view.requestMeasure();
    },
    defaultCharWidth() {
      return view.defaultCharacterWidth;
    },
    getOption(name) {
      if (name === "lineWrapping") return lineWrapping;
      if (name === "readOnly") return view.state.readOnly;
      if (name === "tabSize") return tabSize;
      if (name === "mode") return modeName;
      if (name === "codeTheme") return savedCodeTheme();
      return null;
    },
    setOption(name, value) {
      if (name === "lineWrapping") {
        lineWrapping = !!value;
        view.dispatch({
          effects: wrapComp.reconfigure(lineWrapping ? EditorView.lineWrapping : []),
        });
      } else if (name === "readOnly") {
        view.dispatch({ effects: roComp.reconfigure(EditorState.readOnly.of(!!value)) });
      } else if (name === "mode") {
        modeName = value;
        view.dispatch({ effects: langComp.reconfigure(languageExtension(value)) });
      } else if (name === "codeTheme") {
        const selected = normalizeCodeTheme(value);
        try { localStorage.setItem(CODE_THEME_KEY, selected); } catch (_) {}
        view.dispatch({ effects: codeThemeComp.reconfigure(codeThemeExtension(selected)) });
      } else if (name === "gutters" && Array.isArray(value)) {
        for (const g of value) {
          if (g !== "CodeMirror-linenumbers" && g !== "CodeMirror-foldgutter") ensureGutter(g);
        }
        view.dispatch({ effects: extraGutterComp.reconfigure(gutterExtensions()) });
      } else if (name === "tabSize") {
        tabSize = value | 0 || 4;
      }
    },
    on(event, handler) {
      if (listeners[event]) listeners[event].push(handler);
    },
    off(event, handler) {
      const list = listeners[event];
      if (!list) return;
      const i = list.indexOf(handler);
      if (i >= 0) list.splice(i, 1);
    },
    addKeyMap(map) {
      keyMaps.push(map);
      rebuildKeymap();
    },
    removeKeyMap(map) {
      const i = keyMaps.indexOf(map);
      if (i >= 0) {
        keyMaps.splice(i, 1);
        rebuildKeymap();
      }
    },
    operation(fn) {
      return fn();
    },
    markText(from, to, opts = {}) {
      const f = toPos(from.line, from.ch);
      const t = toPos(to.line, to.ch);
      const id = markId++;
      const entry = {
        from: f,
        to: t,
        className: opts.className || "",
        attributes: opts.attributes,
      };
      markStore.set(id, entry);
      pushMarks();
      return {
        clear() {
          markStore.delete(id);
          pushMarks();
        },
        find() {
          const m = markStore.get(id);
          if (!m) return undefined;
          return { from: fromPos(m.from), to: fromPos(m.to) };
        },
      };
    },
    setBookmark(pos, opts = {}) {
      const f = toPos(pos.line, pos.ch);
      const id = markId++;
      markStore.set(id, {
        from: f,
        to: f,
        widget: opts.widget,
        insertLeft: !!opts.insertLeft,
      });
      pushMarks();
      return {
        clear() {
          markStore.delete(id);
          pushMarks();
        },
        find() {
          const m = markStore.get(id);
          return m ? fromPos(m.from) : undefined;
        },
      };
    },
    setGutterMarker(line, gutterName, el) {
      ensureGutter(gutterName);
      const map = new Map(gutterData.get(gutterName) || []);
      if (el) map.set(line, el);
      else map.delete(line);
      gutterData.set(gutterName, map);
      view.dispatch({
        effects: [
          setGutterMap.of({ name: gutterName, map }),
          extraGutterComp.reconfigure(gutterExtensions()),
        ],
      });
    },
    clearGutter(gutterName) {
      if (!gutterReg.has(gutterName)) return;
      const empty = new Map();
      gutterData.set(gutterName, empty);
      view.dispatch({ effects: setGutterMap.of({ name: gutterName, map: empty }) });
    },
    addLineClass(line, where, cls) {
      const key = line + ":" + where + ":" + cls;
      lineClassStore.set(key, { line, where, cls });
      pushMarks();
    },
    removeLineClass(line, where, cls) {
      const key = line + ":" + where + ":" + cls;
      if (lineClassStore.delete(key)) pushMarks();
    },
    scrollIntoView(pos, margin) {
      const p = typeof pos === "number" ? pos : pos.line != null ? toPos(pos.line, pos.ch || 0) : 0;
      view.dispatch({
        effects: EditorView.scrollIntoView(p, { y: "center", yMargin: margin || 0 }),
      });
    },
    charCoords(pos, mode) {
      const p = toPos(pos.line, pos.ch);
      const rect = view.coordsAtPos(p);
      if (!rect) return { left: 0, right: 0, top: 0, bottom: 0 };
      return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
    },
    coordsChar(coords) {
      const pos = view.posAtCoords({ x: coords.left, y: coords.top });
      return pos == null ? { line: 0, ch: 0 } : fromPos(pos);
    },
    getWrapperElement() {
      return view.dom;
    },
    getScrollerElement() {
      return view.scrollDOM;
    },
    getGutterElement() {
      return view.dom.querySelector(".cm-gutters");
    },
    getScrollInfo() {
      const el = view.scrollDOM;
      return {
        left: el.scrollLeft,
        top: el.scrollTop,
        height: el.scrollHeight,
        width: el.scrollWidth,
        clientHeight: el.clientHeight,
        clientWidth: el.clientWidth,
      };
    },
    scrollTo(left, top) {
      if (left != null) view.scrollDOM.scrollLeft = left;
      if (top != null) view.scrollDOM.scrollTop = top;
    },
    somethingSelected() {
      return !view.state.selection.main.empty;
    },
    setSelection(anchor, head) {
      const a = typeof anchor === "number" ? anchor : toPos(anchor.line, anchor.ch);
      const h =
        head == null ? a : typeof head === "number" ? head : toPos(head.line, head.ch);
      view.dispatch({
        selection: EditorSelection.range(a, h),
        scrollIntoView: true,
      });
    },
    listSelections() {
      return view.state.selection.ranges.map((r) => ({
        anchor: fromPos(r.anchor),
        head: fromPos(r.head),
      }));
    },
    execCommand(name) {
      if (name === "selectAll") {
        view.dispatch({
          selection: EditorSelection.range(0, view.state.doc.length),
        });
      } else if (name === "goDocStart") {
        view.dispatch({ selection: EditorSelection.cursor(0), scrollIntoView: true });
      } else if (name === "goDocEnd") {
        view.dispatch({
          selection: EditorSelection.cursor(view.state.doc.length),
          scrollIntoView: true,
        });
      }
    },
  };

  // Alias CM5 Pass (handlers return this to fall through)
  facade.constructor = { Pass: window.CodeMirror?.Pass || Symbol("CodeMirror.Pass") };

  return facade;
}

function escHtml(value) {
  return String(value).replace(/[&<>]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[char]);
}

export function highlightCode(text, mode) {
  const source = String(text ?? "");
  const language = languageForMode(mode);
  if (!language) return escHtml(source);
  const out = [];
  let cursor = 0;
  highlightTree(language.parser.parse(source), classHighlightStyle, (from, to, classes) => {
    if (from > cursor) out.push(escHtml(source.slice(cursor, from)));
    out.push(`<span class="${classes}">${escHtml(source.slice(from, to))}</span>`);
    cursor = to;
  });
  if (cursor < source.length) out.push(escHtml(source.slice(cursor)));
  return out.join("");
}

// Global for browser IIFE bundle
export function installGlobals() {
  const Pass = Symbol("CodeMirror.Pass");
  window.createEditor = createEditor;
  window.CodeMirror = function (parent, options) {
    return createEditor(parent, options);
  };
  window.CodeMirror.Pass = Pass;
  window.CodeMirror.countColumn = countColumn;
  window.CodeMirror.version = "6-facade";
  // Non-CM5 internal API (Phase 1)
  window.AtelierCM6Native = {
    createEditor,
    countColumn,
    highlightCode,
    CODE_THEMES,
    version: "6-native",
  };
}
