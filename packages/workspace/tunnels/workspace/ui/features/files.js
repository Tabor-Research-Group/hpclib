// The file manager: a Tree and Details view of the cluster's files (<hpc-file-browser>, hpclib/ui, shared with the
// console's Files page), with the path bar, Places, upload, New file/folder, Usage and the ⋯ and right-click menus
// around it. What the shell needs from it, and all it needs from the shell, is here:
//
//   export function mountFiles(host)      draw into `host` (a block that fills the side column) and return
//
//   it reads   ws.info (user, host, home, root, places)
//   it sends   ws:cwd {path}        the directory it shows now (the shell and the terminal's "here" follow it)
//              ws:open {path, entry}  a file was opened
//              ws:select {paths}      the selection changed
//   it listens ws:reveal {path}     show this directory, or the directory of this file
//              ws:fs-changed {dir}  something wrote in dir: list it again if it is the one shown
//              ws:tab-path {path}   the tab now shown is this file: select it in the tree
//   it asks    ws.actionsFor(entry) for the verbs a ⋯ or right-click menu offers beyond its own (Open, Copy path,
//              Rename, Delete) and showMenu() (hpclib-ui) for the menu itself
//
// The server's files API (/api/list, /api/stat, /api/file, /api/mkdir, /api/rename, /api/delete) is unchanged by the
// shell, so a replacement keeps using it.

import { el, toast, showMenu } from "hpclib-ui/index.js";
import { api, q, join, dirname, size, when, ask, confirmAsk, fill } from "../client.js";
import { ws } from "../shell.js";

const isDir = (e) => e.type === "directory" || (e.type === "link" && e.target_type === "directory");
const send = (name, detail) => document.dispatchEvent(new CustomEvent(name, { detail }));

export function mountFiles(host) {
  const info = ws.info;
  const state = { dir: null, listing: null, hidden: false, selected: null };
  const files = el("hpc-file-browser", { class: "files", id: "files" });
  const path = el("input", { id: "path", "aria-label": "Directory", spellcheck: "false", autocomplete: "off" });
  const places = el("select", { "aria-label": "Places", onchange: () => { if (places.value) go(places.value); places.value = ""; } },
    el("option", { value: "" }, "Places"), info.places.map((p) => el("option", { value: p.path }, `${p.label}: ${p.path}`)));
  const filter = el("input", { id: "file-filter", type: "search", placeholder: "Filter: *.py  run_?.sh  [a-c]*", "aria-label": "Filter files and folders by name (wildcards * ? [abc])",
    spellcheck: "false", autocomplete: "off", title: "Show only the files and folders matching these patterns: * any text, ? one character, [abc] a set; separate several with spaces or commas",
    oninput: () => files.setFilter(filter.value), onkeydown: (e) => { if (e.key === "Escape" && filter.value) { filter.value = ""; files.setFilter(""); } } });
  const picker = el("input", { type: "file", multiple: true, hidden: true, onchange: () => { upload([...picker.files]); picker.value = ""; } });
  const hiddenBox = el("input", { type: "checkbox", onchange: () => { state.hidden = hiddenBox.checked; files.invalidate(); go(state.dir); } });
  host.append(
    el("form", { class: "path-form", onsubmit: (e) => { e.preventDefault(); go(path.value.trim() || info.home); } }, path),
    el("div", { class: "actions files-tools" }, places,
      el("button", { type: "button", title: "List it again", onclick: () => go(state.dir) }, "Refresh"),
      el("button", { type: "button", onclick: newFile }, "New file"),
      el("button", { type: "button", onclick: newFolder }, "New folder"),
      el("button", { type: "button", title: "This folder's folders by size and by file count",
        onclick: () => ws.open(state.dir, { dir: true }, { opener: "usage" }) }, "Usage"),
      el("button", { type: "button", onclick: () => picker.click() }, "Upload"),
      el("label", { class: "check small" }, hiddenBox, "Hidden files"), filter, picker),
    files);
  const withDir = (e) => Object.assign({}, e, { dir: isDir(e) });
  files.configure({
    storageKey: "ws.files.mode", size, when, refresh: false,
    list: (dir) => api(`/api/list?${q({ path: dir, hidden: state.hidden ? "1" : "0" })}`),
    onOpen: (entry) => select(withDir(entry)),
    onMenu: (entry, event) => menu(withDir(entry), event.currentTarget && event.currentTarget.tagName === "BUTTON" ? event.currentTarget : event),
    onDirectory: (listing) => {
      state.listing = listing;
      state.dir = listing.path;
      path.value = listing.path;
      history.replaceState(null, "", `${location.pathname}${location.search}#${q({ dir: state.dir })}`);
      send("ws:cwd", { path: state.dir });
    },
    onError: (err) => toast(err.message, { tone: "error" }),
  });

  /** Show a directory (listing it again, so it is current). false when it couldn't be listed. */
  async function go(dir, { quiet = false } = {}) {
    try {
      await files.navigate(dir, { refresh: true });
      return true;
    } catch (err) {
      if (!quiet) toast(err.message, { tone: "error" });
      return false;
    }
  }

  function select(entry) {
    state.selected = entry.path;
    send("ws:select", { paths: [entry.path] });
    send("ws:open", { path: entry.path, entry });
  }

  function menu(entry, at) {
    const items = [{ label: "Open", run: () => (entry.dir ? go(entry.path) : select(entry)) }];
    let group = null;
    for (const a of ws.actionsFor(entry)) {
      if (group !== null && a.group !== group) items.push({ separator: true });
      group = a.group;
      items.push({ label: a.label, run: () => a.run(entry) });
    }
    items.push({ separator: true },
      { label: "Copy path", run: () => navigator.clipboard.writeText(entry.path).then(() => toast("Copied."), () => toast(entry.path, { ms: 8000 })) },
      { label: "Rename…", run: () => rename(entry.path) },
      { label: "Delete…", tone: "danger", run: () => remove(entry.path, entry.dir) });
    showMenu(items, at);
  }

  async function rename(from) {
    const to = await ask(`Rename or move ${from} to:`, from, "Rename");
    if (!to || to === from) return;
    try {
      await api("/api/rename", { method: "POST", body: { from, to: to.startsWith("/") ? to : join(dirname(from), to) } });
      for (const t of ws.tabs.list.filter((t) => t.path === from)) ws.tabs.close(t.id, true);
      go(state.dir);
    } catch (err) { toast(err.message, { tone: "error" }); }
  }

  async function remove(target, dir) {
    if (!await confirmAsk(`Delete ${target}${dir ? " (an empty directory)" : ""}? This can't be undone.`, "Delete")) return;
    try {
      await api("/api/delete", { method: "POST", body: { path: target } });
      for (const t of ws.tabs.list.filter((t) => t.path === target)) ws.tabs.close(t.id, true);
      go(state.dir);
    } catch (err) { toast(err.message, { tone: "error" }); }
  }

  let creating = false;       // a create is on its way: asking again (a second click, a repeated Enter) waits for it
  async function newFile() {
    if (creating) return;
    creating = true;
    try {
      const name = await ask(`A new file in ${state.dir}:`, "", "Create");
      if (!name) return;
      const target = name.startsWith("/") ? name : join(state.dir, name);
      try {
        await api(`/api/file?${q({ path: target, how: "create" })}`, { method: "PUT", body: "" });
        await go(state.dir);
        ws.open(target, { size: 0 }, { opener: "text" });
      } catch (err) { toast(err.message, { tone: "error" }); }
    } finally { creating = false; }
  }

  async function newFolder() {
    if (creating) return;
    creating = true;
    try {
      const name = await ask(`A new folder in ${state.dir}:`, "", "Create");
      if (!name) return;
      try {
        const out = await api("/api/mkdir", { method: "POST", body: { path: name.startsWith("/") ? name : join(state.dir, name) } });
        go(out.path);
      } catch (err) { toast(err.message, { tone: "error" }); }
    } finally { creating = false; }
  }

  async function upload(list) {
    const dir = state.dir;
    for (const f of list) {
      const target = join(dir, f.name);
      toast(`Uploading ${f.name} (${size(f.size)})…`, { ms: 60000 });
      try {
        try {
          await api(`/api/file?${q({ path: target, how: "create" })}`, { method: "PUT", body: f });
        } catch (err) {
          if (err.status !== 409 || !await confirmAsk(`${target} exists. Replace it?`, "Replace")) throw err;
          await api(`/api/file?${q({ path: target, how: "any" })}`, { method: "PUT", body: f });
        }
      } catch (err) { toast(`${f.name}: ${err.message}`, { tone: "error" }); return; }
    }
    toast(`Uploaded ${list.length} file${list.length > 1 ? "s" : ""}.`, { tone: "ok" });
    if (state.dir === dir) go(dir);
  }

  files.addEventListener("dragover", (e) => { e.preventDefault(); files.classList.add("drop"); });
  files.addEventListener("dragleave", () => files.classList.remove("drop"));
  files.addEventListener("drop", (e) => {
    e.preventDefault();
    files.classList.remove("drop");
    if (e.dataTransfer.files.length) upload([...e.dataTransfer.files]);
  });
  document.addEventListener("ws:reveal", (e) => {
    const target = e.detail.path;
    go(target, { quiet: true }).then((ok) => { if (!ok) go(dirname(target)); });
  });
  document.addEventListener("ws:fs-changed", (e) => { if (e.detail.dir === state.dir) go(state.dir, { quiet: true }); });
  document.addEventListener("ws:tab-path", (e) => files.select(e.detail.path));

  // A link can name a file: #file=/path/to/file[&line=N]. Its folder is shown, and app.js opens the file once the tabs
  // from last time are back (a saved tab doesn't take the link's place). `dir=` still names a folder alone.
  const hash = new URLSearchParams(location.hash.slice(1));
  const linked = hash.get("file");
  const link = linked ? { path: linked.startsWith("/") ? linked : join(info.home, linked), line: Math.floor(Number(hash.get("line"))) || 0 } : null;
  return { go, link, ready: (async () => {
    await go(link ? dirname(link.path) : hash.get("dir") || info.root);
    if (!state.listing) await go(info.home);
  })() };
}
