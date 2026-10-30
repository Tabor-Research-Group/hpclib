// The Workspace page (workspace_server.py serves it): a file list, tabs of editors (CodeMirror), viewers and job
// submission forms, terminals (xterm.js) and your jobs. CodeMirror and xterm.js load from the CDN the server names
// (info.cdn); without them the editor is a plain text box and terminals say why they can't open.
"use strict";

const CM = "codemirror@5.65.18";
const XTERM = "@xterm/xterm@5.5.0";
const XTERM_FIT = "@xterm/addon-fit@0.10.0";
const CM_MODES = ["python", "shell", "yaml", "javascript", "toml", "markdown", "xml", "htmlmixed", "css", "clike",
                  "fortran", "r", "julia", "sql", "properties"];
const MODES = {
  py: "python", pyw: "python", ipy: "python", sh: "shell", bash: "shell", zsh: "shell", slurm: "shell",
  sbatch: "shell", job: "shell", bashrc: "shell", profile: "shell", yaml: "yaml", yml: "yaml", cff: "yaml",
  json: "application/json", js: "javascript", mjs: "javascript", ts: "text/typescript", toml: "toml", lock: "toml",
  md: "markdown", markdown: "markdown", xml: "xml", html: "htmlmixed", htm: "htmlmixed", svg: "xml", css: "css",
  c: "text/x-csrc", h: "text/x-csrc", cpp: "text/x-c++src", cc: "text/x-c++src", hpp: "text/x-c++src",
  cu: "text/x-c++src", java: "text/x-java", f: "fortran", f90: "fortran", f95: "fortran", f03: "fortran",
  r: "r", jl: "julia", sql: "sql", ini: "properties", cfg: "properties", conf: "properties",
};
const IMAGE = /\.(png|jpe?g|gif|webp|bmp|ico|avif|tiff?)$/i;
const VIDEO = /\.(mp4|m4v|webm|ogv|mov)$/i;
const AUDIO = /\.(mp3|wav|ogg|oga|flac|m4a|aac)$/i;
const PDF = /\.pdf$/i;
const HTML = /\.html?$/i;
const SVG = /\.svg$/i;
const SUBMITTABLE = /\.(py|sh|bash)$/i;

// ------------------------------------------------------------------ basics

const $ = (id) => document.getElementById(id);
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else if (k in node && typeof v !== "string") node[k] = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
}
// replaceChildren, leaving out the null/false a conditional child gives (replaceChildren would print "null")
const fill = (node, ...kids) => node.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
const token = (() => {
  const q = new URLSearchParams(location.search);
  let t = q.get("token");
  try {
    if (t) sessionStorage.setItem("workspace-token", t);
    else t = sessionStorage.getItem("workspace-token");
  } catch (e) { /* storage may be off: keep it in the URL then */ }
  if (q.get("token")) {
    try { sessionStorage.getItem("workspace-token") && history.replaceState(null, "", location.pathname + location.hash); }
    catch (e) { /* keep the URL */ }
  }
  return t || "";
})();
async function api(path, { method = "GET", body, raw = false, headers = {} } = {}) {
  const init = { method, headers: { "X-Workspace-Token": token, ...headers } };
  if (body !== undefined) {
    if (body instanceof Blob || body instanceof ArrayBuffer || typeof body === "string") init.body = body;
    else { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
  }
  const res = await fetch(path, init);
  if (raw && res.ok) return res;
  let data = {};
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (!res.ok) {
    const err = new Error(data.error || `${res.status} ${res.statusText}`);
    err.status = res.status;
    throw err;
  }
  return data;
}
const q = (params) => new URLSearchParams(params).toString();
// A file for an <img>, <iframe> or <video>: the server signs a URL that expires and covers the file's directory (the
// token itself never goes in a URL, where a page the viewer shows could read it).
const rawUrl = async (path) => (await api("/api/raw-url", { method: "POST", body: { path } })).url;
const join = (dir, name) => (dir === "/" ? "" : dir) + "/" + name;
const dirname = (p) => p.replace(/\/[^/]*$/, "") || "/";
const basename = (p) => p.replace(/^.*\//, "");
const ext = (p) => { const m = basename(p).match(/\.([^.]+)$/); return m ? m[1].toLowerCase() : basename(p).toLowerCase(); };
function size(n) {
  if (n === null || n === undefined) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}
function when(t) {
  const d = new Date(t * 1000);
  const now = new Date();
  return d.toDateString() === now.toDateString() ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { year: d.getFullYear() === now.getFullYear() ? undefined : "numeric", month: "short", day: "numeric" });
}
let toastTimer = null;
function toast(text, ms = 3500) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}
function ask(text, value = "", ok = "OK") {
  const d = $("ask");
  $("ask-text").textContent = text;
  const input = $("ask-input");
  input.hidden = value === null;
  input.value = value || "";
  $("ask-ok").textContent = ok;
  return new Promise((resolve) => {
    d.addEventListener("close", () => resolve(d.returnValue === "ok" ? (value === null ? true : input.value) : null),
                       { once: true });
    d.returnValue = "";
    d.showModal();
    if (!input.hidden) { input.focus(); input.select(); }
  });
}
const confirmAsk = (text, ok = "OK") => ask(text, null, ok);

// ------------------------------------------------------------------ CDN libraries

const loaded = {};
function loadScript(url) {
  loaded[url] = loaded[url] || new Promise((resolve, reject) => {
    const s = el("script", { src: url, crossorigin: "anonymous" });
    s.onload = resolve;
    s.onerror = () => reject(new Error(`couldn't load ${url}`));
    document.head.append(s);
  });
  return loaded[url];
}
function loadStyle(url) {
  if (!document.querySelector(`link[href="${url}"]`)) document.head.append(el("link", { rel: "stylesheet", href: url }));
}
let editorLib = null;
function codemirror() {
  editorLib = editorLib || (async () => {
    const base = `${state.info.cdn}/${CM}`;
    loadStyle(`${base}/lib/codemirror.css`);
    loadStyle(`${base}/theme/material-darker.css`);
    loadStyle(`${base}/addon/dialog/dialog.css`);
    await loadScript(`${base}/lib/codemirror.js`);
    await Promise.all([...CM_MODES.map((m) => `${base}/mode/${m}/${m}.js`),
      `${base}/addon/edit/matchbrackets.js`, `${base}/addon/selection/active-line.js`,
      `${base}/addon/dialog/dialog.js`, `${base}/addon/search/searchcursor.js`, `${base}/addon/search/search.js`,
      `${base}/addon/search/jump-to-line.js`].map((u) => loadScript(u).catch(() => null)));
    return window.CodeMirror;
  })().catch(() => null);
  return editorLib;
}
let termLib = null;
function xterm() {
  termLib = termLib || (async () => {
    loadStyle(`${state.info.cdn}/${XTERM}/css/xterm.css`);
    await loadScript(`${state.info.cdn}/${XTERM}/lib/xterm.js`);
    await loadScript(`${state.info.cdn}/${XTERM_FIT}/lib/addon-fit.js`);
    return { Terminal: window.Terminal, FitAddon: window.FitAddon && window.FitAddon.FitAddon };
  })();
  return termLib;
}
const dark = () => matchMedia("(prefers-color-scheme: dark)").matches;

// An editor: CodeMirror when it loaded, else a textarea with the same few methods.
async function makeEditor(parent, text, { mode = null, readOnly = false, onChange = null, onSave = null } = {}) {
  const CodeMirror = await codemirror();
  if (CodeMirror) {
    const cm = CodeMirror(parent, {
      value: text, mode, readOnly, lineNumbers: true, matchBrackets: true, styleActiveLine: true, indentUnit: 4,
      tabSize: 4, indentWithTabs: false, lineWrapping: false, theme: dark() ? "material-darker" : "default",
      extraKeys: { "Cmd-S": () => onSave && onSave(), "Ctrl-S": () => onSave && onSave(), Tab: (c) => {
        if (c.somethingSelected()) c.indentSelection("add"); else c.replaceSelection(" ".repeat(c.getOption("indentUnit")));
      } },
    });
    if (onChange) cm.on("change", onChange);
    setTimeout(() => cm.refresh(), 0);
    return { get: () => cm.getValue(), set: (v) => cm.setValue(v), focus: () => cm.focus(), refresh: () => cm.refresh(),
             clean: () => cm.markClean(), isClean: () => cm.isClean(), cm };
  }
  const ta = el("textarea", { class: "plain", spellcheck: false, readOnly });
  ta.value = text;
  let saved = text;
  ta.addEventListener("input", () => onChange && onChange());
  ta.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); onSave && onSave(); }
  });
  parent.append(ta);
  return { get: () => ta.value, set: (v) => { ta.value = v; }, focus: () => ta.focus(), refresh: () => {},
           clean: () => { saved = ta.value; }, isClean: () => ta.value === saved, plain: true };
}

// ------------------------------------------------------------------ state

const state = { info: null, dir: null, listing: null, hidden: false, tabs: [], active: null, templates: null,
                terminals: [], activeTerm: null, selected: null };

function setHash() {
  history.replaceState(null, "", `${location.pathname}${location.search}#${q({ dir: state.dir })}`);
}

// ------------------------------------------------------------------ the file list

async function go(dir, { quiet = false } = {}) {
  const files = $("files");
  try {
    const listing = await api(`/api/list?${q({ path: dir, hidden: state.hidden ? "1" : "0" })}`);
    state.listing = listing;
    state.dir = listing.path;
    $("path").value = listing.path;
    $("up").disabled = !listing.parent;
    setHash();
    drawFiles();
  } catch (err) {
    if (quiet) return;
    toast(err.message, 6000);
    if (!state.listing) files.replaceChildren(el("p", { class: "note error-text" }, err.message));
  }
}

function drawFiles() {
  const { entries, truncated, hidden } = state.listing;
  const sorted = entries.slice().sort((a, b) => (isDir(a) ? 0 : 1) - (isDir(b) ? 0 : 1) ||
    a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" }));
  const rows = sorted.map((e) => {
    const path = join(state.dir, e.name);
    const dir = isDir(e);
    const more = el("button", { type: "button", class: "more small-button", title: "More", "aria-label": `More for ${e.name}`,
      onclick: (ev) => { ev.stopPropagation(); fileMenu(e, path, ev.currentTarget); } }, "⋯");
    const tr = el("tr", { class: state.selected === path ? "selected" : null, "data-path": path,
      onclick: () => (dir ? go(path) : openFile(path, e)),
      oncontextmenu: (ev) => { ev.preventDefault(); fileMenu(e, path, ev); } },
      el("td", { class: "icon" }, dir ? "▸" : e.type === "link" ? "↪" : ""),
      el("td", { class: "name" }, el("span", { class: dir ? "dir-name" : null }, e.name + (dir ? "/" : "")),
        e.type === "link" ? el("span", { class: "muted small" }, ` → ${e.target}`) : null),
      el("td", { class: "size" }, dir ? "" : size(e.size)),
      el("td", { class: "when" }, when(e.mtime)),
      el("td", {}, more));
    return tr;
  });
  const notes = [];
  if (!entries.length) notes.push(el("p", { class: "note muted" }, "Empty."));
  if (truncated) notes.push(el("p", { class: "note muted" }, "Only the first 5000 are listed."));
  if (hidden && !state.hidden) notes.push(el("p", { class: "note muted small" }, `${hidden} hidden (Hidden files shows them).`));
  $("files").replaceChildren(el("table", {}, el("tbody", {}, rows)), ...notes);
}
const isDir = (e) => e.type === "directory" || (e.type === "link" && e.target_type === "directory");

function closeMenu() { $("menu").hidden = true; }
function showMenu(items, at) {
  const menu = $("menu");
  menu.replaceChildren(...items.filter(Boolean).map(([label, fn, cls]) =>
    el("button", { type: "button", class: cls || null, role: "menuitem", onclick: () => { closeMenu(); fn(); } }, label)));
  menu.hidden = false;
  const r = at.getBoundingClientRect ? at.getBoundingClientRect() : { left: at.clientX, bottom: at.clientY, right: at.clientX };
  const w = menu.offsetWidth, h = menu.offsetHeight;
  menu.style.left = `${Math.max(4, Math.min(r.left, innerWidth - w - 4))}px`;
  menu.style.top = `${Math.max(4, Math.min(r.bottom, innerHeight - h - 4))}px`;
}
document.addEventListener("click", (e) => { if (!$("menu").contains(e.target)) closeMenu(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });

function fileMenu(e, path, at) {
  const dir = isDir(e);
  showMenu([
    dir ? ["Open", () => go(path)] : ["Open", () => openFile(path, e)],
    !dir ? ["Open as text", () => openFile(path, e, { asText: true })] : null,
    SUBMITTABLE.test(path) && !dir ? ["Submit job…", () => openSubmit(path)] : null,
    dir ? ["Terminal here", () => newTerminal(path)] : ["Terminal in its directory", () => newTerminal(dirname(path))],
    dir ? ["Disk usage", () => openUsage(path)] : null,
    !dir ? ["Download", () => download(path)] : null,
    ["Copy path", () => navigator.clipboard.writeText(path).then(() => toast("Copied."), () => toast(path, 8000))],
    ["Rename…", () => rename(path)],
    ["Delete…", () => remove(path, dir), "danger"],
  ], at);
}

async function download(path) {
  try {
    const a = el("a", { href: await rawUrl(path), download: basename(path) });
    document.body.append(a);
    a.click();
    a.remove();
  } catch (err) { toast(err.message, 6000); }
}

async function rename(path) {
  const to = await ask(`Rename or move ${path} to:`, path, "Rename");
  if (!to || to === path) return;
  try {
    await api("/api/rename", { method: "POST", body: { from: path, to: to.startsWith("/") ? to : join(dirname(path), to) } });
    for (const t of state.tabs) if (t.path === path) closeTab(t, true);
    go(state.dir);
  } catch (err) { toast(err.message, 6000); }
}

async function remove(path, dir) {
  if (!await confirmAsk(`Delete ${path}${dir ? " (an empty directory)" : ""}? This can't be undone.`, "Delete")) return;
  try {
    await api("/api/delete", { method: "POST", body: { path } });
    for (const t of state.tabs) if (t.path === path) closeTab(t, true);
    go(state.dir);
  } catch (err) { toast(err.message, 6000); }
}

async function newFile() {
  const name = await ask(`A new file in ${state.dir}:`, "", "Create");
  if (!name) return;
  const path = name.startsWith("/") ? name : join(state.dir, name);
  try {
    await api(`/api/file?${q({ path, how: "create" })}`, { method: "PUT", body: "" });
    await go(state.dir);
    openFile(path, { size: 0 }, { asText: true });
  } catch (err) { toast(err.message, 6000); }
}

async function newFolder() {
  const name = await ask(`A new folder in ${state.dir}:`, "", "Create");
  if (!name) return;
  try {
    const out = await api("/api/mkdir", { method: "POST", body: { path: name.startsWith("/") ? name : join(state.dir, name) } });
    go(out.path);
  } catch (err) { toast(err.message, 6000); }
}

async function upload(files) {
  const dir = state.dir;
  for (const f of files) {
    const path = join(dir, f.name);
    toast(`Uploading ${f.name} (${size(f.size)})…`, 60000);
    try {
      try {
        await api(`/api/file?${q({ path, how: "create" })}`, { method: "PUT", body: f });
      } catch (err) {
        if (err.status !== 409 || !await confirmAsk(`${path} exists. Replace it?`, "Replace")) throw err;
        await api(`/api/file?${q({ path, how: "any" })}`, { method: "PUT", body: f });
      }
    } catch (err) { toast(`${f.name}: ${err.message}`, 8000); return; }
  }
  toast(`Uploaded ${files.length} file${files.length > 1 ? "s" : ""}.`);
  if (state.dir === dir) go(dir);
}

// ------------------------------------------------------------------ tabs

function addTab(tab) {
  state.tabs.push(tab);
  tab.view.classList.add("view");
  $("views").append(tab.view);
  activate(tab);
  return tab;
}
function activate(tab) {
  state.active = tab;
  for (const t of state.tabs) t.view.hidden = t !== tab;
  $("empty").hidden = !!tab;
  drawTabs();
  if (tab && tab.editor) setTimeout(() => tab.editor.refresh(), 0);
  if (tab && tab.preview) setTimeout(() => tab.preview.refresh(), 0);
  if (tab && tab.path) state.selected = tab.path;
}
async function closeTab(tab, force = false) {
  if (!force && tab.dirty && !await confirmAsk(`${basename(tab.path)} has changes that aren't saved. Close it anyway?`, "Close")) return;
  state.tabs = state.tabs.filter((t) => t !== tab);
  tab.view.remove();
  if (state.active === tab) activate(state.tabs[state.tabs.length - 1] || null);
  else drawTabs();
}
function drawTabs() {
  $("tabs").replaceChildren(...state.tabs.map((t) => el("div", {
    class: `tab${t === state.active ? " active" : ""}${t.dirty ? " dirty" : ""}`, role: "tab", title: t.path,
    "aria-selected": String(t === state.active), onclick: () => activate(t),
    onauxclick: (e) => { if (e.button === 1) closeTab(t); } },
    el("span", { class: "label" }, t.label),
    el("button", { type: "button", class: "close", "aria-label": `Close ${t.label}`,
                   onclick: (e) => { e.stopPropagation(); closeTab(t); } }, "×"))));
}
addEventListener("beforeunload", (e) => {
  if (state.tabs.some((t) => t.dirty)) { e.preventDefault(); e.returnValue = ""; }
});

// ------------------------------------------------------------------ opening files

async function openFile(path, e = {}, { asText = false } = {}) {
  const existing = state.tabs.find((t) => t.path === path && t.kind === (asText ? "text" : kindOf(path)));
  if (existing) return activate(existing);
  const kind = asText ? "text" : kindOf(path);
  if (kind === "text") return openText(path, e);
  return openViewer(path, kind);
}
function kindOf(path) {
  if (IMAGE.test(path) || SVG.test(path)) return "image";
  if (VIDEO.test(path)) return "video";
  if (AUDIO.test(path)) return "audio";
  if (PDF.test(path)) return "pdf";
  if (HTML.test(path)) return "html";
  return "text";
}

async function openViewer(path, kind) {
  let url;
  try { url = await rawUrl(path); } catch (err) { toast(err.message, 6000); return; }
  const body = el("div", { class: `body${kind === "pdf" || kind === "html" ? "" : " center"}` });
  if (kind === "image") body.append(el("img", { src: url, alt: basename(path) }));
  else if (kind === "video") body.append(el("video", { src: url, controls: true, preload: "metadata" }));
  else if (kind === "audio") body.append(el("audio", { src: url, controls: true }));
  else if (kind === "pdf") body.append(el("iframe", { src: url, title: basename(path) }));
  else if (kind === "html") {
    // the page runs its scripts, in an opaque origin (the server's CSP sandbox, and this frame's)
    body.append(el("iframe", { src: url, title: basename(path), sandbox: "allow-scripts allow-popups" }));
  }
  const reload = async () => {
    try { url = await rawUrl(path); } catch (err) { toast(err.message, 6000); return; }
    body.firstChild.src = `${url}?t=${Date.now()}`;
  };
  const bar = el("div", { class: "bar" },
    el("code", {}, path),
    el("button", { type: "button", onclick: reload }, "Reload"),
    el("button", { type: "button", onclick: () => window.open(url, "_blank", "noopener") }, "New browser tab ↗"),
    el("button", { type: "button", onclick: () => download(path) }, "Download"),
    kind === "html" || /\.svg$/i.test(path) ? el("button", { type: "button", onclick: () => openFile(path, {}, { asText: true }) }, "Edit source") : null);
  return addTab({ kind, path, label: basename(path), view: el("div", {}, bar, body) });
}

async function openText(path, e) {
  const body = el("div", { class: "body" });
  const status = el("span", { class: "status" }, "Loading…");
  const view = el("div", {}, el("div", { class: "bar" }, el("code", {}, path), status), body);
  const tab = addTab({ kind: "text", path, label: basename(path), view });
  const limits = state.info.limits;
  let st = e;
  if (st.size === undefined) {
    try { st = await api(`/api/stat?${q({ path })}`); } catch (err) { status.textContent = err.message; status.classList.add("error"); return; }
  }
  if (st.size > limits.edit_bytes) {
    body.replaceChildren(el("div", { class: "note-box" }, el("p", {}, `${basename(path)} is ${size(st.size)}, more than the editor opens (${size(limits.edit_bytes)}).`),
      el("button", { type: "button", onclick: () => download(path) }, "Download")));
    status.textContent = "";
    return;
  }
  if (st.size > limits.confirm_bytes) {
    status.textContent = "";
    await new Promise((resolve) => body.replaceChildren(el("div", { class: "note-box" },
      el("p", {}, `${basename(path)} is ${size(st.size)}: loading it into the editor may take a while.`),
      el("div", { class: "actions" }, el("button", { type: "button", class: "primary", onclick: resolve }, "Open it"),
        el("button", { type: "button", onclick: () => download(path) }, "Download")))));
    status.textContent = "Loading…";
    body.replaceChildren();
  }
  await loadText(tab, body, status, false);
}

async function loadText(tab, body, status, force) {
  let res;
  try { res = await api(`/api/file?${q({ path: tab.path })}`, { raw: true }); }
  catch (err) { status.textContent = err.message; status.classList.add("error"); return; }
  const bytes = new Uint8Array(await res.arrayBuffer());
  tab.mtime = res.headers.get("X-Mtime-Ns");
  const writable = res.headers.get("X-Writable") === "1";
  if (!force && bytes.subarray(0, 8192).includes(0)) {
    status.textContent = "";
    body.replaceChildren(el("div", { class: "note-box" }, el("p", {}, `${basename(tab.path)} looks binary (${size(bytes.length)}).`),
      el("div", { class: "actions" }, el("button", { type: "button", onclick: () => download(tab.path) }, "Download"),
        el("button", { type: "button", onclick: () => { body.replaceChildren(); loadText(tab, body, status, true); } }, "Open as text anyway"))));
    return;
  }
  const text = new TextDecoder("utf-8").decode(bytes);
  body.replaceChildren();
  const save = async (overwrite = false) => {
    status.textContent = "Saving…";
    status.classList.remove("error");
    try {
      const out = await api(`/api/file?${q(overwrite ? { path: tab.path } : { path: tab.path, mtime_ns: tab.mtime || "" })}`,
                            { method: "PUT", body: tab.editor.get() });
      tab.mtime = out.mtime_ns;
      tab.editor.clean();
      setDirty(false);
      status.textContent = `Saved ${new Date().toLocaleTimeString()}`;
      if (dirname(tab.path) === state.dir) go(state.dir, { quiet: true });
    } catch (err) {
      status.textContent = err.message;
      status.classList.add("error");
      if (err.status === 409 && await confirmAsk(`${basename(tab.path)} changed on the cluster since you opened it. ` +
          "Overwrite it with yours? (Cancel keeps both: reload to see theirs.)", "Overwrite")) save(true);
    }
  };
  const setDirty = (d) => { if (tab.dirty !== d) { tab.dirty = d; drawTabs(); } };
  tab.editor = await makeEditor(body, text, { mode: MODES[ext(tab.path)] || null, readOnly: !writable,
    onChange: () => setDirty(!tab.editor.isClean()), onSave: () => save() });
  tab.editor.clean();
  status.textContent = writable ? `${size(bytes.length)}` : "read-only";
  const bar = tab.view.querySelector(".bar");
  const reload = async () => {
    if (tab.dirty && !await confirmAsk("Discard your changes and load the file again?", "Reload")) return;
    body.replaceChildren();
    setDirty(false);
    loadText(tab, body, status, true);
  };
  fill(bar, el("code", {}, tab.path),
    writable ? el("button", { type: "button", class: "primary", onclick: () => save() }, "Save") : null,
    el("button", { type: "button", onclick: reload }, "Reload"),
    SUBMITTABLE.test(tab.path) ? el("button", { type: "button", onclick: () => openSubmit(tab.path) }, "Submit job…") : null,
    tab.editor.plain ? el("span", { class: "muted small" }, "(plain editor: CodeMirror didn't load from the CDN)") : null,
    status);
  if (tab === state.active) tab.editor.focus();
}

// ------------------------------------------------------------------ submitting jobs

async function templates(refresh = false) {
  if (!state.templates || refresh) state.templates = await api("/api/templates");
  return state.templates;
}

async function openSubmit(input) {
  const existing = state.tabs.find((t) => t.kind === "submit" && t.path === input);
  if (existing) return activate(existing);
  const view = el("div", {}, el("div", { class: "submit" }, el("p", { class: "muted" }, "Loading the templates…")));
  const tab = addTab({ kind: "submit", path: input, label: `Submit: ${basename(input)}`, view });
  try { await drawSubmit(tab, await templates(true)); }
  catch (err) { view.firstChild.replaceChildren(el("p", { class: "error-text" }, err.message)); }
}

async function drawSubmit(tab, list, chosen = null) {
  const input = tab.path;
  const box = tab.view.firstChild;
  const fits = (t) => t.for.includes("*") || t.for.some((e) => input.endsWith(e));
  const offered = list.templates.filter(fits);
  const others = list.templates.filter((t) => !fits(t));
  const pick = chosen || (offered.find((t) => t.name === list.last) || offered.find((t) => t.name === "python-uv") ||
    offered[0] || list.templates[0] || {}).name;
  const template = list.templates.find((t) => t.name === pick);
  if (!template) { box.replaceChildren(el("p", {}, "No script templates.")); return; }
  const remembered = list.remembered[template.name] || {};
  const select = el("select", { "aria-label": "Template", onchange: () => drawSubmit(tab, list, select.value) },
    el("optgroup", { label: "For this file" }, offered.map((t) => el("option", { value: t.name, selected: t.name === pick },
      `${t.title}${t.source === "yours" ? " (yours)" : ""}`))),
    others.length ? el("optgroup", { label: "Others" }, others.map((t) => el("option", { value: t.name, selected: t.name === pick }, t.title))) : null);
  const inputs = {};
  const fields = template.fields.map((f) => {
    const value = remembered[f.name] !== undefined ? remembered[f.name] : null;
    inputs[f.name] = el("input", { "aria-label": f.label, "data-field": f.name, spellcheck: false,
      placeholder: f.hint || "", title: f.hint || "", oninput: () => schedule() });
    if (value !== null) inputs[f.name].value = value;
    inputs[f.name].dataset.untouched = value === null ? "1" : "";
    return el("label", { class: "field" }, f.label, inputs[f.name]);
  });
  const name = el("input", { class: "name", "aria-label": "Job script name", spellcheck: false });
  const overwrite = el("input", { type: "checkbox" });
  const result = el("div", { hidden: true });
  const previewBox = el("div", { class: "preview" });
  const handNote = el("span", { class: "muted small", hidden: true }, "Edited by hand: the form no longer changes it. ");
  const rebuild = el("button", { type: "button", class: "small-button", hidden: true,
    onclick: () => { handEdited = false; handNote.hidden = rebuild.hidden = true; render(); } }, "Rebuild from the form");
  let handEdited = false, settingPreview = false, rendered = null, timer = null;
  const preview = await makeEditor(previewBox, "", { mode: "shell", onChange: () => {
    if (settingPreview) return;
    handEdited = true;
    handNote.hidden = rebuild.hidden = false;
  } });
  tab.preview = preview;
  const values = () => {
    const out = {};
    for (const [k, i] of Object.entries(inputs)) if (!i.dataset.untouched || i.value) out[k] = i.value;
    return out;
  };
  const render = async () => {
    try {
      rendered = await api("/api/render", { method: "POST", body: { template: template.name, input, values: values() } });
      for (const f of template.fields) {
        const i = inputs[f.name];
        if (i.dataset.untouched && !i.value) i.placeholder = rendered.values[f.name] ? `${rendered.values[f.name]}` : (f.hint || "");
      }
      if (!name.dataset.touched) name.value = rendered.name;
      if (!handEdited) { settingPreview = true; preview.set(rendered.script); settingPreview = false; }
      result.hidden = true;
    } catch (err) {
      result.hidden = false;
      result.className = "result error";
      result.textContent = err.message;
    }
  };
  const schedule = () => { clearTimeout(timer); timer = setTimeout(render, 250); };
  for (const i of Object.values(inputs)) i.addEventListener("input", () => { i.dataset.untouched = ""; });
  name.addEventListener("input", () => { name.dataset.touched = "1"; });
  const submit = el("button", { type: "button", class: "primary", onclick: async () => {
    submit.disabled = true;
    result.hidden = false;
    result.className = "result";
    result.textContent = "Submitting…";
    try {
      const vals = Object.assign({}, rendered ? rendered.values : {}, values());
      const out = await api("/api/submit", { method: "POST", body: { script: preview.get(), name: name.value.trim(),
        dir: dirname(input), overwrite: overwrite.checked, template: template.name, values: vals } });
      const jobName = (rendered && rendered.values.job_name) || "";
      const guess = jobName && /%x-%j\.out/.test(preview.get()) ? join(dirname(input), `${jobName}-${out.job_id}.out`) : null;
      result.className = "result ok";
      fill(result, `Submitted job ${out.job_id}: ${out.path}. `,
        guess ? el("button", { type: "button", class: "small-button", onclick: () => openFile(guess, {}, { asText: true }) },
                   `Open ${basename(guess)}`) : null);
      state.templates = null;
      refreshJobs();
      if (dirname(input) === state.dir) go(state.dir, { quiet: true });
    } catch (err) {
      result.className = "result error";
      result.textContent = err.message;
    } finally { submit.disabled = false; }
  } }, "Submit");
  const edit = el("button", { type: "button", onclick: async () => {
    try {
      const out = await api("/api/templates/edit", { method: "POST", body: { name: template.name } });
      toast(out.created ? `Your copy: ${out.path}. Changes apply the next time you open Submit job.` : `Yours: ${out.path}`, 7000);
      state.templates = null;
      openFile(out.path, {}, { asText: true });
    } catch (err) { toast(err.message, 6000); }
  } }, template.source === "yours" ? "Edit template" : "Edit (your copy)");
  const copy = el("button", { type: "button", onclick: async () => {
    const n = await ask("A name for the new template (letters, digits, _ . -):", `${template.name}-mine`, "Create");
    if (!n) return;
    try {
      const out = await api("/api/templates/edit", { method: "POST", body: { name: n, from: template.name } });
      state.templates = null;
      openFile(out.path, {}, { asText: true });
    } catch (err) { toast(err.message, 6000); }
  } }, "New template from this…");
  fill(box,
    el("div", { class: "row" }, el("strong", {}, "Template"), select, edit, copy,
      el("span", { class: "muted small" }, template.source === "yours" ? `yours: ${template.path}` : `the Workspace's; yours go in ${list.dir}`)),
    template.description ? el("p", { class: "muted", style: "margin:0" }, template.description) : null,
    el("div", { class: "fields" }, fields),
    el("div", { class: "row" }, el("strong", {}, "Job script"), name, el("span", { class: "muted small" }, `in ${dirname(input)}`),
      el("label", { class: "check small" }, overwrite, "Replace one that exists")),
    el("div", { class: "row" }, handNote, rebuild),
    previewBox,
    el("div", { class: "row" }, submit, el("span", { class: "muted small" }, "Writes the job script there, then runs sbatch in that directory.")),
    result);
  await render();
}

// ------------------------------------------------------------------ disk usage

// A directory's folders by size (du) and by file count (du --inodes: files, folders and links, what a file quota
// counts), like the console's Storage tab, for any directory. Rows open the folder, or its own usage.
async function openUsage(dir) {
  const existing = state.tabs.find((t) => t.kind === "usage" && t.path === dir);
  if (existing) { activate(existing); return existing.refresh(); }
  const box = el("div", { class: "usage" });
  const tab = addTab({ kind: "usage", path: dir, label: `Usage: ${basename(dir) || "/"}`, view: el("div", {}, box) });
  let key = "kb", data = null;
  const draw = () => {
    if (!data) return;
    const kids = data.children.slice().sort((a, b) => (b[key] || 0) - (a[key] || 0));
    const top = kids.length ? kids[0][key] || 0 : 0;
    const total = key === "kb" ? data.kb : data.files;
    const pct = (v) => (total && v ? `${Math.round(100 * v / total)}%` : "");
    const value = (v, k) => (v === null || v === undefined ? "—" : k === "kb" ? size(v * 1024) : v.toLocaleString());
    const sortButton = (k, label) => el("button", { type: "button", class: k === key ? "primary" : null,
      "aria-pressed": String(k === key), onclick: () => { key = k; draw(); } }, label);
    fill(box,
      el("div", { class: "bar-row" },
        el("code", {}, data.path),
        el("strong", {}, value(data.kb, "kb")), el("span", { class: "muted" }, `${value(data.files, "files")} files`),
        sortButton("kb", "By size"), sortButton("files", "By files"),
        el("button", { type: "button", onclick: () => tab.refresh() }, "Scan again"),
        data.partial ? el("span", { class: "warn" }, `partial: the scan hit its ${data.limit} s limit`) : null,
        el("span", { class: "muted small push-right" }, `${data.seconds} s${data.count_method === "walk" ? " · counted by walking (du has no --inodes)" : ""}`)),
      el("table", { class: "usage-table" },
        el("thead", {}, el("tr", {}, el("th", {}, "Folder"), el("th", {}), el("th", { class: "num" }, "Size"),
          el("th", { class: "num" }, "Files"), el("th", { class: "num" }, ""), el("th", {}))),
        el("tbody", {}, kids.map((c) => el("tr", {},
          el("td", {}, el("a", { href: "#", title: `Open ${c.path}`, onclick: (e) => { e.preventDefault(); go(c.path); } }, c.name + "/")),
          el("td", { class: "bar-cell" }, el("div", { class: "bar", style: `width:${top ? Math.max(1, 100 * (c[key] || 0) / top) : 0}%` })),
          el("td", { class: `num${key === "kb" ? "" : " muted"}` }, value(c.kb, "kb")),
          el("td", { class: `num${key === "files" ? "" : " muted"}` }, value(c.files, "files")),
          el("td", { class: "num muted" }, pct(c[key])),
          el("td", {}, el("button", { type: "button", class: "small-button", onclick: () => openUsage(c.path) }, "Usage")))),
          data.here && data.here.files ? el("tr", { class: "muted" },
            el("td", {}, "(files directly here)"), el("td", {}),
            el("td", { class: "num" }, value(data.here.kb, "kb")), el("td", { class: "num" }, value(data.here.files, "files")),
            el("td", { class: "num" }, pct(data.here[key])), el("td", {})) : null)),
      kids.length ? null : el("p", { class: "muted" }, "No folders here."));
  };
  tab.refresh = async () => {
    fill(box, el("p", { class: "muted" }, `Scanning ${dir} (du, at low priority; up to two minutes)…`));
    try {
      data = await api(`/api/usage?${q({ path: dir })}`);
      draw();
    } catch (err) { fill(box, el("p", { class: "error-text" }, err.message)); }
  };
  await tab.refresh();
}

// ------------------------------------------------------------------ jobs

let jobsTimer = null;
async function refreshJobs() {
  clearTimeout(jobsTimer);
  const list = $("jobs-list");
  try {
    const { jobs, at } = await api("/api/jobs");
    $("jobs-at").textContent = new Date(at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    list.replaceChildren(jobs.length ? el("table", {}, el("tbody", {}, jobs.map((j) => el("tr", {},
      el("td", {}, j.id),
      el("td", {}, el("a", { href: "#", title: j.dir, onclick: (e) => { e.preventDefault(); if (j.dir) go(j.dir); } }, j.name)),
      el("td", { class: `state-${j.state}`, title: j.reason }, j.state.toLowerCase()),
      el("td", { class: "muted" }, `${j.time}/${j.limit}`),
      el("td", {}, el("button", { type: "button", class: "small-button danger", onclick: async () => {
        if (!await confirmAsk(`Cancel job ${j.id} (${j.name})?`, "Cancel job")) return;
        try { await api("/api/cancel", { method: "POST", body: { job: j.id } }); refreshJobs(); }
        catch (err) { toast(err.message, 6000); }
      } }, "Cancel")))))) : "None queued or running.");
  } catch (err) {
    list.textContent = err.message;
  }
  jobsTimer = setTimeout(refreshJobs, document.hidden ? 120000 : 30000);
}

// ------------------------------------------------------------------ terminals

function showTerminals(show = true) {
  $("term-panel").hidden = !show;
  if (show && state.activeTerm) setTimeout(() => state.activeTerm.fit(), 0);
}

async function newTerminal(cwd = state.dir) {
  showTerminals(true);
  const n = state.terminals.length + 1;
  const box = el("div", { class: "term" });
  $("term-views").append(box);
  const term = { label: `${basename(cwd) || "/"} (${n})`, box, fit: () => {}, cwd };
  state.terminals.push(term);
  activateTerm(term);
  let lib;
  try { lib = await xterm(); } catch (err) { lib = null; }
  if (!lib || !lib.Terminal) {
    box.append(el("p", { class: "note" }, `The terminal (xterm.js) didn't load from ${state.info.cdn}: check this machine's ` +
      "internet access, or set the Workspace's CDN to a mirror you can reach."));
    return;
  }
  const xt = new lib.Terminal({ cursorBlink: true, fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    fontSize: 13, scrollback: 5000, allowProposedApi: false,
    theme: dark() ? { background: "#111111" } : { background: "#111111" } });
  const fit = lib.FitAddon ? new lib.FitAddon() : null;
  if (fit) xt.loadAddon(fit);
  xt.open(box);
  term.xt = xt;
  term.fit = () => { try { fit && fit.fit(); } catch (e) { /* hidden */ } };
  term.fit();
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  let ticket;
  try { ticket = (await api("/api/term/ticket", { method: "POST" })).ticket; }
  catch (err) { xt.write(`\r\n[can't open the terminal: ${err.message}]\r\n`); term.ended = true; drawTermTabs(); return; }
  const ws = new WebSocket(`${proto}//${location.host}/terminal?${q({ ticket, cwd, cols: xt.cols, rows: xt.rows })}`);
  ws.binaryType = "arraybuffer";
  term.ws = ws;
  const enc = new TextEncoder();
  ws.onmessage = (m) => {
    if (typeof m.data === "string") {
      const msg = JSON.parse(m.data);
      if (msg.type === "exit") { xt.write(`\r\n[the shell ended (exit ${msg.code})]\r\n`); term.ended = true; drawTermTabs(); }
    } else xt.write(new Uint8Array(m.data));
  };
  ws.onclose = () => { if (!term.ended) { xt.write("\r\n[disconnected]\r\n"); term.ended = true; drawTermTabs(); } };
  const pending = [];                        // typed before the shell is there: sent once it is
  const send = (bytes) => { if (ws.readyState === 1) ws.send(bytes); else if (ws.readyState === 0) pending.push(bytes); };
  ws.onopen = () => { for (const b of pending.splice(0)) ws.send(b); };
  xt.onData((d) => send(enc.encode(d)));
  xt.onBinary((d) => send(Uint8Array.from(d, (c) => c.charCodeAt(0))));
  xt.onResize(({ cols, rows }) => { if (ws.readyState === 1) ws.send(JSON.stringify({ type: "resize", cols, rows })); });
  new ResizeObserver(() => { if (!box.hidden) term.fit(); }).observe(box);
  xt.focus();
}
function activateTerm(term) {
  state.activeTerm = term;
  for (const t of state.terminals) t.box.hidden = t !== term;
  drawTermTabs();
  if (term) setTimeout(() => { term.fit(); term.xt && term.xt.focus(); }, 0);
}
function closeTerm(term) {
  if (term.ws) term.ws.close();
  if (term.xt) term.xt.dispose();
  term.box.remove();
  state.terminals = state.terminals.filter((t) => t !== term);
  if (state.activeTerm === term) activateTerm(state.terminals[state.terminals.length - 1] || null);
  if (!state.terminals.length) showTerminals(false);
  drawTermTabs();
}
function drawTermTabs() {
  const tabs = $("term-tabs");
  const keep = [$("term-new"), $("term-hide")];
  tabs.replaceChildren(...state.terminals.map((t) => el("div", {
    class: `tab${t === state.activeTerm ? " active" : ""}`, title: t.cwd, onclick: () => activateTerm(t) },
    el("span", { class: "label" }, t.label + (t.ended ? " (ended)" : "")),
    el("button", { type: "button", class: "close", "aria-label": `Close terminal ${t.label}`,
                   onclick: (e) => { e.stopPropagation(); closeTerm(t); } }, "×"))), ...keep);
}

// ------------------------------------------------------------------ resizing

function dragResize(handle, onMove) {
  handle.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    const move = (ev) => onMove(ev);
    const up = () => { handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", up);
                       for (const t of state.tabs) t.editor && t.editor.refresh(); };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  });
}

// ------------------------------------------------------------------ start

async function start() {
  if (!token) {
    document.body.replaceChildren(el("p", { class: "note-box" }, "Open the Workspace from the console (its Open button " +
      "carries the token it needs)."));
    return;
  }
  try { state.info = await api("/api/info"); }
  catch (err) {
    document.body.replaceChildren(el("p", { class: "note-box error-text" }, err.status === 401
      ? "This Workspace's token is wrong or has changed (it was restarted): open it from the console again."
      : err.message));
    return;
  }
  const info = state.info;
  document.title = `Workspace · ${info.host.split(".")[0]}`;
  $("host").textContent = `${info.user}@${info.host}`;
  $("places").replaceChildren(el("option", { value: "" }, "Places"),
    ...info.places.map((p) => el("option", { value: p.path }, `${p.label}: ${p.path}`)));
  $("places").addEventListener("change", (e) => { if (e.target.value) go(e.target.value); e.target.value = ""; });
  $("go").addEventListener("submit", (e) => { e.preventDefault(); go($("path").value.trim() || info.home); });
  $("up").addEventListener("click", () => state.listing && state.listing.parent && go(state.listing.parent));
  $("refresh").addEventListener("click", () => go(state.dir));
  $("new-file").addEventListener("click", newFile);
  $("new-folder").addEventListener("click", newFolder);
  $("upload").addEventListener("click", () => $("upload-input").click());
  $("upload-input").addEventListener("change", (e) => { upload([...e.target.files]); e.target.value = ""; });
  $("hidden").addEventListener("change", (e) => { state.hidden = e.target.checked; go(state.dir); });
  $("terminal-here").addEventListener("click", () => newTerminal(state.dir));
  $("usage").addEventListener("click", () => openUsage(state.dir));
  $("term-new").addEventListener("click", () => newTerminal(state.dir));
  $("term-hide").addEventListener("click", () => showTerminals(false));
  $("jobs-refresh").addEventListener("click", refreshJobs);
  const files = $("files");
  files.addEventListener("dragover", (e) => { e.preventDefault(); files.classList.add("drop"); });
  files.addEventListener("dragleave", () => files.classList.remove("drop"));
  files.addEventListener("drop", (e) => {
    e.preventDefault();
    files.classList.remove("drop");
    if (e.dataTransfer.files.length) upload([...e.dataTransfer.files]);
  });
  dragResize($("splitter"), (e) => { $("side").style.width = `${Math.max(200, Math.min(e.clientX, innerWidth - 300))}px`; });
  dragResize($("term-splitter"), (e) => {
    $("term-panel").style.height = `${Math.max(120, Math.min(innerHeight - e.clientY, innerHeight - 160))}px`;
    if (state.activeTerm) state.activeTerm.fit();
  });
  const wanted = new URLSearchParams(location.hash.slice(1)).get("dir");
  await go(wanted || info.root);
  if (!state.listing) await go(info.home);
  refreshJobs();
  codemirror();          // start loading the editor
}
start();
