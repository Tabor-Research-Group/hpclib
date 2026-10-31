// The entry of vendor/codemirror6.js: CodeMirror 6's parts <hpc-code> uses, as the global CM6. _build/build.sh makes it.
import { EditorState, Compartment, Transaction } from "@codemirror/state";
import { EditorView, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter, drawSelection,
         highlightSpecialChars, dropCursor } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { StreamLanguage, syntaxHighlighting, defaultHighlightStyle, bracketMatching, indentUnit } from "@codemirror/language";
import { searchKeymap, highlightSelectionMatches } from "@codemirror/search";
import { MergeView, unifiedMergeView } from "@codemirror/merge";
import { oneDarkHighlightStyle } from "@codemirror/theme-one-dark";
import { python } from "@codemirror/lang-python";
import { shell } from "@codemirror/legacy-modes/mode/shell";
import { yaml } from "@codemirror/legacy-modes/mode/yaml";
import { toml } from "@codemirror/legacy-modes/mode/toml";
import { javascript, json, typescript } from "@codemirror/legacy-modes/mode/javascript";
import { xml, html } from "@codemirror/legacy-modes/mode/xml";
import { css } from "@codemirror/legacy-modes/mode/css";
import { c, cpp, java } from "@codemirror/legacy-modes/mode/clike";
import { fortran } from "@codemirror/legacy-modes/mode/fortran";
import { r } from "@codemirror/legacy-modes/mode/r";
import { julia } from "@codemirror/legacy-modes/mode/julia";
import { standardSQL } from "@codemirror/legacy-modes/mode/sql";
import { properties } from "@codemirror/legacy-modes/mode/properties";

const stream = (mode) => StreamLanguage.define(mode);
// the names languageFor() in components/code.js answers with (CodeMirror 5's mode names, which pages already use)
const LANGUAGES = {
  python: () => python(), shell: () => stream(shell), yaml: () => stream(yaml), toml: () => stream(toml),
  "application/json": () => stream(json), javascript: () => stream(javascript), "text/typescript": () => stream(typescript),
  xml: () => stream(xml), htmlmixed: () => stream(html), css: () => stream(css), "text/x-csrc": () => stream(c),
  "text/x-c++src": () => stream(cpp), "text/x-java": () => stream(java), fortran: () => stream(fortran),
  r: () => stream(r), julia: () => stream(julia), sql: () => stream(standardSQL), properties: () => stream(properties),
};

/** The language extension for a name from languageFor(), or [] (plain text). */
function language(name) {
  const make = LANGUAGES[name];
  return make ? make() : [];
}

/** The colours the page's tokens give (so light and dark follow the page), and the syntax colours for each. */
const chrome = EditorView.theme({
  "&": { color: "var(--fg)", backgroundColor: "var(--bg)", height: "100%", fontSize: "13px" },
  ".cm-scroller": { fontFamily: "var(--font-mono)", lineHeight: "1.5", overflow: "auto" },
  ".cm-content": { caretColor: "var(--fg)" },
  "&.cm-focused .cm-cursor": { borderLeftColor: "var(--fg)" },
  ".cm-gutters": { backgroundColor: "var(--bg)", color: "var(--muted)", border: "none", borderRight: "1px solid var(--line)" },
  ".cm-activeLine": { backgroundColor: "color-mix(in srgb, var(--fg) 6%, transparent)" },
  ".cm-activeLineGutter": { backgroundColor: "color-mix(in srgb, var(--fg) 6%, transparent)", color: "var(--fg)" },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection":
    { backgroundColor: "color-mix(in srgb, var(--accent) 30%, transparent)" },
  ".cm-panels": { backgroundColor: "var(--bg)", color: "var(--fg)", borderColor: "var(--line)" },
  ".cm-panels input, .cm-panels button": { font: "inherit" },
  ".cm-searchMatch": { backgroundColor: "color-mix(in srgb, var(--accent) 25%, transparent)" },
  ".cm-searchMatch-selected": { backgroundColor: "color-mix(in srgb, var(--accent) 50%, transparent)" },
  // the merge view, in the page's good and bad: soft solid backgrounds (so any text colour stays readable on them),
  // a stronger one for the changed words, and none of the underline and strike-through the library adds
  ".cm-changedLine, .cm-insertedLine": { backgroundColor: "color-mix(in srgb, var(--good) 12%, var(--bg)) !important" },
  ".cm-deletedChunk": { backgroundColor: "color-mix(in srgb, var(--bad) 10%, var(--bg))", paddingLeft: "6px" },
  ".cm-deletedChunk .cm-line": { backgroundColor: "transparent" },
  ".cm-deletedLine": { backgroundColor: "color-mix(in srgb, var(--bad) 10%, var(--bg)) !important" },
  ".cm-changedText, ins.cm-insertedText": { backgroundColor: "color-mix(in srgb, var(--good) 28%, var(--bg)) !important", textDecoration: "none !important" },
  ".cm-deletedChunk .cm-changedText, del.cm-deletedText": { backgroundColor: "color-mix(in srgb, var(--bad) 28%, var(--bg)) !important", textDecoration: "none !important" },
  ".cm-changedLine .cm-changedText": { background: "color-mix(in srgb, var(--good) 28%, var(--bg)) !important" },
  ".cm-collapsedLines": { color: "var(--muted)", backgroundColor: "color-mix(in srgb, var(--fg) 6%, var(--bg))", cursor: "pointer", padding: "2px 8px" },
  ".cm-changeGutter": { width: "4px" },
});

const highlight = (dark) => syntaxHighlighting(dark ? oneDarkHighlightStyle : defaultHighlightStyle, { fallback: true });

/** What every editor has, whatever it edits: the basics, history, search, and Tab to indent. */
function basics() {
  return [
    lineNumbers(), highlightActiveLineGutter(), highlightSpecialChars(), history(), drawSelection(), dropCursor(),
    bracketMatching(), highlightActiveLine(), highlightSelectionMatches(), indentUnit.of("    "),
    EditorState.tabSize.of(4), chrome,
    keymap.of([indentWithTab, ...searchKeymap, ...historyKeymap, ...defaultKeymap]),
  ];
}

globalThis.CM6 = {
  EditorState, EditorView, Compartment, Transaction, keymap, MergeView, unifiedMergeView, language, highlight, basics,
  versions: "@codemirror/view 6.43.15, state 6.7.6, merge 6.12.3",
};
