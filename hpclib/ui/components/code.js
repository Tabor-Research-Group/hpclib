// ------------------------------------------------------------------ <hpc-code> and <hpc-diff>
//
// A text editor for the page: CodeMirror 6 when it loads (vendor/codemirror6.js, which libs.json hashes), else a plain
// textarea with the same methods, so a page works even when the script is blocked. The element is in the light DOM,
// because the editor's own styles are page styles. Give it a height (it fills its parent when that has one).
//
//   const ed = el("hpc-code", { language: "python" });
//   parent.append(ed);
//   await ed.ready;                         // it has loaded (or fallen back to a textarea)
//   ed.value = text;  ed.markClean();       // ed.value reads it back; ed.isClean says whether it changed
//   ed.addEventListener("change", ...);     // an edit
//   ed.addEventListener("save", ...);       // Ctrl or ⌘-S
//
// Attributes: language (a name languageFor(path) answers with), readonly. ed.plain is true for the fallback.
//
// <hpc-diff> shows what changes between two texts, one view with the changed lines marked, the long unchanged runs
// folded, and the changed words marked inside a line (CodeMirror's merge view):
//
//   const diff = el("hpc-diff", { language: "shell" });
//   diff.show(originalText, newText);       // re-callable; `same` is true when they are the same text

import { isDark, onThemeChange } from "../core.js";
import { loadLib } from "../libs.js";

// name: what CM6.language() knows (the names CodeMirror 5 used, which pages already use)
const MODES = {
  py: "python", pyw: "python", ipy: "python", sh: "shell", bash: "shell", zsh: "shell", slurm: "shell",
  sbatch: "shell", job: "shell", bashrc: "shell", profile: "shell", yaml: "yaml", yml: "yaml", cff: "yaml",
  json: "application/json", js: "javascript", mjs: "javascript", ts: "text/typescript", toml: "toml", lock: "toml",
  md: "markdown", markdown: "markdown", xml: "xml", html: "htmlmixed", htm: "htmlmixed", svg: "xml", css: "css",
  c: "text/x-csrc", h: "text/x-csrc", cpp: "text/x-c++src", cc: "text/x-c++src", hpp: "text/x-c++src",
  cu: "text/x-c++src", java: "text/x-java", f: "fortran", f90: "fortran", f95: "fortran", f03: "fortran",
  r: "r", jl: "julia", sql: "sql", ini: "properties", cfg: "properties", conf: "properties",
};

/** The mode for a file name ("train.py" -> "python"), or null. */
export function languageFor(path) {
  const name = String(path).replace(/^.*\//, "");
  const m = name.match(/\.([^.]+)$/);
  return MODES[m ? m[1].toLowerCase() : name.toLowerCase()] || null;
}

const parts = () => loadLib("codemirror").then((g) => g.CM6);

export class HpcCode extends HTMLElement {
  static observedAttributes = ["language", "readonly"];

  constructor() {
    super();
    this.plain = false;
    this._text = "";
    this._saved = "";
    this.ready = new Promise((resolve) => { this._ready = resolve; });
  }

  connectedCallback() {
    if (this._started) return;
    this._started = true;
    this._start();
  }

  attributeChangedCallback(name) {
    if (!this._view) { if (this._ta && name === "readonly") this._ta.readOnly = this.hasAttribute("readonly"); return; }
    const { language, readonly } = this._c;
    if (name === "language") this._view.dispatch({ effects: language.reconfigure(this._language()) });
    if (name === "readonly") this._view.dispatch({ effects: readonly.reconfigure(this._readOnly()) });
  }

  _language() { return this._cm.language(this.getAttribute("language") || ""); }
  _readOnly() { return this._cm.EditorState.readOnly.of(this.hasAttribute("readonly")); }

  async _start() {
    let cm = null;
    try { cm = await parts(); } catch { /* a textarea instead */ }
    if (cm) {
      this._cm = cm;
      const c = this._c = { language: new cm.Compartment(), readonly: new cm.Compartment(), theme: new cm.Compartment() };
      const save = () => { this.dispatchEvent(new CustomEvent("save")); return true; };
      const view = new cm.EditorView({
        parent: this,
        state: cm.EditorState.create({
          doc: this._text,
          extensions: [
            cm.keymap.of([{ key: "Mod-s", run: save, preventDefault: true }]),
            cm.basics(), c.language.of(this._language()), c.readonly.of(this._readOnly()), c.theme.of(cm.highlight(isDark())),
            cm.EditorView.updateListener.of((u) => { if (u.docChanged) this.dispatchEvent(new CustomEvent("change")); }),
          ],
        }),
      });
      this._view = view;
      this._clean = view.state.doc;
      onThemeChange(() => view.dispatch({ effects: c.theme.reconfigure(cm.highlight(isDark())) }));
    } else {
      this.plain = true;
      const ta = document.createElement("textarea");
      ta.className = "plain";
      ta.spellcheck = false;
      ta.readOnly = this.hasAttribute("readonly");
      ta.value = this._text;
      ta.addEventListener("input", () => this.dispatchEvent(new CustomEvent("change")));
      ta.addEventListener("keydown", (e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); this.dispatchEvent(new CustomEvent("save")); }
      });
      this.append(ta);
      this._ta = ta;
    }
    this._ready(this);
  }

  get value() { return this._view ? this._view.state.doc.toString() : this._ta ? this._ta.value : this._text; }
  set value(text) {
    this._text = String(text);
    if (this._view) {
      this._view.dispatch({ changes: { from: 0, to: this._view.state.doc.length, insert: this._text }, selection: { anchor: 0 },
        annotations: this._cm.Transaction.addToHistory.of(false) });   // loading isn't an edit to undo
    } else if (this._ta) this._ta.value = this._text;
  }

  /** What is in the editor now is the saved state (after a save or a load). */
  markClean() {
    if (this._view) this._clean = this._view.state.doc; else this._saved = this.value;
  }
  get isClean() { return this._view ? this._view.state.doc.eq(this._clean) : this.value === this._saved; }

  focus() { if (this._view) this._view.focus(); else if (this._ta) this._ta.focus(); }
  /** Puts the cursor at the start of a line (1-based; past the end goes to the last line) and scrolls it to the middle. */
  goto(line) {
    const n = Math.max(1, Math.floor(Number(line)) || 1);
    if (this._view) {
      const at = this._view.state.doc.line(Math.min(n, this._view.state.doc.lines));
      this._view.dispatch({ selection: { anchor: at.from }, effects: this._cm.EditorView.scrollIntoView(at.from, { y: "center" }) });
    } else if (this._ta) {
      const lines = this._ta.value.split("\n");
      const offset = lines.slice(0, Math.min(n, lines.length) - 1).reduce((sum, l) => sum + l.length + 1, 0);
      this._ta.setSelectionRange(offset, offset);
    }
    this.focus();
  }
  /** Call when the editor's size or visibility changed (a tab was shown). */
  refresh() { if (this._view) this._view.requestMeasure(); }
  /** The CodeMirror EditorView (null for the textarea fallback), for tests and for what this element doesn't offer. */
  get view() { return this._view || null; }
}

export class HpcDiff extends HTMLElement {
  static observedAttributes = ["language"];

  constructor() {
    super();
    this.same = true;
    this._texts = ["", ""];
    this.ready = new Promise((resolve) => { this._ready = resolve; });
  }

  connectedCallback() {
    if (this._started) return;
    this._started = true;
    parts().then((cm) => { this._cm = cm; }, () => null).then(() => { this._draw(); this._ready(this); });
    onThemeChange(() => this._draw());
  }

  attributeChangedCallback() { if (this._started) this._draw(); }

  /** Shows what changing `original` into `modified` does. */
  show(original, modified) {
    const texts = [String(original), String(modified)];
    if (texts[0] === this._texts[0] && texts[1] === this._texts[1] && this._drawn) return;   // nothing new: keep the view
    this._texts = texts;
    this.same = texts[0] === texts[1];
    if (this._started && (this._cm || this._failed)) this._draw();
  }

  _draw() {
    this._drawn = true;
    this._view?.destroy();
    this._view = null;
    const [original, modified] = this._texts;
    const cm = this._cm;
    if (!cm) {
      this._failed = true;
      const pre = document.createElement("pre");
      pre.className = "plain";
      pre.textContent = modified;
      this.replaceChildren(pre);
      return;
    }
    this.replaceChildren();
    this._view = new cm.EditorView({
      parent: this,
      state: cm.EditorState.create({
        doc: modified,
        extensions: [
          cm.basics(), cm.language(this.getAttribute("language") || ""),   // no syntax colours: they muddy the shading
          cm.EditorState.readOnly.of(true), cm.EditorView.editable.of(false),
          cm.unifiedMergeView({ original, mergeControls: false, gutter: true, highlightChanges: true,
                                collapseUnchanged: { margin: 2, minSize: 4 } }),
        ],
      }),
    });
  }

  /** The CodeMirror EditorView (null before it loads, or for the fallback). */
  get view() { return this._view || null; }
}

if (!customElements.get("hpc-code")) customElements.define("hpc-code", HpcCode);
if (!customElements.get("hpc-diff")) customElements.define("hpc-diff", HpcDiff);
