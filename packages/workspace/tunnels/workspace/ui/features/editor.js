// Opening a file: the text editor (<hpc-code>) for anything, and viewers for what the browser shows itself (images,
// video, audio, PDF, HTML). Registers the openers "text" and "viewer", and the actions "Open as text" and
// "Download".
//
//   Text: loads the file with its mtime and whether it is writable; Save (Ctrl or ⌘-S) sends the mtime back, so a file
//   somebody else changed asks before it is overwritten. Files above the server's limits, and binary files, say so
//   instead of loading.
//   Drafts: unsaved changes are kept in this browser (per user and host, up to 20 files of up to 1 MB each) a second
//   after the last keystroke, and put back when the file is opened again (after a reload, a crash, a closed tab), as long as
//   the file hasn't changed on the cluster since; if it has, the person is asked. Saving, reloading from disk and closing
//   the tab with "Close it anyway" drop the draft.
//   Viewer: the file's signed URL (client.js rawUrl) in an <img>, <video>, <audio> or <iframe>; an HTML page runs in a
//   sandboxed frame (the server's CSP sandbox adds to the frame's own).

import { el, toast, languageFor, remember } from "hpclib-ui/index.js";
import { api, q, rawUrl, basename, dirname, size, confirmAsk, download, fill } from "../client.js";
import { ws, barButton } from "../shell.js";

const IMAGE = /\.(png|jpe?g|gif|webp|bmp|ico|avif|tiff?|svg)$/i;
const VIDEO = /\.(mp4|m4v|webm|ogv|mov)$/i;
const AUDIO = /\.(mp3|wav|ogg|oga|flac|m4a|aac)$/i;
const PDF = /\.pdf$/i;
const HTML = /\.html?$/i;
const SOURCE_TOO = /\.(html?|svg)$/i;

function viewerKind(path) {
  if (IMAGE.test(path)) return "image";
  if (VIDEO.test(path)) return "video";
  if (AUDIO.test(path)) return "audio";
  if (PDF.test(path)) return "pdf";
  if (HTML.test(path)) return "html";
  return null;
}

// ------------------------------------------------------------------ drafts

const DRAFT_FILES = 20;
const DRAFT_BYTES = 1 << 20;
const draftsKey = () => ws.layoutKey("drafts");
const readDrafts = () => remember.get(draftsKey(), {});
const loadDraft = (path) => readDrafts()[path] || null;

function dropDraft(path) {
  const all = readDrafts();
  if (!(path in all)) return;
  delete all[path];
  remember.set(draftsKey(), all);
}

function saveDraft(path, text, mtime) {
  if (text.length > DRAFT_BYTES) return;                      // too large to keep in a browser's storage
  const all = readDrafts();
  delete all[path];                                           // the most recently changed last
  all[path] = { text, mtime, at: Date.now() };
  for (const old of Object.keys(all).slice(0, Math.max(0, Object.keys(all).length - DRAFT_FILES))) delete all[old];
  remember.set(draftsKey(), all);
}

/** The bar's buttons from the registry (an editor's "Submit job…"). */
const barActions = (path) => ws.actionsFor({ path, name: basename(path), dir: false }).filter((a) => a.bar)
  .map((a) => barButton(a.label, () => a.run({ path, name: basename(path), dir: false })));

// ------------------------------------------------------------------ viewers

async function openViewer(path) {
  const kind = viewerKind(path);
  let url;
  try { url = await rawUrl(path); } catch (err) { toast(err.message, { tone: "error" }); return null; }
  const body = el("div", { class: `body${kind === "pdf" || kind === "html" ? "" : " center"}` });
  if (kind === "image") body.append(el("img", { src: url, alt: basename(path) }));
  else if (kind === "video") body.append(el("video", { src: url, controls: true, preload: "metadata" }));
  else if (kind === "audio") body.append(el("audio", { src: url, controls: true }));
  else if (kind === "pdf") body.append(el("iframe", { src: url, title: basename(path) }));
  else body.append(el("iframe", { src: url, title: basename(path), sandbox: "allow-scripts allow-popups" }));
  const reload = async () => {
    try { url = await rawUrl(path); } catch (err) { toast(err.message, { tone: "error" }); return; }
    body.firstChild.src = `${url}?t=${Date.now()}`;
  };
  const bar = el("div", { class: "bar" },
    el("code", {}, path),
    barButton("Reload", reload),
    barButton("New browser tab ↗", () => window.open(url, "_blank", "noopener")),
    barButton("Download", () => download(path)),
    SOURCE_TOO.test(path) ? barButton("Edit source", () => ws.open(path, {}, { opener: "text" })) : null);
  return ws.tabs.add({ label: basename(path), title: path, path, view: el("div", {}, bar, body) });
}

// ------------------------------------------------------------------ text

async function openText(path, entry, ctx = {}) {
  const body = el("div", { class: "body" });
  const status = el("span", { class: "status" }, "Loading…");
  const bar = el("div", { class: "bar" }, el("code", {}, path), status);
  const tab = ws.tabs.add({ label: basename(path), title: path, path, view: el("div", {}, bar, body) });
  const fail = (err) => { status.textContent = err.message; status.classList.add("error"); };
  const limits = ws.info.limits;
  let st = entry;
  if (st.size === undefined) {
    try { st = await api(`/api/stat?${q({ path })}`); } catch (err) { fail(err); return tab; }
  }
  if (st.size > limits.edit_bytes) {
    body.replaceChildren(el("div", { class: "note-box" },
      el("p", {}, `${basename(path)} is ${size(st.size)}, more than the editor opens (${size(limits.edit_bytes)}).`),
      barButton("Download", () => download(path))));
    status.textContent = "";
    return tab;
  }
  if (st.size > limits.confirm_bytes) {
    status.textContent = "";
    await new Promise((resolve) => body.replaceChildren(el("div", { class: "note-box" },
      el("p", {}, `${basename(path)} is ${size(st.size)}: loading it into the editor may take a while.`),
      el("div", { class: "actions" }, barButton("Open it", resolve, { class: "primary" }),
        barButton("Download", () => download(path))))));
    status.textContent = "Loading…";
    body.replaceChildren();
  }
  await loadText(tab, body, bar, status, false);
  if (ctx.line && tab.goto) tab.goto(ctx.line);
  return tab;
}

async function loadText(tab, body, bar, status, force) {
  const path = tab.path;
  let res;
  try { res = await api(`/api/file?${q({ path })}`, { raw: true }); }
  catch (err) { status.textContent = err.message; status.classList.add("error"); return; }
  const bytes = new Uint8Array(await res.arrayBuffer());
  tab.mtime = res.headers.get("X-Mtime-Ns");
  const writable = res.headers.get("X-Writable") === "1";
  if (!force && bytes.subarray(0, 8192).includes(0)) {
    status.textContent = "";
    body.replaceChildren(el("div", { class: "note-box" }, el("p", {}, `${basename(path)} looks binary (${size(bytes.length)}).`),
      el("div", { class: "actions" }, barButton("Download", () => download(path)),
        barButton("Open as text anyway", () => { body.replaceChildren(); loadText(tab, body, bar, status, true); }))));
    return;
  }
  body.replaceChildren();
  const code = el("hpc-code", { language: languageFor(path) || "", readonly: !writable });
  body.append(code);
  await code.ready;
  code.value = new TextDecoder("utf-8").decode(bytes);
  code.markClean();
  const setDirty = (dirty) => { if (tab.dirty !== dirty) ws.tabs.update(tab.id, { dirty }); };
  let draftTimer = null;
  const keepDraft = () => {
    clearTimeout(draftTimer);
    draftTimer = setTimeout(() => (code.isClean ? dropDraft(path) : saveDraft(path, code.value, tab.mtime)), 1000);
  };
  tab.onclose = () => { clearTimeout(draftTimer); dropDraft(path); };
  const draft = writable && !force ? loadDraft(path) : null;
  if (draft && draft.text !== code.value) {
    const same = draft.mtime === tab.mtime;
    const when = new Date(draft.at).toLocaleString();
    if (same || await confirmAsk(`${basename(path)} changed on the cluster after you made the unsaved changes from ${when}. ` +
        "Put your changes back anyway? (Cancel keeps the file as it is now.)", "Restore")) {
      code.value = draft.text;
      setDirty(true);
      status.textContent = `restored the unsaved changes from ${when}`;
    } else dropDraft(path);
  } else if (draft) dropDraft(path);
  const save = async (overwrite = false) => {
    status.textContent = "Saving…";
    status.classList.remove("error");
    try {
      const out = await api(`/api/file?${q(overwrite ? { path } : { path, mtime_ns: tab.mtime || "" })}`,
                            { method: "PUT", body: code.value });
      tab.mtime = out.mtime_ns;
      code.markClean();
      setDirty(false);
      dropDraft(path);
      status.textContent = `Saved ${new Date().toLocaleTimeString()}`;
      ws.changed(dirname(path));
    } catch (err) {
      status.textContent = err.message;
      status.classList.add("error");
      if (err.status === 409 && await confirmAsk(`${basename(path)} changed on the cluster since you opened it. ` +
          "Overwrite it with yours? (Cancel keeps both: reload to see theirs.)", "Overwrite")) save(true);
    }
  };
  code.addEventListener("change", () => { setDirty(!code.isClean); keepDraft(); });
  code.addEventListener("save", () => save());
  tab.goto = (line) => code.goto(line);                      // a link to a line (#file=…&line=N)
  tab.onshow = () => { code.refresh(); code.focus(); };
  tab.onresize = () => code.refresh();
  const reload = async () => {
    if (tab.dirty && !await confirmAsk("Discard your changes and load the file again?", "Reload")) return;
    body.replaceChildren();
    setDirty(false);
    dropDraft(path);
    status.textContent = "";
    loadText(tab, body, bar, status, true);
  };
  if (!status.textContent.startsWith("restored")) status.textContent = writable ? size(bytes.length) : "read-only";
  fill(bar, el("code", {}, path),
    writable ? barButton("Save", () => save(), { class: "primary" }) : null,
    barButton("Reload", reload), ...barActions(path),
    code.plain ? el("span", { class: "muted small" }, "(plain editor: CodeMirror didn't load)") : null,
    status);
  if (tab === ws.tabs.active) code.focus();
}

// ------------------------------------------------------------------ registry

ws.registerOpener({ id: "text", match: () => 1, open: openText, restore: true });
ws.registerOpener({ id: "viewer", match: (e) => (!e.dir && viewerKind(e.path) ? 10 : 0), open: openViewer, restore: true });

ws.registerAction({ id: "open-text", label: "Open as text", group: "open", when: (e) => !e.dir,
  run: (e) => ws.open(e.path, e, { opener: "text" }) });
ws.registerAction({ id: "download", label: "Download", group: "transfer", when: (e) => !e.dir,
  run: (e) => download(e.path).catch((err) => toast(err.message, { tone: "error" })) });
