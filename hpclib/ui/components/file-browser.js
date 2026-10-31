import { el, remember } from "../core.js";
import { loadLib } from "../libs.js";

// ------------------------------------------------------------------ <hpc-file-browser>
//
// A folder tree and a details table over one listing function, for the console's Files page and the Workspace's file
// list. The tree is Wunderbaum (libs.json pins it) and falls back to a plain <details> tree when it can't load; the
// details view is a sortable table. Folders load one level at a time, and nothing here walks the filesystem: every
// listing comes from the page's own authenticated callback.
//
//   const browser = el("hpc-file-browser");
//   browser.configure({
//     list(path) -> Promise<{path, entries: [{name, type, size, mtime, target?, target_type?}], truncated?, hidden?}>,
//     size(bytes), when(mtime)          formatters for the details view
//     onOpen(entry)                     a file was chosen (entries carry .path)
//     onDirectory(listing)              the directory shown changed (address bar, hash)
//     onMenu(entry, event)              a right-click or the row's ⋯ (omit: no menu, no ⋯ column)
//     onError(error)                    a listing failed (the status line shows it as well)
//     boundary                          the topmost folder the breadcrumb offers (the console's base)
//     storageKey                        where this page remembers Tree or Details
//     refresh: false                    leave the Refresh button off (the page has its own)
//   });
//   await browser.navigate(path, { refresh });     browser.select(path);     browser.invalidate();   // after the listing's rules change
//   browser.run(() => browser.navigate(path))      like navigate, but a failure goes to onError instead of throwing
//
// Races are settled here: a slow folder can't replace a newer one, requests for one folder share a promise, and
// invalidate() drops listings still in flight (the Workspace's "Hidden files" box changes what a listing is).

const join = (p, n) => `${p === "/" ? "" : p}/${n}`;
const parent = (p) => p.replace(/\/[^/]+\/?$/, "") || "/";
const directory = (e) => e.type === "directory" || (e.type === "link" && e.target_type === "directory");
const label = (e) => `${e.type === "link" ? "↪ " : ""}${e.name}${directory(e) ? "/" : ""}`;
const tooltip = (e) => e.path + (e.target ? ` → ${e.target}` : "");
const inside = (p, root) => p === root || p.startsWith(root === "/" ? "/" : `${root}/`);
/**
 * Wildcard patterns, the shell way: * (anything), ? (one character), [abc] / [a-z] / [!abc]. Several patterns separated by
 * spaces or commas match if any does; case is ignored. A pattern with no wildcard matches names containing it.
 * Returns a name => boolean function, or null for an empty filter.
 */
export function globs(text) {
  const parts = String(text || "").split(/[\s,]+/).filter(Boolean);
  if (!parts.length) return null;
  const regexes = parts.map((part) => {
    const wild = /[*?[]/.test(part);
    let out = "";
    for (let i = 0; i < part.length; i++) {
      const c = part[i];
      if (c === "*") out += ".*";
      else if (c === "?") out += ".";
      else if (c === "[" && part.indexOf("]", i + 2) > 0) {
        const end = part.indexOf("]", i + 2);
        const body = part.slice(i + 1, end).replace(/\\/g, "\\\\");
        out += `[${body[0] === "!" ? "^" + body.slice(1) : body}]`;
        i = end;
      } else out += c.replace(/[.*+?^${}()|[\]\\\/]/g, "\\$&");
    }
    return new RegExp(wild ? `^${out}$` : out, "i");
  });
  return (name) => regexes.some((re) => re.test(name));
}

export class HpcFileBrowser extends HTMLElement {
  constructor() {
    super();
    this.cache = new Map();
    this.pending = new Map();
    this.expanded = new Set();
    this.sequence = 0;
    this.scope = 0;
    this.mode = "tree";
    this.sort = "name";
    this.descending = false;
  }
  configure(options) {
    this.options = options;
    this.mode = remember.get(options.storageKey || "hpclib-files-mode") === "details" ? "details" : "tree";
    this.controls = el("div", { class: "file-controls", role: "group", "aria-label": "File view" });
    this.buttons = Object.fromEntries(["tree", "details"].map((mode) => {
      const button = el("button", { type: "button", onclick: () => this.setMode(mode) }, mode === "tree" ? "Tree" : "Details");
      this.controls.append(button);
      return [mode, button];
    }));
    if (options.refresh !== false) this.controls.append(el("button", { type: "button", onclick: () => this.run(() => this.navigate(this.listing.path, { refresh: true })) }, "Refresh"));
    this.crumbs = el("nav", { class: "file-crumbs", "aria-label": "Folder path" });
    this.viewport = el("div", { class: "file-viewport" });
    this.status = el("div", { class: "file-status", role: "status" });
    this.replaceChildren(this.controls, this.crumbs, this.viewport, this.status);
    loadLib("wunderbaum").then(({ mar10 }) => {
      this.Tree = mar10.Wunderbaum;
      if (this.isConnected && this.mode === "tree") this.draw();
    }).catch(() => { this.fallback = true; this.updateStatus(); });
    return this;
  }
  connectedCallback() { if (this.listing) this.draw(); }
  disconnectedCallback() { this.sequence++; this.tree?.destroy(); this.tree = null; }
  setMode(mode) {
    this.mode = mode;
    remember.set(this.options.storageKey || "hpclib-files-mode", mode);
    this.draw();
  }
  invalidate() { this.scope++; this.cache.clear(); this.pending.clear(); this.alive = new Map(); }
  async load(path, refresh = false) {
    if (!refresh && this.cache.has(path)) return this.cache.get(path);
    if (!refresh && this.pending.has(path)) return this.pending.get(path);
    const scope = this.scope;
    const request = Promise.resolve().then(() => this.options.list(path)).then((listing) => {
      const normalized = { ...listing, entries: listing.entries.map((e) => ({ ...e, path: e.path || join(listing.path, e.name) })) };
      if (scope === this.scope) { this.cache.set(path, normalized); this.cache.set(listing.path, normalized); }
      return normalized;
    }).finally(() => { if (this.pending.get(path) === request) this.pending.delete(path); });
    this.pending.set(path, request);
    return request;
  }
  async navigate(path, { refresh = false } = {}) {
    const sequence = ++this.sequence;
    const listing = await this.load(path, refresh);
    if (sequence !== this.sequence) return;
    if (!this.root || !inside(listing.path, this.root)) this.root = listing.path;
    const keepSelection = refresh && this.listing?.path === listing.path &&
      listing.entries.some((entry) => entry.path === this.selected);
    this.listing = listing;
    if (!keepSelection) this.selected = listing.path;
    for (let at = listing.path; inside(at, this.root); at = parent(at)) {
      this.expanded.add(at);
      if (at === this.root || at === "/") break;
    }
    this.options.onDirectory?.(listing);
    this.draw();
    return listing;
  }
  async run(fn) {
    try { await fn(); }
    catch (error) { this.status.textContent = error.message; this.options.onError?.(error); }
  }
  /**
   * Show only what matches the patterns: files by name, and a folder when it holds a match at any depth (or its own name
   * matches and it isn't empty). A folder that has nothing to show is left out, once the tree has looked inside it, which
   * it does in the background a few folders at a time, so a folder can disappear a moment after the filter is typed.
   * "" shows everything.
   */
  setFilter(text) {
    this.filterText = String(text || "").trim();
    this.matcher = globs(this.filterText);
    this.alive = new Map();
    if (this.listing) { this.draw(); this.updateStatus(); this.settle(); }
  }
  shown(entries) {
    return this.matcher ? entries.filter((e) => (directory(e) ? this.alive.get(e.path) !== false : this.matcher(e.name))) : entries;
  }
  /** Look inside the folders on show (and the loaded ones below them), and drop those with nothing that matches. */
  async settle() {
    if (!this.matcher || !this.listing) return;
    const token = this.settling = (this.settling || 0) + 1, matcher = this.matcher, alive = this.alive;
    let budget = 400;                                          // folders looked into per pass; beyond it a folder stays
    const survives = async (entry) => {
      if (!directory(entry)) return matcher(entry.name);
      if (alive.has(entry.path)) return alive.get(entry.path);
      if (budget-- <= 0) return true;
      let listing;
      try { listing = await this.load(entry.path); } catch (error) { return true; }
      if (token !== this.settling) return true;
      let ok = false;
      if (listing.entries.length) {
        ok = matcher(entry.name);
        for (const child of listing.entries) { if (ok) break; ok = await survives(child); }
      }
      if (token === this.settling) alive.set(entry.path, ok);
      return ok;
    };
    const shown = [this.listing, ...[...this.expanded].map((path) => this.cache.get(path))].filter(Boolean);
    const queue = [...new Set(shown.flatMap((listing) => listing.entries.filter(directory)))];
    let changed = false;
    const worker = async () => {
      for (let entry = queue.shift(); entry && token === this.settling; entry = queue.shift()) {
        if (!(await survives(entry)) && token === this.settling) { changed = true; clearTimeout(this.pruneTimer); this.pruneTimer = setTimeout(() => token === this.settling && this.prune(), 120); }
      }
    };
    await Promise.all([worker(), worker(), worker(), worker()]);
    if (changed && token === this.settling) { clearTimeout(this.pruneTimer); this.prune(); }
  }
  prune() {
    if (this.tree && this.mode === "tree") {
      for (const [path, ok] of this.alive) if (!ok) this.tree.findFirst((node) => node.key === path)?.remove();
      this.updateStatus();
    } else this.draw();
  }
  sorted(entries) {
    return this.shown(entries).sort((a, b) => Number(directory(b)) - Number(directory(a)) ||
      (this.descending ? -1 : 1) * (this.sort === "name"
        ? a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" })
        : (a[this.sort] || 0) - (b[this.sort] || 0)) || a.name.localeCompare(b.name));
  }
  updateStatus() {
    if (!this.listing || !this.status) return;
    this.status.textContent = [
      this.matcher ? `${this.shown(this.listing.entries).length} of ${this.listing.entries.length} items match "${this.filterText}"` : `${this.listing.entries.length} items`,
      this.listing.truncated ? "Listing truncated by server" : "",
      this.listing.hidden ? `${this.listing.hidden} hidden` : "",
      this.fallback && this.mode === "tree" ? "Basic tree (CDN unavailable)" : "",
    ].filter(Boolean).join(" · ");
  }
  select(path) {
    this.selected = path;
    for (const row of this.viewport.querySelectorAll("[data-path]")) row.classList.toggle("selected", row.dataset.path === path);
    this.activateQuietly(path);
  }
  /** Mark a node active without the tree's activate event acting on it (Wunderbaum fires it later, not inside setActive). */
  activateQuietly(path) {
    const node = this.tree?.findKey(path);
    if (!node || node.isActive()) return;
    this.programmatic = path;
    node.setActive(true);
    setTimeout(() => { if (this.programmatic === path) this.programmatic = null; }, 1000);   // it never fired: don't swallow a real click
  }
  open(entry) {
    if (!directory(entry)) this.sequence++;
    this.select(entry.path);
    if (directory(entry)) return this.run(() => this.navigate(entry.path));
    const listing = this.cache.get(parent(entry.path));
    if (listing && listing.path !== this.listing.path) {
      this.listing = listing;
      this.drawCrumbs(); this.options.onDirectory?.(listing); this.updateStatus();
    }
    return this.run(() => this.options.onOpen(entry));
  }
  menu(entry, event) {
    if (!this.options.onMenu) return;
    event.preventDefault();
    event.stopPropagation();
    this.select(entry.path);
    this.options.onMenu(entry, event);
  }
  draw() {
    if (!this.listing) return;
    const scroll = this.viewport.scrollTop;
    this.tree?.destroy();
    this.tree = null;
    this.viewport.replaceChildren();
    for (const [mode, button] of Object.entries(this.buttons)) button.setAttribute("aria-pressed", String(mode === this.mode));
    this.drawCrumbs();
    if (this.mode === "details") this.drawDetails();
    else if (this.Tree && this.isConnected) this.drawWunderbaum();
    else this.drawBasic();
    this.viewport.scrollTop = scroll;
    this.select(this.selected);
    this.updateStatus();
  }
  drawCrumbs() {
    const bound = this.options.boundary;
    const paths = [];
    for (let at = this.listing.path; !bound || inside(at, bound); at = parent(at)) {
      paths.unshift(at);
      if (at === "/" || at === bound) break;
    }
    this.crumbs.replaceChildren(...paths.map((path) => el("button", {
      type: "button", title: path, onclick: () => this.run(() => this.navigate(path)),
    }, path === "/" ? "/" : path.split("/").pop())));
  }
  drawDetails() {
    const head = el("tr");
    for (const [key, label] of [["name", "Name"], ["size", "Size"], ["mtime", "Modified"]]) {
      const th = el("th", { "aria-sort": this.sort === key ? (this.descending ? "descending" : "ascending") : "none" });
      th.append(el("button", { type: "button", onclick: () => {
        this.descending = this.sort === key ? !this.descending : false; this.sort = key; this.draw();
      } }, label));
      head.append(th);
    }
    head.append(el("th", {}, ""));
    const body = el("tbody");
    for (const entry of this.sorted(this.listing.entries)) {
      const name = el("button", { type: "button", class: "file-name", title: tooltip(entry) }, `${directory(entry) ? "▸ " : ""}${label(entry)}`);
      const row = el("tr", { "data-path": entry.path, onclick: () => this.open(entry), oncontextmenu: (e) => this.menu(entry, e) },
        el("td", {}, name), el("td", { class: "num" }, directory(entry) ? "" : this.options.size(entry.size)),
        el("td", { class: "num" }, this.options.when(entry.mtime)));
      const actions = el("td");
      if (this.options.onMenu) actions.append(el("button", { type: "button", "aria-label": `More for ${entry.name}`, onclick: (e) => this.menu(entry, e) }, "⋯"));
      row.append(actions); body.append(row);
    }
    this.viewport.append(el("table", {}, el("thead", {}, head), body));
    if (!this.listing.entries.length) this.viewport.append(el("p", {}, "Empty directory."));
  }
  node(entry) {
    const listing = this.cache.get(entry.path);
    return { title: entry.type === "link" ? `↪ ${entry.name}` : entry.name, key: entry.path, lazy: directory(entry), entry,
      tooltip: tooltip(entry), expanded: this.expanded.has(entry.path),
      ...(listing ? { children: this.sorted(listing.entries).map((e) => this.node(e)) } : {}) };
  }
  rootEntry() { return { name: this.root === "/" ? "/" : this.root.split("/").pop(), path: this.root, type: "directory" }; }
  drawWunderbaum() {
    const host = el("div", { class: "file-wunderbaum" });
    this.viewport.append(host);
    // Inline glyphs and drawings avoid an icon-font dependency. Each has a box of fixed width (base.css), so the
    // names of a folder's files line up under the folder's own name, with one step of indent between the levels.
    // Filenames always remain escaped text.
    // the tree finds these again by tag and class (i.wb-expander / i.wb-icon) to redraw them, so keep both
    const mark = (kind, inner) => `<i class="wb-${kind === "expander" ? "expander" : "icon"} file-${kind}" aria-hidden="true">${inner}</i>`;
    const draw = (path) => `<svg viewBox="0 0 16 16" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round">${path}</svg>`;
    const FOLDER = draw('<path d="M1.6 4.2c0-.7.5-1.2 1.2-1.2h3l1.4 1.5h6c.7 0 1.2.5 1.2 1.2v6.3c0 .7-.5 1.2-1.2 1.2H2.8c-.7 0-1.2-.5-1.2-1.2z"/>');
    const FOLDER_OPEN = draw('<path d="M1.6 12.5V4.2c0-.7.5-1.2 1.2-1.2h3l1.4 1.5h5.8c.7 0 1.2.5 1.2 1.2v1.3M1.6 12.5l1.7-5.2c.1-.4.5-.6.9-.6h10.2c.6 0 1 .6.8 1.1l-1.5 4.6c-.1.4-.5.6-.9.6H2.8"/>');
    const DOC = draw('<path d="M3.5 1.8h5.3l3.7 3.7v8.7h-9z"/><path d="M8.8 1.8v3.7h3.7"/>');
    const iconMap = { ...this.Tree.iconMaps.bootstrap,
      expanderExpanded: mark("expander", "▾"), expanderCollapsed: mark("expander", "▸"), expanderLazy: mark("expander", "▸"),
      folder: mark("icon", FOLDER), folderOpen: mark("icon", FOLDER_OPEN), folderLazy: mark("icon", FOLDER), doc: mark("icon", DOC),
      loading: mark("expander", "…"), error: mark("expander", "!"), noData: mark("icon", DOC) };
    const tree = new this.Tree({ element: host, source: [this.node(this.rootEntry())], iconMap,
      lazyLoad: (e) => this.load(e.node.key).then((listing) => {
        const kids = this.sorted(listing.entries).map((entry) => this.node(entry));
        if (this.matcher) setTimeout(() => this.settle(), 0);       // new folders to look into
        return kids;
      }),
      expand: (e) => { if (e.flag) this.expanded.add(e.node.key); else this.expanded.delete(e.node.key); },
      click: (e) => {
        const region = e.info?.region;
        if (region !== "title" && region !== "icon") return;
        const entry = e.node.data.entry;
        if (!directory(entry)) { if (e.node.isActive()) this.open(entry); }   // a file opens when the click lands on the row already chosen
        else { this.clicked = e.node.key; Promise.resolve(e.node.setExpanded(!e.node.isExpanded())).catch(() => {}); }   // a click on a folder opens it, another folds it again
      },
      activate: (e) => {
        if (this.programmatic === e.node.key) { this.programmatic = null; return; }
        const entry = e.node.data.entry;
        if (!directory(entry)) { this.open(entry); return; }
        const sequence = ++this.sequence;
        this.run(async () => {
          const listing = await this.load(entry.path);
          if (sequence !== this.sequence || this.tree !== tree) return;
          this.listing = listing; this.selected = entry.path;
          this.drawCrumbs(); this.options.onDirectory?.(listing); this.updateStatus();
          if (this.clicked === e.node.key) this.clicked = null;      // the click already opened or folded it: leave it so
          else if (this.tree === tree) await e.node.setExpanded(true);                       // arrowing onto a folder opens it
        });
      },
    });
    this.tree = tree;
    tree.ready.then(() => {
      if (this.tree !== tree) return;
      this.activateQuietly(this.selected);
    }).catch((error) => { if (this.tree === tree) { this.Tree = null; this.fallback = true; this.draw(); console.warn("File tree:", error); } });
    host.addEventListener("contextmenu", (event) => {
      const node = this.Tree.getNode(event);
      if (node?.data.entry) this.menu(node.data.entry, event);
    });
    // Double-click (or Enter on) a folder makes it the top of the tree: its contents are the whole view, and the
    // breadcrumbs, Up and Top lead back out.
    host.addEventListener("dblclick", (event) => {
      const entry = this.Tree?.getNode(event)?.data.entry;
      if (entry && directory(entry)) this.enter(entry.path);
    });
    host.addEventListener("keydown", (event) => {
      const entry = event.key === "Enter" && !event.altKey && !event.ctrlKey && !event.metaKey ? this.tree?.getActiveNode()?.data.entry : null;
      if (entry && directory(entry)) { event.preventDefault(); event.stopPropagation(); this.enter(entry.path); }
    }, true);
  }
  /** Make a folder the top of the tree and show it. */
  enter(path) {
    if (path === this.root && this.listing?.path === path) return;
    this.root = path;
    this.expanded = new Set([path]);
    this.sequence++;
    return this.run(() => this.navigate(path));
  }
  drawBasic() {
    const branch = (entry, ancestry = new Set()) => {
      if (!directory(entry)) return el("div", { class: `basic-file${entry.path === this.selected ? " selected" : ""}`, "data-path": entry.path, oncontextmenu: (e) => this.menu(entry, e) },
        el("button", { type: "button", title: tooltip(entry), onclick: () => this.open(entry) }, label(entry)));
      const details = el("details", { "data-path": entry.path });
      details.classList.toggle("selected", entry.path === this.selected);
      const summary = el("summary", { title: tooltip(entry), oncontextmenu: (e) => this.menu(entry, e) }, label(entry));
      const children = el("div", { class: "basic-children" });
      details.append(summary, children);
      let populated = false;
      const populate = async () => {
        if (populated || ancestry.has(entry.path)) return;
        children.textContent = "Loading…";
        try {
          const listing = await this.load(entry.path);
          populated = true;
          const next = new Set(ancestry).add(entry.path);
          children.replaceChildren(...this.sorted(listing.entries).map((child) => branch(child, next)));
          if (!listing.entries.length) children.textContent = "Empty directory.";
          if (listing.truncated) children.append(el("p", {}, "Listing truncated by server"));
        } catch (error) {
          children.replaceChildren(el("button", { type: "button", onclick: () => this.run(populate) }, `Retry: ${error.message}`));
        }
      };
      summary.addEventListener("click", () => {
        const sequence = ++this.sequence;
        this.run(async () => {
          const listing = await this.load(entry.path);
          if (sequence !== this.sequence) return;
          this.listing = listing; this.select(entry.path); this.options.onDirectory?.(listing);
          if (this.mode === "details") this.draw();
          else { this.drawCrumbs(); this.updateStatus(); }
        });
      });
      details.addEventListener("toggle", () => {
        if (details.open) { this.expanded.add(entry.path); this.run(populate); }
        else this.expanded.delete(entry.path);
      });
      if (this.expanded.has(entry.path)) { details.open = true; this.run(populate); }
      return details;
    };
    this.viewport.append(branch(this.rootEntry()));
  }
}

if (!customElements.get("hpc-file-browser")) customElements.define("hpc-file-browser", HpcFileBrowser);
