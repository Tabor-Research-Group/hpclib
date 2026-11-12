// Custom elements for the console's front end: native Web Components, no library and no build step.
// The ones every hpclib page shares (hpc-panel, hpc-log, hpc-dialog, hpc-state, hpc-field, hpc-action-button,
// hpc-env-editor, and hpc-tabs, hpc-split, hpc-menu, hpc-toasts, hpc-code, hpc-terminal) live in hpclib/ui and are
// re-exported here, so the console's pages keep importing them from this file. What stays here needs the console's
// API: cluster pickers, cluster and app rows, the LLM-client connector.
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
//     (by-app: named by the app, for a cluster's page; app-href: its app page; app-page: on that page, no Settings…)
//
//   <hpclib-toolbar>                                      a row of tools under a cluster/tunnel row (an app with
//                                                         `downloads`, like OpenCode, gets its Model Download here)
//     <hpclib-toolbar-tool label="…">…text, a select…</hpclib-toolbar-tool>
//   <hpclib-extra-controls>                               controls under a row's buttons, on the right
//     <hpclib-control>…a button…</hpclib-control>
//   confirmDialog(heading, text, ok): a themed yes/no question, resolving to true or false
//
// Rows ask the page for what only it can do with events that bubble to document: "hpc-login" {cluster, then},
// "hpc-prompt" {label, answer, prompt}, "hpc-setup" {cluster, row} and "hpc-app-settings" {app, session, row}.

import { configure as configureUI, el, action, confirmDialog } from "./hpclib-ui/index.js";

export * from "./hpclib-ui/index.js";

let fetcher = async () => { throw new Error("components.js: call configure({ api }) first"); };

export function configure({ api }) {
  fetcher = api;
  configureUI({ api });
}

const ask = (node, name, detail) => node.dispatchEvent(new CustomEvent(name, { bubbles: true, composed: true, detail }));
const enc = encodeURIComponent;

// ------------------------------------------------------------------ after setup: the LLM clients

// arguments as one line, quoted where a shell would need it (the console splits them back the same way)
export const shellJoin = (args) => (args || []).map((a) => /^[A-Za-z0-9_@%+=:,./-]+$/.test(a) ? a
  : `'${a.replaceAll("'", "'\\''")}'`).join(" ");

// The cluster's MCP entry in each LLM client on this machine (GET clusters/NAME/llm-clients), with Write entry
// (POST llm-clients/ID/write {names}) for each: what Console Settings → LLM clients does, for this one cluster.
// local: also what runs the server here (launch command and arguments) and the agent's local folders, saved at
// once (PUT clusters/NAME/local: nothing to do on the cluster), as on the cluster's Settings tab.
export function connectClients(name, { heading = null, local = false } = {}) {
  const box = el("div", { class: "connect-clients" });
  const draw = async () => {
    let info;
    try { info = await fetcher(`clusters/${enc(name)}/llm-clients`); }
    catch (err) { box.replaceChildren(el("p", { class: "error-box" }, err.message)); return; }
    const rows = (info.clients || []).filter((c) => c.available || c.state === "current").map((c) => {
      const own = c.state === "current" && c.launcher === "own";
      const state = own ? "has it, with its own launcher"
        : { current: "has it", different: "has an older entry", missing: "doesn't have it" }[c.state] || "—";
      const write = action(own ? "Use the console's launcher" : c.state === "current" ? "Write again" : "Write entry", async () => {
        await fetcher(`llm-clients/${c.id}/write`, { method: "POST", body: { names: [info.mcp_name], force: true } });
        await draw();
      }, { primary: c.state !== "current", disabled: !!c.error });
      return el("tr", {},
        el("td", {}, el("strong", {}, c.title), c.windows ? el("span", { class: "muted small" }, " · Windows") : null,
          el("div", { class: "muted small" }, c.path || "")),
        el("td", { class: c.state === "current" ? "ok-text" : "muted" }, c.error || state,
          c.state === "current" && c.after ? el("div", { class: "muted small" }, c.after) : null),
        el("td", { class: "next" }, write));
    });
    // a client's own launcher can become the console's (e.g. /usr/bin/arch -arm64 PYTHON)
    const theirs = (info.clients || []).filter((c) => c.launcher_argv && c.launcher_argv.length);
    const sdk = info.python_has_mcp === false ? el("p", { class: "warning small" }, `${info.python} can't import ` +
      "the MCP SDK, so a client can't start this server with the console's entry. " +
      (theirs.length ? `${theirs.map((c) => c.title).join(" and ")} start${theirs.length === 1 ? "s" : ""} it their own way, ` +
        "which you can use below. " : "") +
      (local ? "Install the SDK for it (pip install mcp), or set another launch command here."
        : "Change the launch command on the cluster's Settings tab.")) : null;
    box.replaceChildren(...[
      heading === "" ? null : el("h4", {}, heading || `Connect your LLM clients to ${info.mcp_name}`),
      local ? localForm(name, info, theirs, draw) : sdk,
      local ? sdk : null,
      rows.length ? el("table", { class: "compact" }, el("tbody", {}, rows))
        : el("p", { class: "muted" }, "No LLM client was found on this machine: copy the entry from the log above, " +
          "or set where a client's configuration is on Console Settings → LLM clients."),
      el("p", { class: "muted small" }, "Console Settings → LLM clients can keep every cluster's entries up to date " +
        "after each setup, new ports and rename."),
    ].filter(Boolean));
  };
  draw();
  return box;
}

function localForm(name, info, theirs, redraw) {
  const l = info.local || { command: "", args: [], local_roots: [] };
  const command = el("input", { size: 30, value: l.command, "aria-label": "MCP launch command",
    placeholder: "found: the installer's Python, else python3 with mcp" });
  const args = el("input", { size: 44, value: shellJoin(l.args), "aria-label": "Launch arguments",
    placeholder: "optional, e.g. -arm64 /path/to/python" });
  const roots = el("textarea", { rows: 2, "aria-label": "Local folders for the agent's files",
    placeholder: "~/Documents/hpclib-agents" }, (l.local_roots || []).join("\n"));
  const put = (body) => fetcher(`clusters/${enc(name)}/local`, { method: "PUT", body });
  const saveBtn = action("Save", async () => {
    await put({ command: command.value.trim(), args: args.value.trim(),
                local_roots: roots.value.split("\n").map((x) => x.trim()).filter(Boolean) });
    await redraw();
    return "";
  }, { primary: true });
  const adopt = theirs.map((c) => action(`Use ${c.title}'s launcher`, async () => {
    await put({ command: c.launcher_argv[0], args: shellJoin(c.launcher_argv.slice(1)) });
    await redraw();
  }));
  return el("div", { class: "local-mcp" },
    el("p", { class: "muted small" }, "On this machine, nothing to set up on the cluster: what your LLM clients run " +
      "for this server, and the folders the agent's push_files and pull_files may use. Saving writes mcp.json " +
      "and the clients kept up to date. ", info.python ? ["Runs: ", el("code", {}, info.python)] : null),
    el("div", { class: "row" },
      el("hpc-field", { label: "MCP launch command", hint: "a Python with the mcp package, or a launcher such as /usr/bin/arch; empty: find a Python" }, command),
      el("hpc-field", { label: "Launch arguments", hint: "before rest_mcp.py, space-separated (quote one with spaces)" }, args)),
    el("div", { class: "row" },
      el("hpc-field", { label: "Local folders for the agent's files", hint: "one per line, created if missing; empty: none" }, roots)),
    el("div", { class: "actions" }, saveBtn, ...adopt));
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
  set content(nodes) { this._body.replaceChildren(...[nodes].flat().filter((x) => x !== null && x !== undefined && x !== false)); }
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


// ------------------------------------------------------------------ rows: <hpc-cluster-row>, <hpc-app-session>

export const LOGIN_BUSY = ["starting", "password_sent", "push_sent", "code_required"];
export const LOGIN_LABEL = { connected: "logged in", none: "not logged in", expired: "login ended",
                             failed: "login failed", starting: "connecting", password_sent: "password sent",
                             push_sent: "approve the push", code_required: "enter MFA code" };

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
    const panel = el("hpc-panel", { heading: "hpclib", dismissible: true, class: "log-panel" }, log);
    log.addEventListener("update", ({ detail: op }) => {
      panel.setAttribute("heading", op.title || op.kind);
      panel.setAttribute("status", { running: "running…", succeeded: "finished",
                                     failed: `failed (exit ${op.exit_code})` }[op.state] || op.state);
      panel.setAttribute("tone", { succeeded: "ok", failed: "error" }[op.state] || "");
      if (op.state !== "running") { log.stop(); this.refresh(); }
      // setup done: write the cluster's MCP entry into the LLM clients from here, as Console Settings does
      if (op.kind === "setup" && op.state === "succeeded" && !panel.querySelector(".connect-clients")) {
        panel.append(connectClients(this.name));
      }
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
    this.showDetail("log", el("hpc-panel", { heading: this.logHeading, dismissible: true, class: "log-panel" },
      el("hpc-log", { src: this.logSrc, every: 3, empty: "(no log yet)" })));
    TunnelRow.openLogs.add(this.key);
  }

  // shared cells
  loginCells(lg, cluster) {
    const b = this.buttons.login;
    const connected = lg.state === "connected";
    b.setAttribute("label", connected ? "Log out" : lg.state === "code_required" ? "Enter code" : "Log in");
    b.disabled = LOGIN_BUSY.includes(lg.state) && lg.state !== "code_required";
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

// Running code and installed files can differ until the agent tunnel is restarted. Older servers only
// report the running version, so use that as the best available installation information.
export function hpclibVersionInfo(t = {}, local = null) {
  t = t || {};
  const running = t.hpclib_version;
  const installed = t.hpclib_version_on_disk || running;
  const restart = !!(running && installed && running !== installed);
  const label = running ? `hpclib ${running}${restart ? ` running · ${installed} installed · restart agent tunnel` : ""}` : "";
  return { running, installed, restart, label: label + (installed && local && installed !== local ? ` · ${local} here` : "") };
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
      ssh: action("Set up SSH", async () => {
        await fetcher(path("ssh/setup"), { method: "POST", body: {} });
        await this.refresh();
      }),
      copyUser: action("Copy username", async () => {
        await navigator.clipboard.writeText(this._data.username || "");
        return "Copied.";
      }, { result: true }),
      login: action("Log in", () => {}),
    };
  }

  opMine(op) { return ["install", "setup", "ssh_setup"].includes(op.kind); }
  updated() { this.syncOperation(this._data.operation); }

  cells() {
    const c = this._data;
    const t = c.tunnel || {};
    const lg = c.login || {};
    const local = this.getAttribute("local");
    const version = hpclibVersionInfo(t, local);
    const behind = version.installed && local && version.installed !== local;
    const loggedIn = lg.state === "connected";
    const running = !!(c.operation && c.operation.state === "running");
    const b = this.buttons;
    b.start.disabled = t.state === "up" || t.state === "starting" || !c.set_up || !loggedIn;
    b.start.title = !c.set_up ? "set it up first" : (loggedIn ? "" : "log in first");
    b.stop.disabled = t.state === "down" && !t.started_here;
    b.setup.setAttribute("label", c.set_up ? "Setup…" : "Set up…");
    for (const x of [b.install, b.setup, b.ssh]) {
      x.disabled = !loggedIn || running;
      x.title = loggedIn ? "" : "log in first";
    }
    if (c.preset?.ssh_keys === "site-managed") {
      b.ssh.disabled = true;
      b.ssh.title = "SSH keys are managed by the cluster site";
    }
    return [
      [c.preset?.title || c.name, c.preset ? el("div", { class: "muted small" }, c.name) : null,
       el("div", { class: "muted" }, c.mcp_name || "")],
      [el("hpc-state", { state: t.state || "down" }),
       t.state === "up" ? el("div", { class: "muted" }, `${t.hostname || ""}${t.slurm_job_id ? ` · job ${t.slurm_job_id}` : ""}`) : null,
       version.running ? el("div", { class: behind || version.restart ? "state starting" : "muted" }, version.label) : null,
       t.error ? el("div", { class: "muted" }, t.error) : null,
       this.promptLine(c.name, `clusters/${enc(c.name)}/tunnel/answer`, t.prompt)],
      this.loginCells(lg, c),
      c.port,
      !c.set_up ? el("span", { class: "muted" }, "not set up") :
        (c.tunnel?.owner_access === "rejected" ? el("span", { class: "muted" }, "agent only · owner rejected")
          : c.has_owner_token ? "owner" : el("span", { class: "muted" }, "agent only · owner missing")),
      [el("div", { class: "actions" }, b.start, b.stop, b.log), el("div", { class: "actions second" }, b.install, b.setup, b.ssh)],
    ];
  }
}

// Owner recovery is separate from setup/rebuild; it preserves the agent token and server settings.
export function ownerRecovery(cluster, started = async () => {}) {
  return action("Recover owner access", async () => {
    if (!await confirmDialog("Recover owner access?",
      "A matching owner credential will be reused. Otherwise a replacement is created for this console. " +
      "Other consoles using the previous owner credential will need the replacement. Agent credentials, " +
      "templates, server settings and databases are preserved. Restart the agent tunnel after recovery.",
      "Recover owner access")) return;
    await fetcher(`clusters/${encodeURIComponent(cluster.name)}/setup`,
      { method: "POST", body: { repair_owner_token: true } });
    await started();
  });
}

/** 19.5 GB: a size in bytes for people (decimal units, as Ollama and Hugging Face give them). */
/** The address an app's Open should use: for an app that follows the console's theme (its `theme_link`), the console's
 *  theme and address are added after a #, which no server sees. */
// link: {file, line} for an app that can open a file (the Workspace: #file=/path&line=N), from a console link
// (#/c/NAME/apps/workspace?file=/path&line=N)
export function appUrl(url, state = {}, link = null) {
  if (!state.theme_link || !url || url.includes("#")) return url;
  const theme = document.documentElement.dataset.theme || "system";
  const params = new URLSearchParams({ theme, console: location.origin });
  if (link && link.file) { params.set("file", link.file); if (link.line) params.set("line", link.line); }
  return `${url}#${params}`;
}

export function formatBytes(n) {
  if (typeof n !== "number") return "?";
  const units = ["B", "kB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i += 1; }
  return `${i ? (n < 100 ? n.toFixed(1).replace(/\.0$/, "") : Math.round(n)) : n} ${units[i]}`;
}

const APP_STATE_LABEL = { up: "running", queued: "queued", starting: "starting", down: "stopped", error: "error" };

export class HpcAppSession extends TunnelRow {
  // A tunnel app's session on one cluster (GET /api/apps/APP/NAME): the login, the session's state, and Open,
  // Start, Stop, Log, Settings. app: the app's id; app-title: its name (JupyterLab); cluster: the profile name.
  // For a tunnel with an install.sh: whether it is installed on the cluster (checked once you're logged in): the
  // row says "not installed" until it is; what the cluster says, Check and Install (whose output shows below the
  // row) are in the settings panel (installBox). A shared app (PAI) says which job serves it.
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
    return !!(s.prompt || s.signing_in || ["starting", "queued"].includes(s.state) || (s.started_here && s.state !== "up") ||
              LOGIN_BUSY.includes((s.login || {}).state) || (s.operation && s.operation.state === "running"));
  }

  makeButtons() {
    return {
      start: action("Start", async () => { await fetcher(this.base + "/start", { method: "POST" }); await this.refresh(); }, { primary: true }),
      open: action(`Open ${this.appTitle}`, () => this.openApp(), { primary: true }),
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
      check: action("Check", () => this.check()),
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

  async openApp() {
    // Reserve the tab during the click, before awaiting the API, so popup blockers allow it.
    const tab = window.open("about:blank", "_blank");
    if (!tab) throw new Error("Allow pop-ups for this console, then Open again.");
    tab.opener = null;
    try {
      const out = await fetcher(this.base + "/open", { method: "POST" });
      tab.location.replace(appUrl(out.url, out, this.link));
    } catch (err) {
      tab.close();
      throw err;
    }
  }

  updated() {
    const s = this._data;
    this.syncOperation(s.operation);
    // whether it is installed: asked once you're logged in (it runs install.sh --check on the cluster)
    if (s.install && s.install.state === "unchecked" && (s.login || {}).state === "connected" && !this._checked) {
      this._checked = true;
      this.check().catch(() => {});      // its error shows in the row: the Check button is in the settings panel
    }
    // signing in to a server beside the session (Data transfer's SMB server): how it would, asked once you're
    // logged in, after the install check
    if ("auth" in s && !s.auth && (s.login || {}).state === "connected" && !this._authAsked &&
        s.install && s.install.state !== "unchecked") {
      this._authAsked = true;
      this.runControl({ id: "status" }).catch(() => {});
    }
  }

  // Whether the app is installed, asked of the cluster; the row shows only the error if that failed
  async check() {
    try {
      await fetcher(this.base + "/check", { method: "POST" });
      this._checkError = null;
    } catch (err) {
      this._checkError = err.message;
      this._draw();
      throw err;
    }
    await this.refresh();
  }

  // What the cluster says about the install, with Check and Install: drawn with the row, shown in its settings panel
  get installBox() { return this._installBox || (this._installBox = el("div", { class: "install-box" })); }

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
      inst.message ? el("p", { class: "install-message" }, inst.message.replace(/^(not )?installed:\s*/, "")) : null,
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
    const download = this.downloadTool(this._data);
    this.toolbar.setTools(download ? [...made, download] : made);
    this.toolbarRow.hidden = this.toolbar.hidden;
  }

  // An app's `downloads` (OpenCode's models): kind, name and Download, which runs on the cluster's login node as an
  // operation with its log, like Install; and what is there already, in a panel under the row, with Remove
  downloadTool(s) {
    const spec = s.downloads;
    if (!spec) return null;
    let t = this._download;
    if (!t) {
      t = this._download = { tool: el("hpclib-toolbar-tool", { class: "download-tool" }) };
      t.kind = el("select", { "aria-label": "Kind" },
        spec.kinds.map((k) => el("option", { value: k.id }, k.label)));
      t.kind.hidden = spec.kinds.length < 2;
      t.name = el("input", { size: 34, autocomplete: "off", spellcheck: "false", "aria-label": `${spec.label}: name` });
      const placeholder = () => {
        const k = spec.kinds.find((x) => x.id === t.kind.value) || spec.kinds[0];
        t.name.placeholder = k.placeholder ? `e.g. ${k.placeholder}` : `${k.label} name`;
      };
      t.kind.addEventListener("change", placeholder);
      placeholder();
      t.go = action("Download", async () => {
        const name = t.name.value.trim();
        if (!name) throw new Error("type a name first");
        await fetcher(this.base + "/downloads", { method: "POST", body: { kind: t.kind.value, name } });
        t.name.value = "";
        await this.refresh();
        return `downloading ${name}: progress below`;
      }, { result: true, primary: true });
      t.name.addEventListener("keydown", (e) => { if (e.key === "Enter" && !t.go.disabled) { e.preventDefault(); t.go.run(); } });
      t.list = action("Downloaded…", () => this.toggleDownloads());
      t.tool.content = [el("div", { class: "download-form" }, t.kind, t.name, t.go, t.list),
                        spec.description ? el("div", { class: "muted small" }, spec.description) : null];
    }
    t.tool.setAttribute("label", spec.label);
    const loggedIn = (s.login || {}).state === "connected";
    const busy = !!(s.operation && s.operation.state === "running");
    t.go.disabled = !loggedIn || busy;
    t.go.title = !loggedIn ? "log in first" : (busy ? "wait for what is running to finish" : "");
    t.list.disabled = !loggedIn;
    t.list.title = loggedIn ? "" : "log in first";
    return t.tool;
  }

  toggleDownloads() {
    if (this.detailKind === "downloads") return this.hideDetail();
    return this.showDownloads();
  }

  async showDownloads() {
    const spec = this._data.downloads;
    const kinds = Object.fromEntries(spec.kinds.map((k) => [k.id, k.label]));
    const body = el("div", { class: "muted" }, "Asking the cluster…");
    const panel = el("hpc-panel", { heading: `${spec.label} on ${this.name}`, dismissible: true, class: "downloads-panel" },
      body);
    panel.append(action("Refresh", () => this.showDownloads(), { slot: "actions" }));
    this.showDetail("downloads", panel);
    let out;
    try {
      out = await fetcher(this.base + "/downloads");
    } catch (err) {
      body.replaceChildren(el("div", { class: "error-box small" }, err.message));
      return;
    }
    const rows = out.items.map((item) => el("tr", {},
      el("td", {}, kinds[item.kind] || item.kind),
      el("td", {}, el("code", {}, item.name)),
      el("td", { class: "num" }, formatBytes(item.size)),
      el("td", {}, item.in_use ? el("hpc-state", { tone: "up", label: "in use" }) : null,
         item.note ? el("div", { class: "muted small" }, item.note) : null),
      el("td", {}, action("Remove", async () => {
        if (!await confirmDialog(`Remove ${item.name}?`, `Deletes ${kinds[item.kind] || item.kind} ${item.name} ` +
            `(${formatBytes(item.size)}) from ${this.name}. Download it again to use it.`, "Remove")) return null;
        await fetcher(this.base + "/downloads/remove", { method: "POST", body: { kind: item.kind, name: item.name } });
        await this.showDownloads();
        return null;
      }, { result: true }))));
    body.className = "";
    body.replaceChildren(...[      // (a null would show as "null")
      out.note ? el("p", { class: "muted" }, out.note) : null,
      rows.length ? el("table", { class: "rows downloads" },
        el("thead", {}, el("tr", {}, ["Kind", "Name", "Size", "", ""].map((h) => el("th", {}, h)))),
        el("tbody", {}, rows))
        : el("p", { class: "muted" }, "Nothing downloaded yet.")].filter(Boolean));
  }

  drawControls(controls) {
    this._controls = this._controls || {};
    const made = (controls || []).map((c) => {
      const key = c.key || c.id;
      let wrap = this._controls[key];
      if (!wrap) {
        const button = action(c.label, () => this.runControl(this._controls[key].spec), { result: true });
        wrap = this._controls[key] = el("hpclib-control", {}, button);
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
      ? (s.prepare_open ? b.open : el("a", { class: "button primary", href: appUrl(s.url, s, this.link), target: "_blank", rel: "noopener noreferrer" },
                                     `Open ${this.appTitle}`))
      : null;
    const notInstalled = missing && s.state === "down";       // nothing to start until Install (in Settings…)
    const stateText = notInstalled ? "not installed" : up ? `running${s.version ? ` (${s.version})` : ""}`
      : (s.state === "queued" ? `queued: ${s.status}` : (APP_STATE_LABEL[s.state] || s.state));
    const line = this.installLine(s, loggedIn);
    this.installBox.replaceChildren(...(line ? [line] : []));
    const cluster = { name: s.cluster, host: s.host, connection_hours: lg.connection_hours };
    // by-app: a row of one cluster's page (its Apps tab, or the app's page), named by the app; the login is on that
    // page's header
    const byApp = this.hasAttribute("by-app");
    if (byApp) { b.start.hidden = b.start.disabled && !missing; b.stop.hidden = b.stop.disabled; }
    // app-page: the row of the app's own page, whose settings are drawn on the page; app-href: that page
    b.settings.hidden = this.hasAttribute("app-page");
    const link = this.getAttribute("app-href");
    const title = link ? el("a", { href: link, class: "cluster-link" }, this.appTitle) : el("strong", {}, this.appTitle);
    return [
      // viewControls (a page's): e.g. the Apps tab's Pin / Hide, drawn again with the row
      [byApp ? title : s.cluster, el("div", { class: "muted" }, `port ${s.port}`),
       typeof this.viewControls === "function" ? this.viewControls(s) : null],
      ...(byApp ? [] : [this.loginCells(lg, cluster)]),
      [el("hpc-state", { state: s.state, label: stateText, ...(notInstalled ? { tone: "starting" } : {}) }),
       up && !s.token_known ? el("div", { class: "muted" }, "token not seen yet; see Log") : null,
       up && "username" in s ? el("div", { class: "muted" }, [`if asked to sign in: user ${s.username} `, b.copyUser]) : null,
       up && "password" in s ? el("div", { class: "muted" }, s.password ? ["password ready ", b.copy]
                                                                      : "password not seen yet; see Log") : null,
       s.error ? el("div", { class: "muted" }, s.error) : null,
       this._checkError ? el("div", { class: "error-box small" }, `install check failed: ${this._checkError}`) : null,
       s.signing_in ? el("hpc-state", { tone: "starting", label: "signing in…" }) : null,
       this.promptLine(`${this.appTitle} on ${s.cluster}`, this.base + "/answer", s.prompt)],
      [el("div", { class: "actions" }, open, b.start, b.stop, b.log, b.settings), this.extras],
    ];
  }

}

customElements.define("hpc-cluster-picker", HpcClusterPicker);
customElements.define("hpc-cluster-row", HpcClusterRow);
customElements.define("hpc-app-session", HpcAppSession);
customElements.define("hpclib-toolbar", HpclibToolbar);
customElements.define("hpclib-toolbar-tool", HpclibToolbarTool);
customElements.define("hpclib-extra-controls", HpclibExtraControls);
customElements.define("hpclib-control", HpclibControl);
