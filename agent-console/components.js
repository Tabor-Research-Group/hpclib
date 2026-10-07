// Custom elements for the console's front end: native Web Components, no library and no build step.
// Colours come from the page's CSS variables (--fg, --line, ...), which reach into shadow DOM, so light and dark
// themes apply everywhere. app.js gives them its API function with configure({ api }).
//
//   <hpc-panel heading="…" status="…" tone="error|ok" dismissible>   a bordered panel; × hides it
//     <button slot="actions">…</button>  …body…
//   </hpc-panel>
//   <hpc-log src="clusters/X/tunnel/log?lines=300" field="lines" every="3"></hpc-log>
//                                                       output that fetches itself, refreshes, keeps to the bottom
//   <hpc-dialog heading="…" transient> …form… </hpc-dialog>  a modal; .showModal(), .close(), "close" event
//   <hpc-state state="up" label="running"></hpc-state>     a state word in its colour
//   <hpc-field label="…" hint="…"><input></hpc-field>     a labelled form control
//   <hpc-action-button label="…" primary result>           a button running an async .action, its error beside it
//   <hpc-cluster-picker live value="…">                    a <select> of clusters (.clusters, or fetched)
//   <hpc-env-editor label="…">                             NAME=value lines, checked as you type (.rules, .value)
//   <hpc-cluster-row cluster="…" local="0.2.24">           an agent profile's rows in a table: refreshes itself
//   <hpc-app-session app="jupyter" app-title="…" cluster="…">   a tunnel app's session on one cluster, likewise
//
//   <hpclib-toolbar>                                      a row of tools under a cluster/tunnel row
//     <hpclib-toolbar-tool label="…">…text, a select…</hpclib-toolbar-tool>
//   <hpclib-extra-controls>                               controls under a row's buttons, on the right
//     <hpclib-control>…a button…</hpclib-control>
//   confirmDialog(heading, text, ok): a themed yes/no question, resolving to true or false
//
// Rows ask the page for what only it can do with events that bubble to document: "hpc-login" {cluster, then},
// "hpc-prompt" {label, answer, prompt}, "hpc-setup" {cluster, row} and "hpc-app-settings" {app, session, row}.

let fetcher = async () => { throw new Error("components.js: call configure({ api }) first"); };

export function configure({ api }) {
  fetcher = api;
}

/** el("tag", {attr: value, onclick: fn}, ...children): a DOM element. */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

/** An <hpc-action-button> running `fn`. */
export function action(label, fn, attrs = {}) {
  const b = el("hpc-action-button", { label, ...attrs });
  b.action = fn;
  return b;
}

const ask = (node, name, detail) => node.dispatchEvent(new CustomEvent(name, { bubbles: true, composed: true, detail }));
const enc = encodeURIComponent;

const css = (text) => {
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(text);
  return sheet;
};

// ------------------------------------------------------------------ <hpc-panel>

const PANEL_CSS = css(`
  :host { display: block; margin: 4px 0 12px; }
  :host([hidden]) { display: none; }
  section {
    position: relative;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 10px 14px 12px;
    background: var(--bg);
  }
  header { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; min-height: 24px; padding-right: 28px; }
  header:not(.has-content) { display: none; }
  .heading { font-weight: 600; }
  .status { color: var(--muted); }
  .status.error { color: var(--bad); }
  .status.ok { color: var(--good); }
  .body { margin-top: 8px; }
  header:not(.has-content) + .body { margin-top: 0; }
  button.close {
    position: absolute;
    top: 6px;
    right: 8px;
    width: 24px;
    height: 24px;
    padding: 0;
    border: none;
    border-radius: 6px;
    background: none;
    color: var(--muted);
    font: 18px/24px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    cursor: pointer;
  }
  button.close:hover { background: var(--panel); color: var(--fg); }
  button.close[hidden] { display: none; }
  ::slotted([slot="actions"]) { margin-left: 2px; }
`);

export class HpcPanel extends HTMLElement {
  static observedAttributes = ["heading", "status", "tone", "dismissible"];

  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    root.adoptedStyleSheets = [PANEL_CSS];
    root.innerHTML = `<section part="panel">
        <header part="header"><span class="heading" part="heading"></span><span class="status" part="status"></span>
          <slot name="actions"></slot></header>
        <div class="body" part="body"><slot></slot></div>
        <button class="close" part="close" type="button" aria-label="Dismiss" title="Dismiss" hidden>×</button>
      </section>`;
    this._parts = {
      header: root.querySelector("header"),
      heading: root.querySelector(".heading"),
      status: root.querySelector(".status"),
      close: root.querySelector(".close"),
    };
    this._parts.close.addEventListener("click", () => this.dismiss());
    root.querySelector('slot[name="actions"]').addEventListener("slotchange", () => this._render());
  }

  connectedCallback() { this._render(); }
  attributeChangedCallback() { this._render(); }

  _render() {
    const p = this._parts;
    p.heading.textContent = this.getAttribute("heading") || "";
    p.status.textContent = this.getAttribute("status") || "";
    p.status.className = `status ${this.getAttribute("tone") || ""}`;
    p.close.hidden = !this.hasAttribute("dismissible");
    const hasActions = !!this.querySelector(':scope > [slot="actions"]');
    p.header.classList.toggle("has-content", !!(p.heading.textContent || p.status.textContent || hasActions));
  }

  /** Hides the panel, unless a "dismiss" listener calls preventDefault(). */
  dismiss() {
    const event = new CustomEvent("dismiss", { cancelable: true, bubbles: true });
    if (this.dispatchEvent(event)) this.hidden = true;
  }
}

// ------------------------------------------------------------------ <hpc-log>

export class HpcLog extends HTMLElement {
  // src: an API path; field: the list of lines in its answer ("lines"); every: seconds between refreshes (0: once);
  // empty: what to show when there is nothing yet. Emits "update" with the whole answer after each fetch.
  static observedAttributes = ["src", "every"];

  constructor() {
    super();
    this._pre = document.createElement("pre");
    this._pre.className = "log";
    this._pre.textContent = "…";
    this._stick = true;
    this._pre.addEventListener("scroll", () => {
      const p = this._pre;
      this._stick = p.scrollHeight - p.scrollTop - p.clientHeight < 24;   // follow only while at the bottom
    });
  }

  connectedCallback() {
    if (!this._pre.isConnected) this.append(this._pre);
    this.refresh();
    this._schedule();
  }

  disconnectedCallback() { clearTimeout(this._timer); }

  attributeChangedCallback(name, old, value) {
    if (!this.isConnected || old === value) return;
    if (name === "src") this.refresh();
    this._schedule();
  }

  _schedule() {
    clearTimeout(this._timer);
    const every = Number(this.getAttribute("every") || 0);
    if (every > 0) {
      this._timer = setTimeout(async () => {
        await this.refresh();
        this._schedule();
      }, every * 1000);
    }
  }

  async refresh() {
    const src = this.getAttribute("src");
    if (!src || !this.isConnected) return;
    let data;
    try {
      data = await fetcher(src);
    } catch (err) {
      this._pre.textContent = err.message || String(err);
      this._pre.classList.add("error");
      return;
    }
    this._pre.classList.remove("error");
    const lines = data[this.getAttribute("field") || "lines"] || [];
    this._pre.textContent = lines.join("\n") || this.getAttribute("empty") || "(no output yet)";
    if (this._stick) this._pre.scrollTop = this._pre.scrollHeight;
    this.dispatchEvent(new CustomEvent("update", { detail: data }));
  }

  /** Stop refreshing (e.g. once an operation has finished). */
  stop() { this.removeAttribute("every"); }
}

// ------------------------------------------------------------------ <hpc-dialog>

const DIALOG_CSS = css(`
  dialog {
    position: relative;
    width: min(440px, calc(100vw - 32px));
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 16px 18px;
    background: var(--bg);
    color: var(--fg);
  }
  dialog::backdrop { background: rgb(0 0 0 / 0.35); }
  h3 { font-size: 15px; margin: 0 28px 10px 0; }
  h3:empty { display: none; }
  button.close {
    position: absolute; top: 10px; right: 10px; width: 24px; height: 24px; padding: 0;
    border: none; border-radius: 6px; background: none; color: var(--muted);
    font: 18px/24px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; cursor: pointer;
  }
  button.close:hover { background: var(--panel); color: var(--fg); }
`);

export class HpcDialog extends HTMLElement {
  // heading: its title; transient: remove the element once closed. Escape and × close it.
  static observedAttributes = ["heading"];

  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    root.adoptedStyleSheets = [DIALOG_CSS];
    root.innerHTML = `<dialog part="dialog"><button class="close" type="button" aria-label="Close" title="Close">×</button>
      <h3 part="heading"></h3><slot></slot></dialog>`;
    this._dialog = root.querySelector("dialog");
    this._heading = root.querySelector("h3");
    root.querySelector(".close").addEventListener("click", () => this.close());
    this._dialog.addEventListener("close", () => {
      this.dispatchEvent(new CustomEvent("close"));
      if (this.hasAttribute("transient")) this.remove();
    });
  }

  connectedCallback() { this._heading.textContent = this.getAttribute("heading") || ""; }
  attributeChangedCallback() { this._heading.textContent = this.getAttribute("heading") || ""; }

  get open() { return this._dialog.open; }

  showModal() {
    if (!this.isConnected) document.body.append(this);
    if (!this._dialog.open) this._dialog.showModal();
    const first = this.querySelector("[autofocus], input:not([type=hidden]), textarea, select, button");
    if (first) first.focus();
  }

  close() {
    if (this._dialog.open) this._dialog.close();
  }
}

// ------------------------------------------------------------------ <hpc-state>

// every state word the API uses, by how it should look
const TONES = {
  up: ["up", "connected", "running", "succeeded", "logged in"],
  starting: ["starting", "queued", "password_sent", "push_sent", "waiting", "behind"],
  down: ["down", "none", "stopped", "expired", "not set up"],
  error: ["error", "failed"],
};
const TONE_OF = Object.fromEntries(Object.entries(TONES).flatMap(([tone, words]) => words.map((w) => [w, tone])));

export class HpcState extends HTMLElement {
  // state: the API's word (up, queued, failed, ...); label: the text to show (default: the state); tone: override
  static observedAttributes = ["state", "label", "tone"];

  connectedCallback() { this._render(); }
  attributeChangedCallback() { this._render(); }

  _render() {
    const state = this.getAttribute("state") || "";
    this.textContent = this.getAttribute("label") ?? state;
    this.className = `state ${this.getAttribute("tone") || TONE_OF[state] || "down"}`;
  }
}

// ------------------------------------------------------------------ <hpc-field>

export class HpcField extends HTMLElement {
  // label, hint: text around its one child control, wrapped in a real <label> so clicking the text focuses it
  static observedAttributes = ["label", "hint"];

  connectedCallback() {
    if (!this._label) {
      const control = this.firstElementChild;
      this._label = document.createElement("label");
      this._label.className = "field";
      this._text = document.createElement("span");
      this._hint = document.createElement("small");
      this._hint.className = "muted";
      this._label.append(this._text);
      if (control) this._label.append(control);
      this._label.append(this._hint);
      this.replaceChildren(this._label);
    }
    this._render();
  }

  attributeChangedCallback() { if (this._label) this._render(); }

  _render() {
    this._text.textContent = this.getAttribute("label") || "";
    this._hint.textContent = this.getAttribute("hint") || "";
    this._hint.hidden = !this._hint.textContent;
  }
}

// ------------------------------------------------------------------ <hpc-action-button>

export class HpcActionButton extends HTMLElement {
  // label, primary, disabled: the button's. .action = async () => ...: runs on click, with the button off until it
  // finishes; an error shows beside the button (× clears it), so it stays next to what failed. result: also show
  // the string the action returns ("Saved."). Fires "done" with the action's value.
  static observedAttributes = ["label", "primary", "disabled"];

  constructor() {
    super();
    this.button = el("button", { type: "button" });
    this.out = el("span", { class: "action-out" });
    this.button.addEventListener("click", () => this.run());
    this._running = false;
  }

  connectedCallback() {
    if (!this.button.isConnected) this.append(this.button, this.out);
    this._render();
  }
  attributeChangedCallback() { this._render(); }

  get disabled() { return this.hasAttribute("disabled"); }
  set disabled(v) { this.toggleAttribute("disabled", !!v); }

  _render() {
    this.button.textContent = this.getAttribute("label") || "";
    this.button.className = this.hasAttribute("primary") ? "primary" : "";
    this.button.disabled = this._running || this.disabled;
  }

  async run() {
    if (this._running || !this.action) return;
    this._running = true;
    this._render();
    this.clear();
    try {
      const value = await this.action();
      if (this.hasAttribute("result") && value) this.show(String(value), "saved");
      this.dispatchEvent(new CustomEvent("done", { detail: value }));
    } catch (err) {
      this.show(err.message || String(err), "error");
    } finally {
      this._running = false;
      this._render();
    }
  }

  show(text, kind) {
    const x = el("button", { type: "button", class: "x", title: "Dismiss", "aria-label": "Dismiss" }, "×");
    x.addEventListener("click", () => this.clear());
    this.out.replaceChildren(el("span", { class: kind }, text), x);
  }
  clear() { this.out.replaceChildren(); }
}

// ------------------------------------------------------------------ <hpc-cluster-picker>

export class HpcClusterPicker extends HTMLElement {
  // .clusters: GET /api/clusters's list (fetched if not given). live: only clusters whose tunnel is up and which
  // have an owner token (what pages reading the REST server need). value: the picked cluster; it may also be given
  // as the login host or the MCP name. .selected: the picked cluster's record. Fires "change".
  static observedAttributes = ["value", "live"];

  static isLive(c) { return !!(c.tunnel && c.tunnel.state === "up" && c.has_owner_token); }
  static matches(c, v) { return !!v && [c.name, c.host, c.mcp_name].includes(v); }

  constructor() {
    super();
    this.select = el("select");
    this.select.addEventListener("change", (e) => {
      e.stopPropagation();
      this.setAttribute("value", this.select.value);
      this.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  connectedCallback() {
    if (!this.select.isConnected) this.append(this.select);
    if (!this._all) {
      fetcher("clusters").then(({ clusters }) => { this.clusters = clusters; })
        .catch((err) => { this.select.replaceChildren(el("option", {}, err.message)); });
    }
    this._render();
  }
  attributeChangedCallback() { this._render(); }

  set clusters(list) { this._all = list; this._render(); }
  get clusters() { return this._all || []; }
  get options() { return this.clusters.filter((c) => !this.hasAttribute("live") || HpcClusterPicker.isLive(c)); }
  get selected() {
    const list = this.options;
    return list.find((c) => HpcClusterPicker.matches(c, this.getAttribute("value"))) || list[0] || null;
  }
  get value() { return this.selected ? this.selected.name : null; }
  set value(v) { this.setAttribute("value", v); }

  _render() {
    if (!this._all) return;
    const pick = this.selected;
    this.select.replaceChildren(...this.options.map((c) => el("option", { value: c.name, selected: c === pick }, c.name)));
    this.select.disabled = !this.options.length;
  }
}

// ------------------------------------------------------------------ <hpc-env-editor>

export class HpcEnvEditor extends HTMLElement {
  // Environment variables as NAME=value lines (# comments allowed), checked as you type against .rules: the
  // server's own (GET /admin/config's rules.environment: name_pattern, reserved, reserved_prefixes, max_value);
  // without them only the NAME=value form is checked, and the server still decides on save.
  // .value: {NAME: value} (throws, naming the line, if one is wrong); label: names it in those messages.
  constructor() {
    super();
    this.textarea = el("textarea", { rows: 3, spellcheck: "false", placeholder: "NAME=value, one per line" });
    this.messages = el("ul", { class: "env-errors" });
    this.textarea.addEventListener("input", () => this._check());
    this.rules = null;
  }

  connectedCallback() {
    if (!this.textarea.isConnected) this.append(this.textarea, this.messages);
    if (this.hasAttribute("rows")) this.textarea.rows = Number(this.getAttribute("rows"));
    this._check();
  }

  set value(vars) {
    this.textarea.value = Object.entries(vars || {}).map(([k, v]) => `${k}=${v}`).join("\n");
    this._check();
  }
  get value() {
    const { vars, errors } = this.parse();
    if (errors.length) throw new Error(`${this.getAttribute("label") || "Environment"}: ${errors[0]}`);
    return vars;
  }
  get errors() { return this.parse().errors; }

  parse() {
    const r = this.rules || {};
    const pattern = new RegExp(`^(?:${r.name_pattern || "[A-Za-z_][A-Za-z0-9_]*"})$`);
    const vars = {};
    const errors = [];
    this.textarea.value.split("\n").forEach((raw, i) => {
      const line = raw.trim();
      if (!line || line.startsWith("#")) return;
      const at = `line ${i + 1}`;
      const eq = line.indexOf("=");
      if (eq < 1) return errors.push(`${at}: "${line}" is not NAME=value`);
      const name = line.slice(0, eq).trim();
      const value = line.slice(eq + 1).trim();
      if (!pattern.test(name)) errors.push(`${at}: ${name} is not a variable name`);
      else if ((r.reserved || []).includes(name) || (r.reserved_prefixes || []).some((p) => name.startsWith(p))) {
        errors.push(`${at}: ${name} can't be set here (it would change PATH, the sandbox, hpclib or SLURM)`);
      } else if (r.max_value && value.length > r.max_value) errors.push(`${at}: the value is over ${r.max_value} characters`);
      else if (name in vars) errors.push(`${at}: ${name} is set twice`);
      vars[name] = value;
    });
    return { vars, errors };
  }

  _check() {
    const { errors } = this.parse();
    this.messages.replaceChildren(...errors.map((e) => el("li", {}, e)));
    this.textarea.toggleAttribute("aria-invalid", errors.length > 0);
  }
}

// ------------------------------------------------------------------ <hpclib-toolbar>, <hpclib-extra-controls>

export class HpclibToolbar extends HTMLElement {
  // A row of <hpclib-toolbar-tool>s that wraps when narrow; hidden while it holds none.
  connectedCallback() { this._sync(); }
  _sync() { this.hidden = !this.querySelector(":scope > hpclib-toolbar-tool"); }
  /** Replace its tools. */
  setTools(tools) {
    this.replaceChildren(...tools);
    this._sync();
  }
}

export class HpclibToolbarTool extends HTMLElement {
  // label: a small caption above its content (text, a select, a few inputs: anything wider than a button)
  static observedAttributes = ["label"];
  constructor() {
    super();
    this._caption = el("span", { class: "tool-label" });
    this._body = el("div", { class: "tool-body" });
  }
  connectedCallback() {
    if (!this._caption.isConnected) {
      this._body.append(...this.childNodes);
      this.append(this._caption, this._body);
    }
    this._render();
  }
  attributeChangedCallback() { this._render(); }
  _render() {
    this._caption.textContent = this.getAttribute("label") || "";
    this._caption.hidden = !this._caption.textContent;
  }
  /** Its content, replaced. */
  set content(nodes) { this._body.replaceChildren(...[nodes].flat()); }
}

export class HpclibExtraControls extends HTMLElement {
  // A column of <hpclib-control>s under a row's main buttons; hidden while it holds none.
  connectedCallback() { this._sync(); }
  _sync() { this.hidden = !this.querySelector(":scope > hpclib-control"); }
  setControls(controls) {
    this.replaceChildren(...controls);
    this._sync();
  }
}

export class HpclibControl extends HTMLElement {
  // Wraps one control, usually an <hpc-action-button>; hint: a line of small text under it.
  static observedAttributes = ["hint"];
  connectedCallback() { this._render(); }
  attributeChangedCallback() { this._render(); }
  _render() {
    let hint = this.querySelector(":scope > .control-hint");
    const text = this.getAttribute("hint") || "";
    if (!text) { if (hint) hint.remove(); return; }
    if (!hint) { hint = el("div", { class: "control-hint muted small" }); this.append(hint); }
    hint.textContent = text;
  }
}

/** A yes/no question in an <hpc-dialog>; resolves true for `ok`, false for Cancel, Escape or ×. */
export function confirmDialog(heading, text, ok = "OK") {
  return new Promise((resolve) => {
    const yes = el("button", { class: "primary", type: "button" }, ok);
    const no = el("button", { type: "button" }, "Cancel");
    const dialog = el("hpc-dialog", { heading, transient: true }, el("p", {}, text), el("div", { class: "actions" }, yes, no));
    let answer = false;
    yes.addEventListener("click", () => { answer = true; dialog.close(); });
    no.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => resolve(answer));
    dialog.showModal();
    no.focus();
  });
}

// ------------------------------------------------------------------ rows: <hpc-cluster-row>, <hpc-app-session>

export const LOGIN_BUSY = ["starting", "password_sent", "push_sent"];
export const LOGIN_LABEL = { connected: "logged in", none: "not logged in", expired: "login ended",
                             failed: "login failed", starting: "connecting", password_sent: "password sent",
                             push_sent: "approve the push" };

// Shared by both rows: a table row group (display: table-row-group) of the row itself and a detail row under it
// for one panel (a log, a form, an operation). The row fetches its own state (src) every 3 s while something is
// in motion (busy) and every 30 s otherwise, and redraws only itself; its buttons are made once and kept, so an
// error shown beside one survives the refreshes. Subclasses give src, key, busy, makeButtons() and cells().
class TunnelRow extends HTMLElement {
  static openLogs = new Set();       // logs left open stay open when the page is drawn again
  // operation panels (install_hpclib, setup_agents, a tunnel's install) closed with ×, by cluster and start time;
  // remembered in this browser
  static dismissedOps = new Set((() => {
    try { return JSON.parse(localStorage.getItem("hpc-dismissed-ops") || "[]"); } catch { return []; }
  })());
  static dismissOp(id) {
    TunnelRow.dismissedOps.add(id);
    try { localStorage.setItem("hpc-dismissed-ops", JSON.stringify([...TunnelRow.dismissedOps].slice(-50))); }
    catch { /* this page only */ }
  }

  constructor() {
    super();
    this.main = el("tr");
    this.detailCell = el("td");
    this.detailRow = el("tr", { class: "detail-row", hidden: true }, this.detailCell);
    // tunnel-specific tools, under the row (see HpcAppSession)
    this.toolbar = el("hpclib-toolbar", { hidden: true });
    this.toolbarCell = el("td", {}, this.toolbar);
    this.toolbarRow = el("tr", { class: "toolbar-row", hidden: true }, this.toolbarCell);
    this.detailKind = "";
    this.buttons = null;
  }

  connectedCallback() {
    if (!this.main.isConnected) this.append(this.main, this.toolbarRow, this.detailRow);
    if (this._data) { this._draw(); this._schedule(); } else this.refresh();
  }
  disconnectedCallback() { clearTimeout(this._timer); }

  get data() { return this._data; }
  set data(d) {
    this._data = d;
    this._error = null;
    if (!this.buttons) this.buttons = this.makeButtons();
    this._draw();
    this.updated();
    this._schedule();
    if (!this._restored) {
      this._restored = true;
      if (TunnelRow.openLogs.has(this.key) && !this.detailKind) this.toggleLog();
    }
    this.dispatchEvent(new CustomEvent("update", { detail: d }));
  }

  async refresh() {
    clearTimeout(this._timer);
    try {
      this.data = await fetcher(this.src);
    } catch (err) {
      this._error = err;
      this._draw();
      this._schedule();
    }
  }

  updated() {}

  // the cluster's operation, if it is one this row shows (opMine), in the detail row
  syncOperation(op) {
    const id = op && `${this.name}@${op.started}`;
    if (op && this.opMine(op) && id !== this._opShown && !TunnelRow.dismissedOps.has(id)) this.showOperation(id);
  }
  opMine() { return false; }

  showOperation(id) {
    // the operation's log, refreshing while it runs; the row refreshes too, so its buttons follow
    this._opShown = id;
    const log = el("hpc-log", { src: `clusters/${enc(this.name)}/operation?lines=400`, field: "log", every: 2 });
    const panel = el("hpc-panel", { heading: "hpclib", dismissible: true }, log);
    log.addEventListener("update", ({ detail: op }) => {
      panel.setAttribute("heading", op.title || op.kind);
      panel.setAttribute("status", { running: "running…", succeeded: "finished",
                                     failed: `failed (exit ${op.exit_code})` }[op.state] || op.state);
      panel.setAttribute("tone", { succeeded: "ok", failed: "error" }[op.state] || "");
      if (op.state !== "running") { log.stop(); this.refresh(); }
    });
    panel.addEventListener("dismiss", () => TunnelRow.dismissOp(id));
    this.showDetail("operation", panel);
  }

  _schedule() {
    clearTimeout(this._timer);
    if (!this.isConnected) return;
    this._timer = setTimeout(() => this.refresh(), (this._data && this.busy ? 3 : 30) * 1000);
  }

  _draw() {
    if (!this._data) return;
    const cells = this.cells();
    if (this._error) cells[0] = [cells[0], el("div", { class: "error-box small" }, `not refreshed: ${this._error.message}`)];
    this.main.replaceChildren(...cells.map((c) => el("td", {}, c)));
    this.detailCell.colSpan = this.toolbarCell.colSpan = cells.length;
    this.toolbarRow.hidden = this.toolbar.hidden;
  }

  // the detail row
  showDetail(kind, panel) {
    panel.addEventListener("dismiss", (e) => { e.preventDefault(); this.hideDetail(); });
    if (this.detailKind === "log") TunnelRow.openLogs.delete(this.key);
    this.detailCell.replaceChildren(panel);
    this.detailKind = kind;
    this.detailRow.hidden = false;
    return panel;
  }
  hideDetail() {
    if (this.detailKind === "log") TunnelRow.openLogs.delete(this.key);
    this.detailRow.hidden = true;
    this.detailKind = "";
    this.detailCell.replaceChildren();
  }
  toggleLog() {
    if (this.detailKind === "log") return this.hideDetail();
    this.showDetail("log", el("hpc-panel", { heading: this.logHeading, dismissible: true },
      el("hpc-log", { src: this.logSrc, every: 3, empty: "(no log yet)" })));
    TunnelRow.openLogs.add(this.key);
  }

  // shared cells
  loginCells(lg, cluster) {
    const b = this.buttons.login;
    const connected = lg.state === "connected";
    b.setAttribute("label", connected ? "Log out" : "Log in");
    b.disabled = LOGIN_BUSY.includes(lg.state);
    b.action = connected
      ? async () => { await fetcher(`clusters/${enc(cluster.name)}/logout`, { method: "POST" }); await this.refresh(); }
      : () => ask(this, "hpc-login", { cluster, then: () => this.refresh() });
    return [el("hpc-state", { state: lg.state || "none", label: LOGIN_LABEL[lg.state] || lg.state || "" }),
            lg.state === "failed" && lg.message ? el("div", { class: "muted" }, lg.message) : null,
            el("div", {}, b)];
  }
  promptLine(label, answer, prompt) {
    if (!prompt) return null;
    const hostkey = prompt.kind === "hostkey";
    const b = el("button", { class: "primary", type: "button" }, hostkey ? "Review host key…" : "Enter password…");
    b.addEventListener("click", () => ask(this, "hpc-prompt", { label, answer, prompt }));
    return el("div", {}, el("hpc-state", { tone: "starting",
      label: hostkey ? `asks to trust ${prompt.host || "a node"}'s host key` : "asks for your password" }), " ", b);
  }
}

export class HpcClusterRow extends TunnelRow {
  // An agent profile (GET /api/clusters/NAME): its tunnel, login, port, token, and the actions on it (start/stop
  // the tunnel, its log, update hpclib, setup_agents), with install/setup output in a dismissible panel.
  // cluster: the profile name; local: this machine's hpclib version (to flag a cluster behind it).
  get name() { return this.getAttribute("cluster") || (this._data && this._data.name); }
  get src() { return `clusters/${enc(this.name)}`; }
  get key() { return `agents/${this.name}`; }
  get logHeading() { return `${this.name}: tunnel log`; }
  get logSrc() { return `clusters/${enc(this.name)}/tunnel/log?lines=200`; }

  get busy() {
    const c = this._data;
    const t = c.tunnel || {};
    return !!(t.prompt || t.state === "starting" || (t.started_here && t.state !== "up") ||
              LOGIN_BUSY.includes((c.login || {}).state) || (c.operation && c.operation.state === "running"));
  }

  makeButtons() {
    const path = (p) => `clusters/${enc(this.name)}/${p}`;
    return {
      start: action("Start", async () => { await fetcher(path("tunnel/start"), { method: "POST", body: {} }); await this.refresh(); }),
      stop: action("Stop", async () => { await fetcher(path("tunnel/stop"), { method: "POST" }); await this.refresh(); }),
      log: action("Log", () => this.toggleLog()),
      install: action("Update hpclib", async () => { await fetcher(path("install"), { method: "POST", body: {} }); await this.refresh(); }),
      setup: action("Set up…", () => {
        if (this.detailKind === "setup") return this.hideDetail();
        ask(this, "hpc-setup", { cluster: this._data, row: this });
      }),
      copyUser: action("Copy username", async () => {
        await navigator.clipboard.writeText(this._data.username || "");
        return "Copied.";
      }, { result: true }),
      login: action("Log in", () => {}),
    };
  }

  opMine(op) { return op.kind === "install" || op.kind === "setup"; }
  updated() { this.syncOperation(this._data.operation); }

  cells() {
    const c = this._data;
    const t = c.tunnel || {};
    const lg = c.login || {};
    const local = this.getAttribute("local");
    const behind = t.hpclib_version && local && t.hpclib_version !== local;
    const loggedIn = lg.state === "connected";
    const running = !!(c.operation && c.operation.state === "running");
    const b = this.buttons;
    b.start.disabled = t.state === "up" || t.state === "starting" || !c.set_up || !loggedIn;
    b.start.title = !c.set_up ? "set it up first" : (loggedIn ? "" : "log in first");
    b.stop.disabled = t.state === "down" && !t.started_here;
    b.setup.setAttribute("label", c.set_up ? "Setup…" : "Set up…");
    for (const x of [b.install, b.setup]) {
      x.disabled = !loggedIn || running;
      x.title = loggedIn ? "" : "log in first";
    }
    return [
      [c.name, el("div", { class: "muted" }, c.mcp_name || "")],
      [el("hpc-state", { state: t.state || "down" }),
       t.state === "up" ? el("div", { class: "muted" }, `${t.hostname || ""}${t.slurm_job_id ? ` · job ${t.slurm_job_id}` : ""}`) : null,
       t.hpclib_version ? el("div", { class: behind ? "state starting" : "muted" },
         `hpclib ${t.hpclib_version}${behind ? ` · ${local} here` : ""}`) : null,
       t.error ? el("div", { class: "muted" }, t.error) : null,
       this.promptLine(c.name, `clusters/${enc(c.name)}/tunnel/answer`, t.prompt)],
      this.loginCells(lg, c),
      c.port,
      !c.set_up ? el("span", { class: "muted" }, "not set up") :
        (c.has_owner_token ? "owner" : el("span", { class: "muted" }, "agent only")),
      [el("div", { class: "actions" }, b.start, b.stop, b.log), el("div", { class: "actions second" }, b.install, b.setup)],
    ];
  }
}

const APP_STATE_LABEL = { up: "running", queued: "queued", starting: "starting", down: "stopped", error: "error" };

export class HpcAppSession extends TunnelRow {
  // A tunnel app's session on one cluster (GET /api/apps/APP/NAME): the login, the session's state, and Open,
  // Start, Stop, Log, Settings. app: the app's id; app-title: its name (JupyterLab); cluster: the profile name.
  // For a tunnel with an install.sh: whether it is installed on the cluster (checked once you're logged in),
  // and Install, whose output shows below the row. A shared app (PAI) says which job serves it.
  get app() { return this.getAttribute("app"); }
  get appTitle() { return this.getAttribute("app-title") || this.app; }
  get name() { return this.getAttribute("cluster") || (this._data && this._data.cluster); }
  get base() { return `apps/${this.app}/${enc(this.name)}`; }
  get src() { return this.base; }
  get key() { return `${this.app}/${this.name}`; }
  get logHeading() { return `${this.appTitle} on ${this.name}: log`; }
  get logSrc() { return `${this.base}/log?lines=300`; }

  get busy() {
    const s = this._data;
    return !!(s.prompt || ["starting", "queued", "working"].includes(s.state) || (s.started_here && s.state !== "up") ||
              LOGIN_BUSY.includes((s.login || {}).state) || (s.operation && s.operation.state === "running"));
  }

  makeButtons() {
    return {
      start: action("Start", async () => { await fetcher(this.base + "/start", { method: "POST" }); await this.refresh(); }, { primary: true }),
      stop: action("Stop", async () => { await fetcher(this.base + "/stop", { method: "POST" }); await this.refresh(); }),
      log: action("Log", () => this.toggleLog()),
      settings: action("Settings…", () => {
        if (this.detailKind === "settings") return this.hideDetail();
        ask(this, "hpc-app-settings", { app: this.app, session: this._data, row: this });
      }),
      install: action("Install", async () => {
        await fetcher(this.base + "/install", { method: "POST", body: { force: this._data.install.state === "installed" } });
        await this.refresh();
      }),
      check: action("Check", async () => { await fetcher(this.base + "/check", { method: "POST" }); await this.refresh(); }),
      copy: action("Copy password", async () => {
        await navigator.clipboard.writeText(this._data.password || "");
        return "Copied.";
      }, { result: true }),
      copyUser: action("Copy username", async () => {
        await navigator.clipboard.writeText(this._data.username || "");
        return "Copied.";
      }, { result: true }),
      login: action("Log in", () => {}),
    };
  }

  opMine(op) { return op.app === this.app; }

  updated() {
    const s = this._data;
    this.syncOperation(s.operation);
    // whether it is installed: asked once you're logged in (it runs install.sh --check on the cluster)
    if (s.install && s.install.state === "unchecked" && (s.login || {}).state === "connected" && !this._checked) {
      this._checked = true;
      this.buttons.check.run();
    }
    // a tool (Data transfer): how it would sign in, asked once you're logged in, after the install check
    if (s.kind === "tool" && !s.auth && (s.login || {}).state === "connected" && !this._authAsked &&
        s.install && s.install.state !== "unchecked") {
      this._authAsked = true;
      this.runControl({ id: "status" }).catch(() => {});
    }
  }

  installLine(s, loggedIn) {
    const inst = s.install || { state: "nothing" };
    const b = this.buttons;
    const busy = !!(s.operation && s.operation.state === "running");
    b.install.disabled = b.check.disabled = !loggedIn || busy;
    b.install.title = b.check.title = loggedIn ? "" : "log in first";
    // once installed it installs again (--force): Reinstall, or what the app calls it (PAI: Update)
    const again = s.reinstall || {};
    b.install.setAttribute("label", inst.state === "installed" ? again.label || "Reinstall" : "Install");
    if (loggedIn && inst.state === "installed" && again.title) b.install.title = again.title;
    b.install.toggleAttribute("primary", inst.state === "missing");
    const text = { installed: "installed", missing: "not installed", unknown: "install status unknown",
                   unchecked: "not checked yet" }[inst.state];
    if (!text) return null;
    return el("div", { class: "install-line" },
      el("hpc-state", { tone: { installed: "up", missing: "starting" }[inst.state] || "down", label: text }),
      inst.message ? el("span", { class: "muted small" }, ` ${inst.message.replace(/^(not )?installed:\s*/, "")}`) : null,
      el("div", { class: "actions" }, inst.state === "installed" ? null : b.install, b.check,
         inst.state === "installed" ? b.install : null));
  }

  // The state's `tools` (toolbar, under the row) and `controls` (under the buttons): kinds the console sends
  // ("info": a line of text; "select": options whose change runs a control), each kept by id so a control's
  // error survives the refreshes
  drawTools(tools) {
    this._tools = this._tools || {};
    const made = (tools || []).map((t) => {
      const tool = this._tools[t.id] || (this._tools[t.id] = el("hpclib-toolbar-tool"));
      tool.setAttribute("label", t.label || "");
      if (t.kind === "select") {
        const pick = tool._select || (tool._select = el("select"));
        const opts = (t.options || []).map((o) => el("option", { value: o.value, selected: o.value === t.value }, o.label));
        pick.replaceChildren(...opts);
        pick.onchange = () => this.runControl({ id: t.control, args: { value: pick.value } })
          .catch((err) => { tool.content = [pick, el("span", { class: "error-box small" }, ` ${err.message}`)]; });
        tool.content = [pick, t.text ? el("span", { class: "muted small" }, ` ${t.text}`) : null].filter(Boolean);
      } else {
        tool.content = el("span", {}, t.text || "");
      }
      return tool;
    });
    this.toolbar.setTools(made);
    this.toolbarRow.hidden = this.toolbar.hidden;
  }

  drawControls(controls) {
    this._controls = this._controls || {};
    const made = (controls || []).map((c) => {
      let wrap = this._controls[c.id];
      if (!wrap) {
        const button = action(c.label, () => this.runControl(this._controls[c.id].spec), { result: true });
        wrap = this._controls[c.id] = el("hpclib-control", {}, button);
        wrap.button = button;
      }
      wrap.spec = c;
      wrap.button.setAttribute("label", c.label);
      wrap.button.title = c.title || "";
      wrap.button.disabled = !!c.disabled;
      wrap.button.toggleAttribute("primary", !!c.primary);
      return wrap;
    });
    this.extras = this.extras || el("hpclib-extra-controls");
    this.extras.setControls(made);
  }

  async runControl(c) {
    if (c.confirm && !(await confirmDialog(c.label, c.confirm, c.label))) return null;
    const out = await fetcher(`${this.base}/control/${enc(c.id)}`, { method: "POST", body: c.args || {} });
    await this.refresh();
    return out.message || "Done.";
  }

  cells() {
    const s = this._data;
    this.drawControls(s.controls);
    this.drawTools(s.tools);
    const lg = s.login || {};
    const loggedIn = lg.state === "connected";
    const missing = s.install && s.install.state === "missing";
    const b = this.buttons;
    b.start.disabled = s.state !== "down" || !loggedIn || missing;
    b.start.title = !loggedIn ? "log in first" : (missing ? "install it first" : "");
    b.stop.disabled = s.state === "down" && !s.started_here;
    const up = s.state === "up";
    const open = up && s.url
      ? el("a", { class: "button primary", href: s.url, target: "_blank", rel: "noopener noreferrer" }, `Open ${this.appTitle}`)
      : null;
    const stateText = up ? `running${s.version ? ` (${s.version})` : ""}`
      : (s.state === "queued" ? `queued: ${s.status}` : (APP_STATE_LABEL[s.state] || s.state));
    const cluster = { name: s.cluster, host: s.host, connection_hours: lg.connection_hours };
    if (s.kind === "tool") {   // commands on the cluster, not a tunnel: no Open/Start/Stop
      return [
        s.cluster,
        this.loginCells(lg, cluster),
        [s.state === "working" ? el("hpc-state", { tone: "starting", label: "working…" }) : null,
         this.promptLine(`${this.appTitle} on ${s.cluster}`, this.base + "/answer", s.prompt),
         this.installLine(s, loggedIn)],
        [el("div", { class: "actions" }, b.log, b.settings), this.extras],
      ];
    }
    return [
      [s.cluster, el("div", { class: "muted" }, `port ${s.port}`)],
      this.loginCells(lg, cluster),
      [el("hpc-state", { state: s.state, label: stateText }),
       up && !s.token_known ? el("div", { class: "muted" }, "token not seen yet; see Log") : null,
       up && "username" in s ? el("div", { class: "muted" }, [`if asked to sign in: user ${s.username} `, b.copyUser]) : null,
       up && "password" in s ? el("div", { class: "muted" }, s.password ? ["password ready ", b.copy]
                                                                      : "password not seen yet; see Log") : null,
       s.error ? el("div", { class: "muted" }, s.error) : null,
       this.promptLine(`${this.appTitle} on ${s.cluster}`, this.base + "/answer", s.prompt),
       this.installLine(s, loggedIn)],
      [el("div", { class: "actions" }, open, b.start, b.stop, b.log, s.kind === "login" ? null : b.settings), this.extras],
    ];
  }

}

customElements.define("hpc-panel", HpcPanel);
customElements.define("hpc-log", HpcLog);
customElements.define("hpc-dialog", HpcDialog);
customElements.define("hpc-state", HpcState);
customElements.define("hpc-field", HpcField);
customElements.define("hpc-action-button", HpcActionButton);
customElements.define("hpc-cluster-picker", HpcClusterPicker);
customElements.define("hpc-env-editor", HpcEnvEditor);
customElements.define("hpc-cluster-row", HpcClusterRow);
customElements.define("hpc-app-session", HpcAppSession);
customElements.define("hpclib-toolbar", HpclibToolbar);
customElements.define("hpclib-toolbar-tool", HpclibToolbarTool);
customElements.define("hpclib-extra-controls", HpclibExtraControls);
customElements.define("hpclib-control", HpclibControl);
