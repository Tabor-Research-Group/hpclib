// Agent Console front end. Talks only to the agent_console HTTP API (see
// hpclib/servers/agent_console.py); nothing in hpclib imports this folder.
//
// Serve it with   agent_console --static agent-console --open
// or from elsewhere with   agent_console --allow-origin http://127.0.0.1:8000
// and open   http://127.0.0.1:8000/?api=http://127.0.0.1:27180

// The page's building blocks are custom elements, in components.js (see its header).
import { configure, el, action, LOGIN_BUSY, LOGIN_LABEL, HpcClusterPicker } from "./components.js";

const params = new URLSearchParams(location.search);
const API = (params.get("api") || "").replace(/\/$/, "");
const main = document.getElementById("main");
const KEY_ITEM = "agent-console-key";
let key = null;
let followTimer = null;

// ---------------------------------------------------------------- session key

// Kept in localStorage, so links opened in new tabs (e.g. #/files?cluster=...&path=...) work too. Only pages
// from this console's own address can read it, and it only works while that console runs: a restart makes a
// new key, and the page asks again.
function storedKey() {
  try { return localStorage.getItem(KEY_ITEM) || sessionStorage.getItem(KEY_ITEM); } catch { return null; }
}
function storeKey(value) {
  key = value;
  try { localStorage.setItem(KEY_ITEM, value); } catch { /* memory only */ }
}
function takeKeyFromHash() {
  const m = location.hash.match(/^#key=([^&]+)/);
  if (!m) return;
  storeKey(decodeURIComponent(m[1]));
  history.replaceState(null, "", location.pathname + location.search + "#/clusters");
}
function askForKey() {
  const dialog = document.getElementById("key-dialog");
  if (!dialog.open) dialog.showModal();
}
document.getElementById("key-form").addEventListener("submit", (e) => {
  e.preventDefault();
  storeKey(document.getElementById("key-input").value.trim());
  document.getElementById("key-dialog").close();
  loadApps().then(render);
});

// ---------------------------------------------------------------- API

class ApiError extends Error {
  constructor(status, payload) {
    super(payload.error || `HTTP ${status}`);
    this.status = status;
    this.payload = payload;
  }
}

async function api(path, { method = "GET", body } = {}) {
  if (!key) { askForKey(); throw new ApiError(401, { error: "no session key yet" }); }
  const res = await fetch(API + "/api/" + path, {
    method,
    headers: { Authorization: `Bearer ${key}`, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  let payload = {};
  try { payload = await res.json(); } catch { /* empty or not JSON */ }
  if (res.status === 401 && !payload.cluster_status) askForKey();
  if (!res.ok) throw new ApiError(res.status, payload);
  return payload;
}

configure({ api });

const cluster = (name) => "clusters/" + encodeURIComponent(name);

// ---------------------------------------------------------------- DOM helpers

function errorBox(err) {
  return el("div", { class: "error-box" }, err.message || String(err));
}

function when(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  const ago = (Date.now() - d) / 1000;
  if (ago < 60) return "just now";
  if (ago < 3600) return `${Math.round(ago / 60)} min ago`;
  if (ago < 86400) return `${Math.round(ago / 3600)} h ago`;
  return d.toLocaleString();
}

function alertError(err) {
  main.prepend(el("hpc-panel", { dismissible: true }, errorBox(err)));
}

function diffView(text) {
  return el("pre", {}, text.split("\n").map((line) => el("span", {
    class: line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : null,
  }, line + "\n")));
}

// ---------------------------------------------------------------- pages

// ---------------------------------------------------------------- cluster login

function loginDialog(c, { onConnected = null, onClosed = render } = {}) {
  const password = el("input", { type: "password", autocomplete: "current-password", placeholder: "cluster password" });
  const status = el("p", { class: "muted" },
    `Logs in to ${c.host} once; the tunnel and other ssh commands reuse the login until it has been idle for ` +
    `${c.connection_hours} h. After the password, a Duo push goes to your phone.`);
  const go = el("button", { class: "primary", type: "submit" }, "Log in");
  const close = el("button", { type: "button" }, "Close");
  const form = el("form", {}, status, password, el("div", { class: "actions" }, go, close));
  const dialog = el("hpc-dialog", { heading: `Log in to ${c.name}`, transient: true }, form);
  let timer = null;
  let connected = false;
  const finish = () => dialog.close();
  dialog.addEventListener("close", async () => {   // Close, ×, Escape, or logged in
    clearInterval(timer);
    if (connected && onConnected) {
      try { await onConnected(); } catch (err) { alertError(err); }
    }
    onClosed();
  });
  close.addEventListener("click", finish);
  const show = (st) => {
    status.className = st.state === "failed" ? "error-box" : (st.state === "connected" ? "saved" : "muted");
    status.textContent = (st.message || LOGIN_LABEL[st.state] || st.state) + (st.prompt ? ` (asked: "${st.prompt}")` : "");
  };
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    go.disabled = password.disabled = true;
    try {
      const value = password.value;
      password.value = "";                          // don't keep it in the page
      show(await api(cluster(c.name) + "/login", { method: "POST", body: value ? { password: value } : {} }));
    } catch (err) {
      show({ state: "failed", message: err.message });
      go.disabled = password.disabled = false;
      return;
    }
    timer = setInterval(async () => {
      let st;
      try { st = await api(cluster(c.name) + "/login"); } catch (err) { st = { state: "failed", message: err.message }; }
      show(st);
      if (!LOGIN_BUSY.includes(st.state)) {
        clearInterval(timer);
        if (st.state === "connected") { connected = true; setTimeout(finish, 1200); }
        else go.disabled = password.disabled = false;
      }
    }, 1000);
  });
  dialog.showModal();
}

function setupForm(c, row) {
  // setup_agents' options, in the cluster's detail row; once started, the row shows its output
  const workDirs = el("textarea", { rows: 2, placeholder: "/scratch/user/me/llm" }, (c.work_dirs || []).join("\n"));
  const binds = el("textarea", { rows: 2, placeholder: "/software" }, (c.binds || []).join("\n"));
  const rebuild = el("input", { type: "checkbox" });
  const run = action(c.set_up ? "Run setup_agents again" : "Set up", async () => {
    const body = { work_dirs: lines(workDirs.value), binds: lines(binds.value), rebuild: rebuild.checked };
    await api(cluster(c.name) + "/setup", { method: "POST", body });
    await row.refresh();
  }, { primary: true });
  row.showDetail("setup", el("hpc-panel", { heading: `${c.set_up ? "Set up again" : "Set up"}: ${c.name}`, dismissible: true },
    el("p", { class: "muted" }, "Installs hpclib, the job templates and the sandboxed REST server config on the " +
      "cluster, and the agent's token and MCP entry here. Reruns keep templates, config and tokens, and add what's new."),
    el("div", { class: "row" },
      field("Work directories (agents may write here)", workDirs, "absolute paths on the cluster, one per line"),
      field("Extra read-only directories for jobs", binds, "e.g. a software tree, one per line")),
    el("label", {}, rebuild, " Rebuild: regenerate templates and config and replace the agent token (old copies are kept)"),
    el("div", { class: "actions" }, run)));
}

function addClusterForm(onAdded) {
  const host = el("input", { placeholder: "user@login.cluster.edu", size: 32 });
  const port = el("input", { type: "number", min: 1, max: 65535, placeholder: "22", size: 6 });
  const jump = el("input", { placeholder: "optional: [user@]jumphost", size: 24 });
  const work = el("input", { placeholder: "/scratch/user/me/llm", size: 32 });
  const binds = el("input", { placeholder: "optional: /software", size: 24 });
  const add = action("Add, then log in and set up", async () => {
    if (!work.value.trim()) throw new Error("give a work directory on the cluster");
    const c = await api("clusters", { method: "POST", body: { host: host.value.trim(),
      port: port.value ? Number(port.value) : null, jump: jump.value.trim() || null } });
    const setup = { work_dirs: [work.value.trim()], binds: words(binds.value) };
    loginDialog(c, { onConnected: () => api(cluster(c.name) + "/setup", { method: "POST", body: setup }) });
    onAdded();
    return `Added ${c.name}.`;
  }, { primary: true, result: true });
  return el("section", { class: "card" },
    el("h3", {}, "Add a cluster"),
    el("p", { class: "muted" }, "The console logs in (password, then a Duo push) and runs setup_agents over that login."),
    el("div", { class: "row" }, field("Login", host), field("Port", port), field("Jump host", jump)),
    el("div", { class: "row" }, field("Work directory for agents", work, "absolute path on the cluster"),
      field("Extra read-only directories", binds, "space-separated")),
    el("div", { class: "actions" }, add));
}

// ---------------------------------------------------------------- a tunnel asking for a password

let promptDialogOpen = false;

function hostKeyDialog(label, answerPath, prompt, { onCancel = null } = {}) {
  // the login node's ssh reaches a compute node it has no key for, and asks whether to trust it
  promptDialogOpen = true;
  const node = prompt.host || "the compute node";
  const status = el("p", {}, `The cluster's login node is connecting to ${node}` +
    `${prompt.address ? ` (${prompt.address})` : ""}, the node your job got, for the first time, and ssh doesn't ` +
    "know its key yet. Trusting it adds the key to ~/.ssh/known_hosts on the cluster, as typing yes in a terminal does.");
  const key = el("p", {}, el("code", {}, `${prompt.key_type || "?"} key ${prompt.fingerprint || "(fingerprint not shown)"}`));
  const accept = el("button", { class: "primary", type: "button" }, "Trust this key");
  const reject = el("button", { type: "button" }, "Don't connect");
  const later = el("button", { type: "button" }, "Not now");
  const dialog = el("hpc-dialog", { heading: `New compute node: ${label}`, transient: true },
    status, key, el("div", { class: "actions" }, accept, reject, later),
    el("p", { class: "muted small" }, "To check the key, run ssh-keyscan " + node + " | ssh-keygen -lf - on the " +
      "login node, or compare with your cluster's published fingerprints. Every new node asks once; to accept " +
      "new compute nodes' keys without asking (a changed key is still refused), put " +
      "HPCLIB_COMPUTE_HOST_KEYS=accept-new in ~/.local/tunnels/config.sh on the cluster."));
  let answered = false;
  dialog.addEventListener("close", () => {
    promptDialogOpen = false;
    if (!answered && onCancel) onCancel();
  });
  later.addEventListener("click", () => dialog.close());
  const send = (answer) => async () => {
    accept.disabled = reject.disabled = true;
    try {
      await api(answerPath, { method: "POST", body: { answer } });
      answered = true;
      dialog.close();
      checkPrompts(1500);            // a password may come next
    } catch (err) {
      status.className = "error-box";
      status.textContent = err.message;
      accept.disabled = reject.disabled = false;
    }
  };
  accept.addEventListener("click", send("yes"));
  reject.addEventListener("click", send("no"));
  dialog.showModal();
}

function promptDialog(label, answerPath, prompt, { onCancel = null } = {}) {
  // the tunnel's ssh (usually the login node's ssh to the compute node) waits for a password
  if (promptDialogOpen) return;
  if (prompt.kind === "hostkey") return hostKeyDialog(label, answerPath, prompt, { onCancel });
  promptDialogOpen = true;
  const input = el("input", { type: "password", autocomplete: "off", placeholder: "password" });
  const status = el("p", { class: prompt.retry ? "error-box" : "muted" },
    prompt.retry ? "That password was refused; try again." : prompt.note ? prompt.note :
      `${prompt.host ? `The cluster's login node is connecting to ${prompt.host.split("@")[1]}, the compute node ` +
        "your job got, and it" : "The tunnel's ssh"} asks for your password again.`);
  const go = el("button", { class: "primary", type: "submit" }, "Send");
  const cancel = el("button", { type: "button" }, "Not now");
  const form = el("form", {}, status,
    el("p", {}, el("code", {}, prompt.text)), input, el("div", { class: "actions" }, go, cancel),
    prompt.note ? null : el("p", { class: "muted small" }, "Tip: if your cluster allows it, ssh keys between its nodes skip this step: " +
      "on the cluster, ssh-keygen -t ed25519 (press Enter for no passphrase), then add ~/.ssh/id_ed25519.pub " +
      "to ~/.ssh/authorized_keys. Some clusters don't allow it; then this prompt is the way."));
  const dialog = el("hpc-dialog", { heading: `${prompt.note ? "Password" : "Second login"}: ${label}`, transient: true }, form);
  let answered = false;
  const close = () => dialog.close();
  dialog.addEventListener("close", () => {
    promptDialogOpen = false;
    if (!answered && onCancel) onCancel();
  });
  cancel.addEventListener("click", close);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const value = input.value;
    input.value = "";
    go.disabled = true;
    try {
      await api(answerPath, { method: "POST", body: { answer: value } });
      answered = true;
      close();
      checkPrompts(1500);            // a refused password asks again
    } catch (err) {
      status.className = "error-box";
      status.textContent = err.message;
      go.disabled = false;
    }
  });
  dialog.showModal();
}

async function clustersPage() {
  const [{ clusters }, health] = await Promise.all([api("clusters"), api("health")]);
  const local = health.hpclib_version;
  const addArea = el("div");
  const addBtn = el("button", { type: "button" }, "Add cluster");
  addBtn.addEventListener("click", () => {
    addArea.replaceChildren(addArea.firstChild ? "" : addClusterForm(() => {}));
  });
  // each cluster's rows refresh themselves (components.js), so this page is drawn once
  const rows = clusters.map((c) => {
    const row = el("hpc-cluster-row", { cluster: c.name, local });
    row.data = c;
    return row;
  });
  return [
    el("div", { class: "toolbar" }, el("h2", {}, "Clusters"), addBtn,
      el("span", { class: "muted" }, local ? `hpclib ${local} on this machine` : "")),
    addArea,
    clusters.length ? el("table", { class: "rows" },
      el("thead", {}, el("tr", {}, ["Cluster", "Tunnel", "Login", "Port", "Token", ""].map((h) => el("th", {}, h)))),
      rows)
      : el("p", { class: "muted" }, "No clusters yet; add one."),
  ];
}

async function proposalsPage() {
  const { proposals, clusters } = await api("proposals");
  setBadge(proposals.length);
  const notes = Object.entries(clusters).filter(([, s]) => s.error)
    .map(([name, s]) => el("div", { class: "muted" }, `${name}: ${s.error}`));
  const rows = proposals.map((p) => {
    const base = cluster(p.cluster) + "/rest/admin/proposals";
    const detailRow = el("tr", { hidden: true }, el("td", { colspan: 6 }));
    const diff = action("Diff", async () => {
      if (!detailRow.hidden) { detailRow.hidden = true; return; }
      const d = await api(base + "/diff?name=" + encodeURIComponent(p.name));
      const files = Object.entries(d.diff);
      detailRow.firstChild.replaceChildren(el("div", { class: "detail" },
        d.valid ? null : errorBox(new Error(`not valid: ${d.error}`)),
        files.length ? files.map(([f, text]) => [el("div", { class: "muted" }, f), diffView(text)])
          : el("p", { class: "muted" }, "Identical to the current template.")));
      detailRow.hidden = false;
    });
    const approve = action(p.replaces_existing ? "Approve, replacing" : "Approve", async () => {
      await api(base + "/approve", { method: "POST", body: { name: p.name, replace: !!p.replaces_existing } });
      render();
    }, { primary: true });
    const reject = action("Reject", async () => {
      const reason = prompt(`Reject ${p.name}? Reason (shown in the cluster's record):`, "");
      if (reason === null) return;
      await api(base + "/reject", { method: "POST", body: { name: p.name, reason } });
      render();
    });
    return [
      el("tr", {},
        el("td", {}, p.cluster),
        el("td", {}, el("strong", {}, p.name), p.revised ? el("div", { class: "muted" }, "revised") : null),
        el("td", {}, p.token),
        el("td", { class: "num" }, when(p.proposed)),
        el("td", {}, p.rationale || el("span", { class: "muted" }, "none given")),
        el("td", {}, el("div", { class: "actions" }, diff, approve, reject))),
      detailRow,
    ];
  });
  return [
    el("h2", {}, "Proposals"),
    notes,
    proposals.length ? el("table", {},
      el("thead", {}, el("tr", {}, ["Cluster", "Template", "From", "Proposed", "Rationale", ""]
        .map((h) => el("th", {}, h)))),
      el("tbody", {}, rows.flat()))
      : el("p", { class: "muted" }, "Nothing waiting for review."),
  ];
}

async function activityPage() {
  const { clusters } = await api("clusters");
  const pick = el("hpc-cluster-picker", { live: true });
  pick.clusters = clusters;
  if (!pick.options.length) {
    return [el("h2", {}, "Activity"), el("p", { class: "muted" }, "No cluster with a live tunnel and an owner token.")];
  }
  const token = el("input", { placeholder: "token name", size: 14 });
  const errorsOnly = el("input", { type: "checkbox" });
  const follow = el("input", { type: "checkbox" });
  const body = el("tbody");
  const status = el("span", { class: "muted" });
  let entries = [];
  let latest = null;

  const draw = () => {
    const shown = entries.filter((e) => !errorsOnly.checked || (e.status || 0) >= 400);
    body.replaceChildren(...shown.slice().reverse().map((e) => el("tr", {},
      el("td", { class: "num" }, new Date(e.time * 1000).toLocaleString()),
      el("td", {}, e.token || ""),
      el("td", {}, `${e.verb} ${e.path}`),
      el("td", { class: "num" }, (e.status || 0) >= 400 ? el("hpc-state", { tone: "error", label: e.status }) : (e.status ?? "")),
      el("td", { class: "muted" }, e.detail ? JSON.stringify(e.detail) : ""))));
    status.textContent = `${shown.length} entries`;
  };
  const query = (since) => {
    const q = new URLSearchParams({ limit: "500" });
    if (token.value.trim()) q.set("token", token.value.trim());
    if (since) q.set("since", since);
    return cluster(pick.value) + "/rest/admin/audit?" + q;
  };
  const load = async () => {
    const out = await api(query(null));
    entries = out.entries;
    latest = out.latest;
    draw();
  };
  const poll = async () => {
    if (!document.body.contains(body)) return stopFollowing();
    try {
      const out = await api(query(latest));
      if (out.entries.length) {
        entries = entries.concat(out.entries).slice(-2000);
        latest = out.latest;
        draw();
      }
    } catch (err) { status.textContent = err.message; }
  };
  pick.addEventListener("change", load);
  token.addEventListener("change", load);
  errorsOnly.addEventListener("change", draw);
  follow.addEventListener("change", () => {
    stopFollowing();
    if (follow.checked) followTimer = setInterval(poll, 5000);
  });
  await load();
  return [
    el("h2", {}, "Activity"),
    el("div", { class: "toolbar" }, pick, token,
      el("label", {}, errorsOnly, " errors only"),
      el("label", {}, follow, " follow"), status),
    el("table", {},
      el("thead", {}, el("tr", {}, ["Time", "Token", "Request", "Status", "Detail"].map((h) => el("th", {}, h)))),
      body),
  ];
}

// ---------------------------------------------------------------- files

const MB = 1 << 20;
const TEXT_LIMIT = 5 * MB;       // text shown whole up to this; larger: the first 1 MB
const VIEW_LIMIT = 25 * MB;      // images, SVG and HTML shown up to this
const DOWNLOAD_WARN = 25 * MB;   // downloads pass through the browser's memory; ask above this
const IMAGE_TYPES = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp",
                      bmp: "image/bmp", svg: "image/svg+xml" };
const filesBase = {};   // each cluster's top directory (its base_dir)

// The Files page's state lives in the address, so it can be linked to:
//   #/files?cluster=NAME&path=/a/directory   that directory
//   #/files?cluster=NAME&path=/a/file.log    its directory, with the file open in the viewer
// NAME is the profile name, the login host or the MCP name.
function filesQuery() {
  return new URLSearchParams(location.hash.split("?")[1] || "");
}
function filesHash(clusterName, path) {
  const q = new URLSearchParams({ cluster: clusterName });
  if (path) q.set("path", path);
  return "#/files?" + q.toString();
}

const ext = (name) => (name.match(/\.([^.]+)$/) || [])[1]?.toLowerCase() || "";
const kindOf = (name) => ext(name) === "svg" ? "image" : (IMAGE_TYPES[ext(name)] ? "image" :
  (["html", "htm"].includes(ext(name)) ? "html" : "text"));

function size(n) {
  if (n === null || n === undefined) return "";
  if (n < 1024) return `${n} B`;
  if (n < MB) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * MB) return `${(n / MB).toFixed(1)} MB`;
  return `${(n / 1024 / MB).toFixed(2)} GB`;
}

async function fetchFile(c, path) {
  // the file's bytes, through the console (which adds the cluster's token) with the session key
  const res = await fetch(`${API}/api/${cluster(c)}/rest/files/content?path=${encodeURIComponent(path)}`,
                          { headers: { Authorization: `Bearer ${key}` } });
  if (!res.ok) {
    let payload = {};
    try { payload = await res.json(); } catch { /* not JSON */ }
    throw new ApiError(res.status, payload);
  }
  return res.blob();
}

async function downloadFile(c, entry) {
  if (entry.size > DOWNLOAD_WARN && !confirm(
      `${entry.name} is ${size(entry.size)}. The browser keeps the whole file in memory while it downloads, ` +
      `which can be slow or fail for large files; psync or the agent's pull_files copy large files better.\n\n` +
      `Download it anyway?`)) {
    return;
  }
  const blob = await fetchFile(c, entry.path);
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: entry.name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

async function viewFile(c, entry, box) {
  const download = action("Download", () => downloadFile(c, entry), { slot: "actions" });
  history.replaceState(null, "", filesHash(c, entry.path));   // a link to this file
  const card = el("hpc-panel", { class: "viewer", heading: entry.name, dismissible: true,
                                 status: `${size(entry.size)} · ${when(entry.mtime)}` },
    download, el("p", { class: "muted" }, "Loading…"));
  card.addEventListener("dismiss", (e) => {   // × closes the file and links to its folder again
    e.preventDefault();
    box.replaceChildren();
    history.replaceState(null, "", filesHash(c, entry.path.replace(/\/[^/]+$/, "") || "/"));
  });
  box.replaceChildren(card);
  const body = (...nodes) => card.replaceChildren(download, ...nodes);
  const kind = kindOf(entry.name);
  try {
    if (kind !== "text" && entry.size > VIEW_LIMIT) {
      body(el("p", { class: "muted" }, `Too large to show here (${size(entry.size)}); download it instead.`));
    } else if (kind === "image") {
      const blob = await fetchFile(c, entry.path);
      // typed by its name, and shown with <img>: an SVG's scripts don't run there
      const url = URL.createObjectURL(new Blob([blob], { type: IMAGE_TYPES[ext(entry.name)] }));
      const img = el("img", { src: url, alt: entry.name });
      img.addEventListener("load", () => setTimeout(() => URL.revokeObjectURL(url), 1000));
      body(img);
    } else if (kind === "html") {
      const text = await (await fetchFile(c, entry.path)).text();
      // a sandboxed frame: no scripts unless asked, and never the console's origin, storage or session key
      const frame = el("iframe", { sandbox: "", title: entry.name });
      frame.srcdoc = text;
      const scripts = el("input", { type: "checkbox" });
      scripts.addEventListener("change", () => {
        frame.setAttribute("sandbox", scripts.checked ? "allow-scripts" : "");
        frame.srcdoc = text;
      });
      body(el("label", { class: "muted" }, scripts,
        " Run its scripts (still sandboxed: it can't reach the console or your session). Files it links to " +
        "by relative path don't load here."), frame);
    } else if (entry.size <= TEXT_LIMIT) {
      const bytes = new Uint8Array(await (await fetchFile(c, entry.path)).arrayBuffer());
      if (bytes.subarray(0, 8192).includes(0)) {
        body(el("p", { class: "muted" }, "A binary file; download it to open it."));
      } else {
        body(el("pre", { class: "file" }, new TextDecoder().decode(bytes)));
      }
    } else {
      const part = await api(`${cluster(c)}/rest/files/read?path=${encodeURIComponent(entry.path)}&length=${MB}`);
      body(...(part.binary ? [el("p", { class: "muted" }, "A binary file; download it to open it.")] : [
        el("p", { class: "muted" }, `The first ${size(part.length)} of ${size(part.size)}; download it for the rest.`),
        el("pre", { class: "file" }, part.text)]));
    }
  } catch (err) {
    body(errorBox(err));
  }
}

async function filesPage() {
  const { clusters } = await api("clusters");
  const q = filesQuery();
  const legacy = decodeURIComponent((location.hash.match(/^#\/files\/([^?]+)/) || [])[1] || "");
  const wanted = q.get("cluster") || legacy;
  const pick = el("hpc-cluster-picker", { live: true, value: wanted || null });
  pick.clusters = clusters;
  pick.addEventListener("change", () => { location.hash = filesHash(pick.value); });
  const matches = (x) => HpcClusterPicker.matches(x, wanted);
  if (wanted && !pick.options.some(matches)) {
    const known = clusters.find(matches);
    return [el("h2", {}, "Files"), errorBox(new Error(known
      ? `${known.name}: start its tunnel (and log in) on the Clusters page to browse its files.`
      : `No cluster called ${wanted}.`))];
  }
  const c = pick.selected;
  if (!c) {
    return [el("h2", {}, "Files"), el("p", { class: "muted" }, "No cluster with a running tunnel and an owner token.")];
  }
  const files = (path) => api(`${cluster(c.name)}/rest/files?path=${encodeURIComponent(path)}`);
  if (!filesBase[c.name]) filesBase[c.name] = (await files(".")).path;
  const base = filesBase[c.name];

  // a path to a file: show its directory and open the file
  let listing = await files(q.get("path") || base);
  let open = null;
  if (listing.type !== "directory") {
    open = listing;
    listing = await files(listing.path.replace(/\/[^/]+$/, "") || "/");
  }
  history.replaceState(null, "", filesHash(c.name, open ? open.path : listing.path));

  const go = (path) => { location.hash = filesHash(c.name, path); };
  const atBase = listing.path === base;
  const up = el("button", { type: "button", disabled: atBase }, "Up");
  up.addEventListener("click", () => go(listing.path.replace(/\/[^/]+\/?$/, "") || "/"));
  const home = el("button", { type: "button", disabled: atBase }, "Top");
  home.addEventListener("click", () => go(base));
  const viewer = el("div");

  const entries = listing.entries.slice().sort((a, b) =>
    (a.type === "directory" ? 0 : 1) - (b.type === "directory" ? 0 : 1) || a.name.localeCompare(b.name));
  const rows = entries.map((e) => {
    const isDir = e.type === "directory";
    const name = isDir ? el("a", { href: filesHash(c.name, e.path), class: "dir" }, e.name + "/")
                       : el("span", {}, e.name);
    const actions = el("div", { class: "actions" });
    if (e.type === "file") {
      const view = el("button", {}, "View");
      view.addEventListener("click", () => viewFile(c.name, e, viewer));
      actions.append(view, action("Download", () => downloadFile(c.name, e)));
    }
    return el("tr", {}, el("td", {}, name), el("td", { class: "num" }, isDir ? "" : size(e.size)),
      el("td", { class: "num" }, when(e.mtime)), el("td", {}, actions));
  });
  if (open) viewFile(c.name, open, viewer);   // same size rules as View: large files aren't loaded
  return [
    el("h2", {}, "Files"),
    el("div", { class: "toolbar" }, pick, home, up, el("code", {}, listing.path)),
    viewer,
    entries.length ? el("table", {},
      el("thead", {}, el("tr", {}, ["Name", "Size", "Modified", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, rows)) : el("p", { class: "muted" }, "Empty directory."),
  ];
}

// ---------------------------------------------------------------- settings

const TUNNEL_FIELDS = [
  ["time", "Time limit", "e.g. 12:00:00 or 1-00:00:00 (default 8:00:00)"],
  ["mem", "Memory", "e.g. 2gb (default 1gb)"],
  ["partition", "Partition", "default: the cluster's"],
];
const LIMIT_FIELDS = [
  ["max_time", "Longest job", "text", "1-00:00:00"],
  ["max_mem", "Most memory per job", "text", "128G"],
  ["max_cpus", "Most CPUs per job", "int"],
  ["max_nodes", "Most nodes per job", "int"],
  ["max_gpus", "Most GPUs per job", "int"],
  ["max_concurrent_jobs", "Jobs at once", "int"],
  ["max_array_tasks", "Tasks per array", "int"],
  ["partitions", "Allowed partitions", "list"],
];

const ENV_SCOPES = [
  ["all", "Jobs and syncs", "e.g. a license server"],
  ["jobs", "Template jobs only", "e.g. OMP_STACKSIZE=512M"],
  ["syncs", "Environment syncs only", "e.g. UV_INDEX_URL or HTTPS_PROXY"],
];
const words = (text) => text.split(/[\s,]+/).map((w) => w.trim()).filter(Boolean);
const lines = (text) => text.split("\n").map((w) => w.trim()).filter(Boolean);

function field(label, input, hint) {
  return el("hpc-field", { label, hint }, input);
}

function saveButton(label, fn) {
  // a primary button whose action's message ("Saved.") or error shows beside it
  return action(label, fn, { primary: true, result: true });
}

async function tunnelSettings(c) {
  const s = await api(cluster(c.name) + "/settings");
  const mode = el("select", {}, s.modes.map((m) => el("option", { value: m, selected: m === s.auto_approve_templates },
    { all: "Approve every valid proposal", new: "Approve new templates; review replacements",
      review: "Review every proposal" }[m] || m)));
  const given = Object.fromEntries(s.tunnel_args.map((a) => a.replace(/^--/, "").split(/=(.*)/s).slice(0, 2)));
  const inputs = Object.fromEntries(TUNNEL_FIELDS.map(([k]) => [k, el("input", { value: given[k] || "", size: 16 })]));
  const other = s.tunnel_args.filter((a) => !TUNNEL_FIELDS.some(([k]) => a.startsWith(`--${k}=`)));
  const hours = el("input", { type: "number", min: 1, max: 168, value: s.connection_hours, size: 6 });
  const save = saveButton("Save tunnel settings", async () => {
    const args = TUNNEL_FIELDS.filter(([k]) => inputs[k].value.trim()).map(([k]) => `--${k}=${inputs[k].value.trim()}`)
      .concat(other);
    await api(cluster(c.name) + "/settings", { method: "PUT", body: {
      auto_approve_templates: mode.value, tunnel_args: args, connection_hours: Number(hours.value) } });
    return "Saved. Applies the next time the tunnel starts (the login hours: the next time you log in).";
  });
  return el("section", { class: "card" },
    el("h3", {}, "Tunnel ", el("span", { class: "muted" }, "· kept on this machine")),
    field("Template proposals", mode, "only while jobs are sandboxed"),
    field("Keep the ssh login for (hours)", hours, "after it was last used; 1 to 168, default 12"),
    el("div", { class: "row" }, TUNNEL_FIELDS.map(([k, label, hint]) => field(`Tunnel job: ${label}`, inputs[k], hint))),
    other.length ? el("p", { class: "muted" }, `Also: ${other.join(" ")}`) : null,
    el("div", { class: "actions" }, save));
}

async function serverSettings(c) {
  const head = el("h3", {}, "Server ", el("span", { class: "muted" }, "· config.json on the cluster"));
  if (!c.tunnel || c.tunnel.state !== "up") {
    return el("section", { class: "card" }, head, el("p", { class: "muted" }, "Start the tunnel to see and change the server's settings."));
  }
  if (!c.has_owner_token) {
    return el("section", { class: "card" }, head, el("p", { class: "muted" }, "Needs this cluster's owner token (setup_agents keeps it in the profile)."));
  }
  const base = cluster(c.name) + "/rest/admin/config";
  const conf = await api(base);
  const raw = conf.config;
  const eff = conf.effective;
  const env = raw.environments || {};
  const sandbox = raw.sandbox || {};
  const limits = Object.assign({}, eff.limits, raw.limits || {});

  const syncModules = el("input", { value: (env.modules || []).join(" "), size: 40, placeholder: "e.g. WebProxy" });
  const uvPath = el("input", { value: env.uv === null ? "off" : (env.uv || "auto"), size: 24 });
  const pixiPath = el("input", { value: env.pixi === null ? "off" : (env.pixi || "auto"), size: 24 });
  const timeout = el("input", { type: "number", min: 1, value: env.timeout || 1800, size: 8 });
  const limitInputs = Object.fromEntries(LIMIT_FIELDS.map(([k, , kind, ph]) => {
    const v = limits[k];
    const value = v === null || v === undefined ? "" : (kind === "list" ? v.join(" ") : v);
    return [k, el("input", { value, size: 12, placeholder: kind === "list" ? "any" : (ph || "no cap"),
                             type: kind === "int" ? "number" : "text", min: kind === "int" ? 0 : null })];
  }));
  const binds = el("textarea", { rows: 3 }, (sandbox.binds || []).join("\n"));
  const writable = el("textarea", { rows: 2 }, (sandbox.writable || []).join("\n"));
  const notes = el("textarea", { rows: 4 }, raw.cluster_notes || "");
  const envBoxes = Object.fromEntries(ENV_SCOPES.map(([k, label]) => {
    const box = el("hpc-env-editor", { label });
    box.rules = (conf.rules || {}).environment || null;   // older servers don't send them; they still check on save
    box.value = (raw.environment || {})[k];
    return [k, box];
  }));

  const tool = (text) => { const t = text.trim(); return t === "off" ? null : (t || "auto"); };
  const collect = () => {
    const lim = {};
    for (const [k, , kind] of LIMIT_FIELDS) {
      const v = limitInputs[k].value.trim();
      if (kind === "list") lim[k] = v ? words(v) : null;
      else if (v === "") lim[k] = null;
      else lim[k] = kind === "int" ? Number(v) : v;
    }
    // defaults are left out, so the file only records what was chosen
    const environments = Object.assign({}, env, { modules: words(syncModules.value), uv: tool(uvPath.value),
                                                  pixi: tool(pixiPath.value), timeout: Number(timeout.value) || 1800 });
    for (const [k, dflt] of [["uv", "auto"], ["pixi", "auto"], ["timeout", 1800]]) {
      if (environments[k] === dflt) delete environments[k];
    }
    if (!environments.modules.length) delete environments.modules;
    const environment = {};
    for (const [k] of ENV_SCOPES) {
      const vars = envBoxes[k].value;          // throws, naming the box and line, if one is wrong
      if (Object.keys(vars).length) environment[k] = vars;
    }
    return {
      environments,
      environment: Object.keys(environment).length ? environment : null,
      limits: lim,
      sandbox: Object.assign({}, sandbox, { binds: lines(binds.value), writable: lines(writable.value) }),
      cluster_notes: notes.value.trim() || null,
    };
  };
  let shown = collect();   // only sections edited since then are sent
  const save = saveButton("Save server settings", async () => {
    const wanted = collect();
    const changes = {};
    for (const [k, v] of Object.entries(wanted)) {
      if (JSON.stringify(v) !== JSON.stringify(shown[k])) changes[k] = v;
    }
    if (!Object.keys(changes).length) return "Nothing changed.";
    const res = await api(base, { method: "PUT", body: { changes } });
    shown = wanted;
    return `Saved ${res.changed.join(", ")}; in effect now.${res.backup ? ` The previous file is ${res.backup}.` : ""}` +
      (res.warning ? ` Warning: ${res.warning}.` : "");
  });

  const jsonBox = el("textarea", { rows: 14, class: "code" },
    JSON.stringify(Object.fromEntries(conf.editable.filter((k) => k in raw).map((k) => [k, raw[k]])), null, 2));
  const saveJson = saveButton("Save JSON", async () => {
    let parsed;
    try { parsed = JSON.parse(jsonBox.value); } catch (e) { throw new Error(`not valid JSON: ${e.message}`); }
    const changes = Object.fromEntries(conf.editable.map((k) => [k, k in parsed ? parsed[k] : null]));
    const res = await api(base, { method: "PUT", body: { changes } });
    return `Saved; in effect now.${res.backup ? ` The previous file is ${res.backup}.` : ""}`;
  });

  const managers = (eff.environments || {}).managers || {};
  const found = Object.entries(managers).map(([m, i]) => `${m} ${i.available ? i.version || "" : "not found"}`).join(" · ");
  return el("section", { class: "card" }, head,
    el("p", { class: "muted" }, conf.path),
    el("h4", {}, "Python environments ", el("span", { class: "muted" }, found)),
    field("Modules to load before a sync", syncModules, "space-separated, e.g. a web proxy module the node needs for PyPI"),
    el("div", { class: "row" },
      field("uv", uvPath, "auto, a path, or off"),
      field("pixi", pixiPath, "auto, a path, or off"),
      field("Sync time limit (s)", timeout)),
    el("h4", {}, "Job limits"),
    el("div", { class: "row" }, LIMIT_FIELDS.map(([k, label, kind]) =>
      field(label, limitInputs[k], kind === "list" ? "space-separated; empty for any" : "empty for no cap"))),
    el("h4", {}, "Sandbox ", el("span", { class: "muted" }, (eff.sandbox || {}).effective || "")),
    el("div", { class: "row" },
      field("Extra read-only directories", binds, "one per line, e.g. /software"),
      field("Extra writable directories", writable, "one per line; besides the token's own")),
    el("h4", {}, "Environment variables"),
    el("p", { class: "muted" }, "Set in every template job (after its modules load) and every environment sync. " +
      "Agents see the names in cluster_info, and a job can print the values; don't put secrets here that " +
      "agents shouldn't read. PATH, LD_*, SINGULARITY*, SLURM_*, HPC_* and the like are refused."),
    el("div", { class: "row" }, ENV_SCOPES.map(([k, label, hint]) => field(label, envBoxes[k], hint))),
    el("h4", {}, "Notes for agents"),
    field("Cluster notes", notes, "shown to agents in cluster_info"),
    el("div", { class: "actions" }, save),
    el("details", {}, el("summary", {}, "Edit the editable sections as JSON"), jsonBox,
      el("div", { class: "actions" }, saveJson)));
}

async function settingsPage() {
  const { clusters } = await api("clusters");
  if (!clusters.length) return [el("h2", {}, "Settings"), el("p", { class: "muted" }, "No agent profiles yet; run setup_agents.")];
  const wanted = decodeURIComponent((location.hash.match(/^#\/settings\/(.+)$/) || [])[1] || "");
  const c = clusters.find((x) => x.name === wanted) || clusters[0];
  const pick = el("hpc-cluster-picker", { value: c.name });
  pick.clusters = clusters;
  pick.addEventListener("change", () => { location.hash = "#/settings/" + encodeURIComponent(pick.value); });
  const sections = await Promise.all([tunnelSettings(c), serverSettings(c).catch(errorBox)]);
  return [el("h2", {}, "Settings"), el("div", { class: "toolbar" }, pick), sections];
}

// ---------------------------------------------------------------- JupyterLab

// sbatch options for the tunnel's job; empty: what its tunnel_config.sh asks for
const APP_TUNNEL_FIELDS = [["time", "Time limit", "e.g. 12:00:00"], ["mem", "Memory", "e.g. 16gb"],
                           ["cpus-per-task", "CPUs", "e.g. 4"], ["partition", "Partition", ""]];

async function appSettingsForm(app, s, row) {
  // the tunnel job's sbatch options, JupyterLab's environment fields, and the tunnel's own settings (paths and the
  // like, saved on the cluster for its job and install.sh)
  const path = `apps/${app}/${encodeURIComponent(s.cluster)}/settings`;
  const conf = await api(path);
  const given = Object.fromEntries(conf.tunnel_args.map((a) => a.replace(/^--/, "").split(/=(.*)/s).slice(0, 2)));
  const inputs = Object.fromEntries(APP_TUNNEL_FIELDS.map(([k]) => [k, el("input", { value: given[k] || "", size: 12 })]));
  const other = conf.tunnel_args.filter((a) => !APP_TUNNEL_FIELDS.some(([k]) => a.startsWith(`--${k}=`)));
  const conda = el("input", { value: conf.conda_env === null ? "" : (conf.conda_env === "" ? "none" : conf.conda_env),
                              placeholder: "default", size: 16 });
  const modules = el("input", { value: conf.modules.join(" "), placeholder: "e.g. JupyterLab/4.2.0", size: 32 });
  const project = el("input", { value: conf.project, placeholder: "/scratch/user/me/llm/my-project", size: 40 });
  const own = Object.fromEntries(conf.fields.map((f) => [f.name, f.choices
    ? el("select", {}, el("option", { value: "" }, "the tunnel's default"),
        f.choices.map((c) => el("option", { value: c, selected: conf.settings[f.name] === c }, c)))
    : el("input", { value: conf.settings[f.name] || "", placeholder: "the tunnel's default", size: 44 })]));
  const save = saveButton("Save", async () => {
    const tunnel_args = APP_TUNNEL_FIELDS.filter(([k]) => inputs[k].value.trim())
      .map(([k]) => `--${k}=${inputs[k].value.trim()}`).concat(other);
    const body = { tunnel_args };
    if (conf.python_env) {
      const c = conda.value.trim();
      Object.assign(body, { conda_env: c === "" ? null : (c === "none" ? "" : c), modules: words(modules.value),
                            project: project.value.trim() });
    }
    if (conf.fields.length) body.settings = Object.fromEntries(conf.fields.map((f) => [f.name, own[f.name].value.trim()]));
    await api(path, { method: "PUT", body });
    return conf.fields.length || conf.installable
      ? "Saved. They go to the cluster with the next Check, Install or Start."
      : "Saved. Applies the next time it starts.";
  });
  row.showDetail("settings", el("hpc-panel", { heading: `${APPS_UI[app].title} on ${s.cluster}`, dismissible: true },
    el("p", { class: "muted" }, "The tunnel's job (empty: its defaults):"),
    el("div", { class: "row" }, APP_TUNNEL_FIELDS.map(([k, label, hint]) => field(`Job: ${label}`, inputs[k], hint))),
    conf.python_env ? [
      el("p", { class: "muted" }, "Where jupyter comes from (any of these; it needs jupyterlab installed):"),
      el("div", { class: "row" },
        field("Conda environment", conda, "empty: the tunnel's default; none: no conda"),
        field("Modules", modules, "loaded in the job, space-separated"),
        field("uv or pixi project", project, "its .venv or .pixi/envs/default; Install puts jupyterlab there"))] : null,
    conf.fields.length ? [
      el("p", { class: "muted" }, "On the cluster (empty: the tunnel's default). Install and the job both use these."),
      el("div", { class: "row" }, conf.fields.map((f) => field(f.label, own[f.name], f.hint)))] : null,
    el("div", { class: "actions" }, save)));
}

async function appPage(app) {
  const { sessions, title } = await api(`apps/${app}`);
  // each session's rows refresh themselves (components.js), so this page is drawn once
  const rows = sessions.map((s) => {
    const row = el("hpc-app-session", { app, "app-title": title, cluster: s.cluster });
    row.data = s;
    return row;
  });
  const tool = (APPS_UI[app] || {}).kind === "tool";
  return [
    el("h2", {}, title),
    el("p", { class: "muted" }, tool
      ? "Files between each cluster and an SMB server, with rclone from the data-transfer-tools image: " +
        "smbshell on the cluster (smbshell --help), and smbshell submit for SLURM jobs. Sign in here once: " +
        "Kerberos (kinit) where the cluster has it, or a password saved for sync jobs. Settings… names the server."
      : `A ${title} session per cluster, in a SLURM job reached through its own tunnel. ` +
        "It runs as you, with your full permissions on the cluster (it is not the agents' sandbox). " +
        "What it needs on the cluster is checked once you're logged in, and Install sets it up there."),
    sessions.length ? el("table", { class: "rows" },
      el("thead", {}, el("tr", {}, ["Cluster", "Login", title, ""].map((h) => el("th", {}, h)))),
      rows)
      : el("p", { class: "muted" }, "No clusters yet; add one under Agents → Clusters."),
  ];
}

// ---------------------------------------------------------------- shell

// The apps in the top bar, each with its own header and pages. Agents keeps its old addresses (#/clusters, ...);
// the others live under #/APP/PAGE.
const APPS_UI = {
  agents: {
    title: "Agents",
    default: "clusters",
    pages: { clusters: ["Clusters", clustersPage], proposals: ["Proposals", proposalsPage],
             activity: ["Activity", activityPage], files: ["Files", filesPage], settings: ["Settings", settingsPage] },
  },
  // the tunnel apps (JupyterLab, VS Code, PAI, ...) come from GET /api/apps, each with a Sessions page
};

function addApp(id, title, kind = "tunnel") {
  APPS_UI[id] = { title, kind, default: "sessions",
                  pages: { sessions: [kind === "tool" ? "Clusters" : "Sessions", () => appPage(id)] } };
}
addApp("jupyter", "JupyterLab");     // until the console says which it has

async function loadApps() {
  if (!key) return;
  try {
    const { apps } = await api("apps");
    for (const a of apps) if (a.id !== "agents" && !APPS_UI[a.id]) addApp(a.id, a.title, a.kind);
  } catch { /* an older console: JupyterLab only */ }
}

function pageHref(app, page) {
  return app === "agents" ? `#/${page}` : `#/${app}/${page}`;
}

function route() {
  const segs = location.hash.replace(/^#\/?/, "").split("?")[0].split("/");
  if (segs[0] in APPS_UI && segs[0] !== "agents") {
    const app = APPS_UI[segs[0]];
    return { app: segs[0], page: segs[1] in app.pages ? segs[1] : app.default };
  }
  if (segs[0] === "agents") segs.shift();
  return { app: "agents", page: segs[0] in APPS_UI.agents.pages ? segs[0] : "clusters" };
}

const badge = el("span", { id: "badge", class: "badge", hidden: true });

function drawChrome({ app, page }) {
  document.getElementById("apps").replaceChildren(...Object.entries(APPS_UI).map(([id, a]) =>
    el("a", { href: pageHref(id, a.default), class: id === app ? "active" : null }, a.title)));
  document.getElementById("app-title").textContent = APPS_UI[app].title;
  document.getElementById("pages").replaceChildren(...Object.entries(APPS_UI[app].pages).map(([id, [label]]) =>
    el("a", { href: pageHref(app, id), class: id === page ? "active" : null }, label,
       app === "agents" && id === "proposals" ? [" ", badge] : null)));
  document.title = `${APPS_UI[app].title} · hpclib console`;
}

function stopFollowing() {
  if (followTimer) clearInterval(followTimer);
  followTimer = null;
}

function setBadge(n) {
  badge.hidden = !n;
  badge.textContent = n || "";
}

async function refreshBadge() {
  try { setBadge((await api("proposals")).proposals.length); } catch { /* shown on the page */ }
}

// ---------------------------------------------------------------- what rows ask of the page

document.addEventListener("hpc-login", ({ detail }) => loginDialog(detail.cluster, { onClosed: detail.then }));
document.addEventListener("hpc-prompt", ({ detail }) => promptDialog(detail.label, detail.answer, detail.prompt));
document.addEventListener("hpc-setup", ({ detail }) => setupForm(detail.cluster, detail.row));
document.addEventListener("hpc-app-settings", ({ detail }) =>
  appSettingsForm(detail.app, detail.session, detail.row).catch(alertError));

// ---------------------------------------------------------------- passwords a tunnel asks for

// A tunnel's ssh (usually the login node's to the compute node) can ask for a password minutes after Start, once
// the job runs. GET /api/prompts is local and cheap, so it is asked every 2 s while anything started here runs
// (every 5 s otherwise), from every page: the dialog opens wherever you are. "Not now" leaves that prompt to its
// row's "Enter password…" button until it asks again.
const snoozed = new Set();
const promptKey = (p) => `${p.answer}|${p.prompt.text}|${p.prompt.retry}`;
let promptTimer = null;

async function checkPrompts(delay = 0) {
  clearTimeout(promptTimer);
  if (delay) { promptTimer = setTimeout(() => checkPrompts(), delay); return; }
  let next = 5000;
  if (key) {
    try {
      const { prompts, running } = await api("prompts");
      next = running ? 2000 : 5000;
      const waiting = new Set(prompts.map(promptKey));
      for (const k of [...snoozed]) if (!waiting.has(k)) snoozed.delete(k);
      const p = prompts.find((x) => !snoozed.has(promptKey(x)));
      if (p && !promptDialogOpen) {
        promptDialog(p.app ? `${p.title} on ${p.cluster}` : p.cluster, p.answer, p.prompt,
                     { onCancel: () => snoozed.add(promptKey(p)) });
      }
    } catch { /* an older console, or not connected: the rows still show the prompt */ }
  }
  promptTimer = setTimeout(() => checkPrompts(), next);
}

let renderCount = 0;
async function render() {
  stopFollowing();
  const where = route();
  drawChrome(where);
  const mine = ++renderCount;
  main.replaceChildren(el("p", { class: "muted" }, "Loading…"));
  let content;
  try {
    content = await APPS_UI[where.app].pages[where.page][1]();
  } catch (err) {
    content = errorBox(err);
  }
  if (mine === renderCount) main.replaceChildren(...[content].flat(3));
}

takeKeyFromHash();
key = key || storedKey();
window.addEventListener("hashchange", () => { takeKeyFromHash(); render(); });
document.getElementById("refresh").addEventListener("click", render);
loadApps().then(render);
checkPrompts();
refreshBadge();
setInterval(refreshBadge, 60000);
