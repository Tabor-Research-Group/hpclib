// The Workspace's shell: what features plug into. It owns the tabs of the work area, the dock (terminals and other
// long-lived views) and the registry; it knows no feature by name, so the file manager, the editor, Submit and the
// rest are each a module that registers itself and can be replaced without touching the others.
//
//   import { ws } from "./shell.js";                (also window.ws, for the console and for tests)
//
// Registry
//   ws.registerOpener({ id, match(entry) -> score, open(path, entry, ctx), restore })
//       what opens a file: the highest score wins (the text editor scores 1 for anything; a viewer 10 for its types).
//       open() builds a view and hands it to ws.tabs.add(); an opener with a negative score is never picked, only
//       asked for by id (Submit, usage). restore: the tab comes back after a reload.
//   ws.registerAction({ id, label, group, when(entry) -> bool, run(entry), bar })
//       a verb on a file or folder ("Submit job…", "Terminal here"): the file manager's ⋯ and right-click menus list
//       the ones whose when() is true; `bar` also puts it on an editor tab's toolbar.
//   ws.registerDock({ id, title, hidden, create(ctx) })   what the dock's "+" offers (a terminal); `hidden` keeps it off
//       the "+" and asks for it only by id: ws.dockOpen(id, ctx) (the follower of a submitted job)
//   An entry is { path, name, dir (bool), type, size, mtime }.
//
// Pages events, on document: the file manager sends ws:cwd {path} (the directory it shows), ws:open {path, entry}
// (a file was opened) and ws:select {paths}; the shell sends ws:reveal {path} (show this directory, or this file's
// directory) and ws:fs-changed {dir} (something wrote there: list it again), ws:tab-path {path} (the tab now shown is this file) and
// ws:job-submitted {id}.
//
// Tabs and the dock use <hpc-tabs>. A tab is { id, key, label, title, view, path, opener, dirty, persist,
// canClose(), onshow(), onresize(), onclose() }; opening the same key (opener + path) again shows the one that is
// there, even while it is still loading.

import { el, remember } from "hpclib-ui/index.js";
import { confirmAsk, basename } from "./client.js";

const GROUPS = ["open", "run", "view", "transfer"];
let counter = 0;

class Tabs {
  /** `bar` is an <hpc-tabs>, `host` holds the panels. */
  constructor(bar, host, { onchange = () => {} } = {}) {
    this.bar = bar;
    this.host = host;
    this.list = [];
    this.active = null;
    this.onchange = onchange;
    bar.addEventListener("tab-select", (e) => this.activate(e.detail.id));
    bar.addEventListener("tab-close", (e) => this.close(e.detail.id));
  }

  get(id) { return this.list.find((t) => t.id === id) || null; }
  byKey(key) { return this.list.find((t) => t.key === key) || null; }

  add(tab) {
    tab.id = tab.id || `t${++counter}`;
    tab.view.id = `panel-${tab.id}`;
    tab.view.setAttribute("role", "tabpanel");
    tab.view.classList.add("view");
    tab.view.hidden = true;
    this.host.append(tab.view);
    this.list.push(tab);
    this.activate(tab.id);
    return tab;
  }

  update(id, patch) {
    const tab = this.get(id);
    if (!tab) return;
    Object.assign(tab, patch);
    this._draw();
    this.onchange();
  }

  activate(id) {
    const tab = this.get(id);
    this.active = tab;
    for (const t of this.list) t.view.hidden = t !== tab;
    this._draw();
    if (tab && tab.onshow) setTimeout(() => tab.onshow(), 0);
    this.onchange();
  }

  /** Close a tab, asking first when it has changes (`force` skips the question: the file is gone). */
  async close(id, force = false) {
    const tab = this.get(id);
    if (!tab) return;
    if (!force && tab.dirty && !await confirmAsk(`${tab.label} has changes that aren't saved. Close it anyway?`, "Close")) return;
    if (tab.onclose) tab.onclose();
    this.list = this.list.filter((t) => t !== tab);
    tab.view.remove();
    if (this.active === tab) this.activate((this.list[this.list.length - 1] || {}).id);
    else { this._draw(); this.onchange(); }
  }

  _draw() {
    this.bar.tabs = this.list.map((t) => ({ id: t.id, label: t.label + (t.suffix || ""), title: t.title || t.path || "",
      dirty: !!t.dirty, closable: true, controls: `panel-${t.id}` }));
    this.bar.active = this.active ? this.active.id : null;
  }
}

export const ws = {
  info: null,
  cwd: null,
  openers: [],
  actions: [],
  docks: [],
  opening: new Map(),

  registerOpener(opener) { this.openers.push(opener); },
  registerAction(action) { this.actions.push(action); },
  registerDock(dock) { this.docks.push(dock); },
  /** Open a dock view by id (null when no feature registered it). */
  dockOpen(id, ctx = {}) {
    const dock = this.docks.find((d) => d.id === id);
    return dock ? dock.create(ctx) : null;
  },

  /** The actions that apply to an entry, in menu order (grouped). */
  actionsFor(entry) {
    return this.actions.filter((a) => !a.when || a.when(entry))
      .sort((a, b) => GROUPS.indexOf(a.group) - GROUPS.indexOf(b.group));
  },

  openerFor(entry) {
    const scored = this.openers.map((o) => [o.match(entry), o]).filter(([score]) => score > 0);
    scored.sort((a, b) => b[0] - a[0]);
    return scored.length ? scored[0][1] : null;
  },

  /** Open a file (or ask a particular opener: ctx.opener is an id). Opening twice shows the first tab. */
  async open(path, entry = {}, ctx = {}) {
    entry = Object.assign({ path, name: basename(path), dir: false }, entry);
    const opener = ctx.opener ? this.openers.find((o) => o.id === ctx.opener) : this.openerFor(entry);
    if (!opener) return null;
    const key = `${opener.id}:${path}`;
    const there = this.tabs.byKey(key);
    if (there) { this.tabs.activate(there.id); if (ctx.line && there.goto) there.goto(ctx.line); return there; }
    if (this.opening.has(key)) return this.opening.get(key);       // a second click while the first is still loading
    const pending = Promise.resolve(opener.open(path, entry, ctx)).then((tab) => {
      if (tab && tab.id) this.tabs.update(tab.id, { key, opener: opener.id, path: tab.path || path });
      return tab;
    }).finally(() => this.opening.delete(key));
    this.opening.set(key, pending);
    return pending;
  },

  /** A directory (or a file's directory) should be shown by the file manager. */
  reveal(path) { document.dispatchEvent(new CustomEvent("ws:reveal", { detail: { path } })); },
  /** Something wrote into `dir`: the file manager lists it again. */
  changed(dir) { document.dispatchEvent(new CustomEvent("ws:fs-changed", { detail: { dir } })); },

  /** Show or hide the dock. */
  showDock(show = true) {
    this.dockPane.hidden = !show;
    this.dockSplit.toggleAttribute("data-collapsed", !show);
    if (show && this.dock.active && this.dock.active.onshow) setTimeout(() => this.dock.active.onshow(), 0);
  },

  /** Wire the page's elements (app.js calls this once the DOM is there). */
  mount({ tabsBar, views, empty, dockBar, dockViews, dockPane, dockSplit }) {
    this.dockPane = dockPane;
    this.dockSplit = dockSplit;
    this.tabs = new Tabs(tabsBar, views, { onchange: () => {
      empty.hidden = this.tabs.list.length > 0;
      this._saveTabs();
      const at = this.tabs.active;
      if (at && at.path && !at.dir) document.dispatchEvent(new CustomEvent("ws:tab-path", { detail: { path: at.path } }));
    } });
    this.dock = new Tabs(dockBar, dockViews, { onchange: () => {
      if (!this.dock.list.length) this.showDock(false);
    } });
    document.addEventListener("ws:cwd", (e) => { this.cwd = e.detail.path; });
    document.addEventListener("ws:open", (e) => this.open(e.detail.path, e.detail.entry || {}));
    document.addEventListener("split-resize", () => {
      for (const t of [this.tabs.active, this.dock.active]) if (t && t.onresize) t.onresize();
    });
    addEventListener("beforeunload", (e) => {
      if (this.tabs.list.some((t) => t.dirty)) { e.preventDefault(); e.returnValue = ""; }
    });
  },

  /** Where this browser keeps what is open: per user and host, so two clusters' Workspaces (same address, other port)
   *  don't open each other's files. The first visit after an upgrade finds the old, unscoped key. */
  layoutKey(name) { return `ws.${name}:${this.info.user}@${this.info.host}`; },

  _saveTabs() {
    if (!this.tabs || this.restoring) return;
    const open = this.tabs.list.filter((t) => t.persist !== false && t.path && t.opener)
      .map((t) => ({ opener: t.opener, path: t.path }));
    const at = this.tabs.active;
    remember.set(this.layoutKey("tabs"), { open, active: at && at.persist !== false ? `${at.opener}:${at.path}` : null });
  },

  /** Open again the tabs this browser had open (the openers that can restore, and only files that are still there). */
  async restoreTabs() {
    const saved = remember.get(this.layoutKey("tabs"), null) || remember.get("ws.tabs", null);
    if (!saved || !Array.isArray(saved.open)) return;
    this.restoring = true;
    try {
      for (const t of saved.open.slice(0, 12)) {
        const opener = this.openers.find((o) => o.id === t.opener);
        if (!opener || !opener.restore || typeof t.path !== "string") continue;
        try { await this.open(t.path, {}, { opener: t.opener, restoring: true }); } catch { /* gone: leave it out */ }
      }
      const at = saved.active && this.tabs.byKey(saved.active);
      if (at) this.tabs.activate(at.id);
    } finally {
      this.restoring = false;
      this._saveTabs();
    }
  },
};

window.ws = ws;

/** A button for the toolbar of a view. */
export const barButton = (label, fn, attrs = {}) => el("button", { type: "button", onclick: fn, ...attrs }, label);
