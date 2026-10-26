// Agent Console front end. Talks only to the agent_console HTTP API (see
// hpclib/servers/agent_console.py); nothing in hpclib imports this folder.
//
// Serve it with   agent_console --static agent-console --open
// or from elsewhere with   agent_console --allow-origin http://127.0.0.1:8000
// and open   http://127.0.0.1:8000/?api=http://127.0.0.1:27180

// The page's building blocks are custom elements, in components.js (see its header).
import { configure, el, action, confirmDialog, LOGIN_BUSY, LOGIN_LABEL, HpcClusterPicker } from "./components.js";
import { activityPage as buildActivity } from "./activity.js";
import * as clusterUI from "./cluster.js";
import * as settingsUI from "./settings.js";
import * as widgetsUI from "./widgets.js";

const params = new URLSearchParams(location.search);
// ?api= may only name a console on this machine (127.0.0.1 or localhost): the session key goes with every call, and a
// link naming another address would hand it, and whatever is typed into the prompts that follow, to that host.
function localApi(given) {
  if (!given) return "";
  try {
    const u = new URL(given);
    if (u.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(u.hostname) && u.pathname === "/" &&
        !u.search && !u.hash && !u.username && !u.password) return u.origin;
  } catch { /* not a URL */ }
  console.warn(`ignoring ?api=${given}: only a console on this machine (http://127.0.0.1:PORT) can be used`);
  return "";
}
const API = localApi((params.get("api") || "").replace(/\/$/, ""));
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
  history.replaceState(null, "", location.pathname + location.search + "#/c");
}
function askForKey() {
  const dialog = document.getElementById("key-dialog");
  if (!dialog.open) dialog.showModal();
}
document.getElementById("key-form").addEventListener("submit", (e) => {
  e.preventDefault();
  storeKey(document.getElementById("key-input").value.trim());
  document.getElementById("key-dialog").close();
  Promise.all([loadApps(), settingsUI.loadPreferences(), widgetsUI.load()]).then(render);
  settingsUI.loadPreferences().then(settingsUI.restartWatch);
});

// ---------------------------------------------------------------- API

class ApiError extends Error {
  constructor(status, payload) {
    super(payload.error || `HTTP ${status}`);
    this.status = status;
    this.payload = payload;
  }
}

async function api(path, { method = "GET", body, raw, contentType } = {}) {
  // body: sent as JSON; raw (an ArrayBuffer or Blob, e.g. a package zip): sent as it is, as contentType
  if (!key) { askForKey(); throw new ApiError(401, { error: "no session key yet" }); }
  const type = raw !== undefined ? (contentType || "application/octet-stream") : (body ? "application/json" : null);
  const res = await fetch(API + "/api/" + path, {
    method,
    headers: { Authorization: `Bearer ${key}`, ...(type ? { "Content-Type": type } : {}) },
    body: raw !== undefined ? raw : (body ? JSON.stringify(body) : undefined),
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

// Native disclosure: Enter/Space toggles it, Tab reaches its action, Escape closes it.
const consoleMenu = document.getElementById("console-menu");
const menuToggle = consoleMenu.querySelector("summary");
const openStylesheetButton = document.getElementById("open-stylesheet");
openStylesheetButton.addEventListener("click", async () => {
  if (openStylesheetButton.disabled) return;
  openStylesheetButton.disabled = true;
  consoleMenu.open = false;
  menuToggle.focus();
  try {
    await api("styles/open", { method: "POST" });
  } catch (err) {
    alertError(err);
  } finally {
    openStylesheetButton.disabled = false;
  }
});
document.addEventListener("pointerdown", (e) => {
  if (!consoleMenu.contains(e.target)) consoleMenu.open = false;
});
consoleMenu.querySelectorAll("a.menu-item").forEach((a) => a.addEventListener("click", () => { consoleMenu.open = false; }));
settingsUI.initThemeSwitch();
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && consoleMenu.open) {
    e.preventDefault();
    consoleMenu.open = false;
    menuToggle.focus();
  }
});

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
    `${c.connection_hours || 12} h. Complete the site's second factor when prompted.`);
  const go = el("button", { class: "primary", type: "submit" }, "Log in");
  const close = el("button", { type: "button" }, "Close");
  const form = el("form", {}, status, password, el("div", { class: "actions" }, go, close));
  const code = el("input", { type: "password", inputmode: "numeric", autocomplete: "one-time-code", "aria-label": "MFA code" });
  let challenge = null;
  const sendCode = action("Send code", async () => {
    const value = code.value.trim(); code.value = "";
    show(await api(cluster(c.name) + "/login/answer", { method: "POST", body: { code: value, challenge } }));
  });
  code.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); sendCode.run(); }
  });
  const codeArea = el("div", { class: "login-code", hidden: true }, field("Authenticator or SMS code", code), sendCode);
  form.append(codeArea);
  const dialog = el("hpc-dialog", { heading: `Log in to ${c.name}`, transient: true }, form);
  let timer = null;
  let connected = false;
  const finish = () => dialog.close();
  dialog.addEventListener("close", async () => {   // Close, ×, Escape, or logged in
    clearInterval(timer);
    code.value = password.value = "";
    if (connected && onConnected) {
      try { await onConnected(); } catch (err) { alertError(err); }
    }
    onClosed();
  });
  close.addEventListener("click", finish);
  const show = (st) => {
    status.className = st.state === "failed" ? "error-box" : (st.state === "connected" ? "saved" : "muted");
    status.textContent = (st.message || LOGIN_LABEL[st.state] || st.state) + (st.prompt ? ` (asked: "${st.prompt}")` : "");
    codeArea.hidden = st.state !== "code_required";
    if (st.state === "code_required" && challenge !== st.challenge) { code.value = ""; code.focus(); }
    challenge = st.challenge || null;
  };
  const poll = () => {
    clearInterval(timer);
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
  };
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!codeArea.hidden) { await sendCode.run(); return; }
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
    poll();
  });
  dialog.showModal();
  api(cluster(c.name) + "/login").then((st) => {
    if (dialog.open && LOGIN_BUSY.includes(st.state)) { go.disabled = password.disabled = true; show(st); poll(); }
  }).catch(() => {});
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
  // this machine's default (Console Settings), with the login's user, until a directory is typed
  const pattern = (settingsUI.preferences()?.new_cluster || {}).work_dir_pattern;
  let typed = false;
  work.addEventListener("input", () => { typed = true; });
  host.addEventListener("input", () => {
    const user = host.value.trim().split("@")[0];
    if (pattern && !typed) work.value = host.value.includes("@") && user ? pattern.replaceAll("{user}", user) : "";
  });
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

function presetClusterForm(spec, onAdded) {
  const username = el("input", { "aria-label": "Cluster username", placeholder: "Your cluster username", size: 24 });
  const work = el("input", { "aria-label": "Cluster work directory", placeholder: "Absolute path on the cluster", size: 40 });
  const suggestedWork = () => (spec.defaults.work_dir || "").replaceAll("{username}", username.value.trim());
  let workEdited = false;
  work.addEventListener("input", () => { workEdited = true; });
  username.addEventListener("input", () => { if (!workEdited) work.value = suggestedWork(); });
  const account = el("input", { "aria-label": "Cluster allocation", placeholder: "Your allocation/account", size: 24 });
  const given = Object.fromEntries(spec.defaults.tunnel_args.map((arg) => arg.replace(/^--/, "").split(/=(.*)/s).slice(0, 2)));
  const controls = Object.fromEntries(["partition", "time", "mem", "cpus-per-task"].map((name) => [name,
    el("input", { "aria-label": `Cluster ${name}`, value: given[name] || "", size: 14 })]));
  const add = action("Add cluster", async () => {
    const args = spec.defaults.tunnel_args.filter((arg) => !Object.keys(controls).some((name) => arg.startsWith(`--${name}=`)));
    for (const [name, input] of Object.entries(controls)) if (input.value.trim()) args.push(`--${name}=${input.value.trim()}`);
    const c = await api(`cluster-presets/${encodeURIComponent(spec.id)}`, { method: "POST", body: {
      username: username.value.trim(), work_dir: work.value.trim(), account: account.value.trim(), tunnel_args: args } });
    await onAdded(c);
    return `Added ${c.name}.`;
  }, { primary: true, result: true });
  return el("section", { class: "card" }, el("h3", {}, spec.title),
    spec.description ? el("p", {}, spec.description) : null,
    el("p", { class: "muted" }, `Login: ${spec.host}:${spec.port} · configuration from ${spec.package}`),
    spec.documentation_url ? el("p", {}, el("a", { href: spec.documentation_url, target: "_blank", rel: "noopener noreferrer" }, "Cluster user guide")) : null,
    el("div", { class: "row" }, field("Username", username), field("Work directory for agents", work,
      "Use a directory you own; enter the absolute path from the cluster's WORK or SCRATCH environment."),
      field("Allocation", account, "For the agent tunnel's job; empty uses the site's account default.")),
    el("div", { class: "row" }, Object.entries(controls).map(([name, input]) => field(
      { partition: "Partition", time: "Time limit", mem: "Memory", "cpus-per-task": "CPUs" }[name], input))),
    spec.ssh_keys === "site-managed" ? el("p", { class: "muted" }, "SSH keys are managed by this cluster's site. The console uses its existing keys and offers read-only SSH key management.") : null,
    spec.defaults.rest_config ? el("p", { class: "muted" },
      `Saved REST settings will be used during setup${spec.defaults.rest_config.limits?.max_concurrent_jobs != null ?
        ` (maximum ${spec.defaults.rest_config.limits.max_concurrent_jobs} concurrent agent job slots)` : ""}. Reruns keep existing remote settings unless Rebuild is selected.`) : null,
    el("p", { class: "muted" }, "After adding, log in and choose Set up… to install the agent service. Passwords and MFA codes are entered during login."),
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

// "Add cluster" and the form it opens (a preset's, or a custom cluster's): [button, area]
function addClusterControls() {
  const addArea = el("div", { class: "add-cluster" });
  const addBtn = el("button", { type: "button" }, "Add cluster");
  let presets = null;
  addBtn.addEventListener("click", async () => {
    if (addArea.firstChild) { addArea.replaceChildren(); return; }
    try { presets = presets || (await api("cluster-presets")).presets || []; } catch (err) { addArea.replaceChildren(errorBox(err)); return; }
    const choose = el("select", { "aria-label": "Cluster configuration" }, el("option", { value: "" }, "Custom cluster"),
      presets.map((spec) => el("option", { value: spec.id }, spec.title)));
    const form = el("div");
    const draw = () => {
      const preset = presets.find((spec) => spec.id === choose.value);
      form.replaceChildren(preset ? presetClusterForm(preset, render) : addClusterForm(render));
    };
    choose.addEventListener("change", draw); draw();
    addArea.replaceChildren(field("Configuration", choose), form);
  });
  return [addBtn, addArea];
}

async function clustersPage() {
  const [{ clusters }, health] = await Promise.all([api("clusters"), api("health")]);
  const local = health.hpclib_version;
  const [addBtn, addArea] = addClusterControls();
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

async function sshKeysPage(only = null) {
  // only: a cluster page's (its Agents tab): that cluster, no picker
  const { clusters } = await api("clusters");
  const pick = el("hpc-cluster-picker", { live: true, value: only, hidden: !!only });
  pick.clusters = clusters;
  if (only && !pick.options.some((c) => c.name === only)) {
    return el("p", { class: "muted" }, `Start ${only}'s agent tunnel (Tunnel and setup) to manage its SSH keys.`);
  }
  const body = el("div", { class: "ssh-manager" });
  const status = el("span", { class: "muted", role: "status" });
  let generation = 0;
  const load = async () => {
    const current = ++generation;
    const name = pick.value;
    if (!name) return;
    const base = cluster(name) + "/rest/admin/ssh/";
    const mutate = async (resource, method, data) => {
      await api(base + resource, { method, body: data });
      // A selection made while saving keeps its own data and target.
      if (pick.value === name) await load();
      return "Saved.";
    };
    status.textContent = "Loading keys…";
    try {
      const [keys, authorized, hosts] = await Promise.all(
        ["keys", "authorized", "hosts"].map((resource) => api(base + resource)));
      if (current !== generation || pick.value !== name) return;
      const input = (label, attrs = {}) => el("input", { "aria-label": label, ...attrs });
      const keyName = input("Key name", { placeholder: "id_ed25519_github" });
      const type = el("select", { "aria-label": "Key type" },
        ["ed25519", "rsa"].map((value) => el("option", { value }, value)));
      const comment = input("New key comment", { placeholder: "GitHub on this cluster" });
      const keyRows = keys.keys.map((key) => {
        const value = el("textarea", { "aria-label": `Public key ${key.name}`, readonly: true, rows: 3 }, key.public_key || "");
        const edit = input(`Comment ${key.name}`, { value: key.comment || "" });
        return el("hpc-panel", { heading: key.name },
          el("p", { class: "small" }, key.error || `${key.algorithm} · ${key.fingerprint}`),
          el("p", { class: "muted" }, key.compute ? "Compute-node identity" : key.private_available ? "Private key available on this cluster" : "Public key only"),
          value, field("Public key comment", edit),
          el("div", { class: "actions" },
            action("Copy public key", async () => { await navigator.clipboard.writeText(key.public_key); return "Copied."; },
              { result: true, disabled: !key.public_key }),
            action("Save comment", () => mutate("keys", "PUT", { name: key.name, comment: edit.value }),
              { disabled: !key.public_key }),
            action("Delete key", async () => {
              if (await confirmDialog(`Delete ${key.name}?`, "Both key files are removed from the shared cluster home. Remove host bindings first. Keys registered with GitHub or in authorized_keys must be removed separately."))
                return mutate("keys", "DELETE", { name: key.name });
            })));
      });
      const authorizedLine = el("textarea", { "aria-label": "New authorized key", rows: 3,
        placeholder: "ssh-ed25519 AAAA… comment (optional SSH restrictions before the key)" });
      const authorizedRows = authorized.entries.map((entry) => {
        const line = el("textarea", { "aria-label": `Authorized key ${entry.id}`, rows: 3 }, entry.line);
        return el("hpc-panel", { heading: entry.comment || entry.fingerprint },
          el("p", { class: "small" }, entry.fingerprint), line,
          el("div", { class: "actions" },
            action("Save authorized key", () => mutate("authorized", "PUT", { id: entry.id, line: line.value, revision: authorized.revision })),
            action("Remove authorization", async () => {
              if (await confirmDialog("Remove this authorization?", "This key will no longer grant login access through the shared authorized_keys file."))
                return mutate("authorized", "DELETE", { id: entry.id, revision: authorized.revision });
            })));
      });
      const host = input("SSH host", { placeholder: "github.com" });
      const user = input("SSH user", { placeholder: "git" });
      const hostname = input("SSH hostname", { placeholder: "Optional; defaults to host" });
      const port = input("SSH port", { type: "number", min: 1, max: 65535, placeholder: "22" });
      const identity = el("select", { "aria-label": "Host identity" }, keys.keys.filter((k) => k.private_available)
        .map((k) => el("option", { value: k.name }, k.name)));
      const hostRows = hosts.hosts.map((binding) => el("hpc-panel", { heading: binding.host },
        el("p", { class: "small" }, binding.identity_files.length ? binding.identity_files.join(", ") : "No explicit identity"),
        el("p", { class: "muted" }, [binding.user && `User ${binding.user}`, binding.hostname && `Hostname ${binding.hostname}`,
          binding.port && `Port ${binding.port}`, binding.managed ? "Managed binding" : "Existing SSH config (view only)"].filter(Boolean).join(" · ")),
        binding.managed ? el("div", { class: "actions" },
          action("Edit binding", () => { host.value = binding.host; identity.value = binding.key || "";
            user.value = binding.user || ""; hostname.value = binding.hostname || ""; port.value = binding.port || "";
            host.focus(); }),
          action("Remove binding", async () => {
            if (await confirmDialog(`Remove ${binding.host}?`, "Only this managed host binding is removed. The key files remain."))
              return mutate("hosts", "DELETE", { host: binding.host, revision: hosts.revision });
          })) : null));
      body.replaceChildren(
        el("p", { class: "muted" }, `Shared SSH directory: ${keys.directory}. Private key contents stay on the cluster.`),
        el("hpc-panel", { heading: "Identity keys" },
          el("p", {}, "New keys have no passphrase so cluster services can use them unattended. Existing key files are preserved."),
          el("div", { class: "actions" }, field("Key name", keyName), field("Type", type), field("Comment", comment),
            action("Create key", () => mutate("keys", "POST", { name: keyName.value, algorithm: type.value, comment: comment.value }))),
          el("p", {}, "For GitHub, copy the public key into ", el("a", { href: "https://github.com/settings/keys", target: "_blank", rel: "noopener noreferrer" }, "your GitHub SSH keys"), "."),
          keyRows, keys.errors.map((error) => el("p", { class: "error-box" }, `${error.name}: ${error.error}`))),
        el("hpc-panel", { heading: "Authorized keys" },
          el("p", { class: "muted" }, "These public keys grant access to this cluster account. Each edit preserves other entries, comments, and restrictions."),
          authorizedRows, authorizedLine,
          action("Add authorized key", () => mutate("authorized", "POST", { line: authorizedLine.value, revision: authorized.revision })),
          authorized.errors.map((error) => el("p", { class: "muted" }, `Line ${error.line_number}: ${error.error}`))),
        el("hpc-panel", { heading: "Host identities" },
          el("p", { class: "muted" }, "Choose which identity SSH uses for GitHub or another host. Bindings are stored in hpclib_keys.conf and included by your SSH config."),
          hostRows, el("div", { class: "actions" }, field("Host or patterns", host), field("Identity key", identity),
            field("User", user), field("Hostname", hostname), field("Port", port)),
          action("Save host binding", () => mutate("hosts", "PUT", { host: host.value, key: identity.value,
            user: user.value, hostname: hostname.value, port: port.value ? Number(port.value) : null, revision: hosts.revision }),
            { disabled: !identity.options.length })));
      if (pick.selected?.preset?.ssh_keys === "site-managed") {
        body.prepend(el("p", { class: "muted" }, "This site's SSH files are managed by the cluster. You can inspect and copy public keys here; changes are disabled."));
        for (const button of body.querySelectorAll("hpc-action-button")) {
          if (button.getAttribute("label") !== "Copy public key") button.disabled = true;
        }
      }
      status.textContent = "";
    } catch (err) {
      if (current === generation) { status.textContent = err.message; body.replaceChildren(); }
      throw err;
    }
  };
  pick.addEventListener("change", () => load().catch(() => {}));
  if (pick.options.length) await load().catch(() => {});
  else status.textContent = "Start a cluster REST tunnel with an owner token to manage keys. Set up compute SSH from the Clusters page.";
  return [el("div", { class: "toolbar" }, only ? null : el("h2", {}, "SSH keys"), pick,
    action("Reload keys", load, { disabled: !pick.options.length }), status), body];
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

async function activityPage(only = null) {
  return buildActivity({ api, filesHash, only, setFollowing: (callback) => {
    stopFollowing();
    if (callback) followTimer = setInterval(callback, 5000);
  } });
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
//   #/c/NAME/files?path=/a/directory          that directory (the cluster's Files tab)
//   #/c/NAME/files?path=/a/file.log           its directory, with the file open in the viewer
//   #/files?cluster=NAME&path=...             the same, from before the cluster pages (still read)
// NAME is the profile name, the login host or the MCP name.
function filesQuery() {
  return new URLSearchParams(location.hash.split("?")[1] || "");
}
function filesHash(clusterName, path) {
  const q = new URLSearchParams();
  if (path) q.set("path", path);
  return clusterUI.href(clusterName, "files") + (path ? "?" + q.toString() : "");
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

async function filesPage(only = null) {
  // only: a cluster page's (its Files tab): that cluster, no picker
  const { clusters } = await api("clusters");
  const q = filesQuery();
  const legacy = decodeURIComponent((location.hash.match(/^#\/files\/([^?]+)/) || [])[1] || "");
  const wanted = only || q.get("cluster") || legacy;
  const pick = el("hpc-cluster-picker", { live: true, value: wanted || null, hidden: !!only });
  pick.clusters = clusters;
  pick.addEventListener("change", () => { location.hash = filesHash(pick.value); });
  const matches = (x) => HpcClusterPicker.matches(x, wanted);
  if (wanted && !pick.options.some(matches)) {
    const known = clusters.find(matches);
    return [el("h2", {}, "Files"), errorBox(new Error(known
      ? `${known.name}: start its agent tunnel (Agents → Tunnel and setup) to browse its files.`
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
    only ? null : el("h2", {}, "Files"),
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
  const restOn = el("select", {}, (s.rest_on_choices || ["job", "login"]).map((m) => el("option",
    { value: m, selected: m === (s.rest_on || "job") },
    { job: "In a SLURM job on a compute node", login: "On the login node (no job)" }[m] || m)));
  const jobFields = el("div", { class: "row" },
    TUNNEL_FIELDS.map(([k, label, hint]) => field(`Tunnel job: ${label}`, inputs[k], hint)));
  const showJob = () => { jobFields.hidden = restOn.value === "login"; };
  restOn.addEventListener("change", showJob);
  showJob();
  const save = saveButton("Save tunnel settings", async () => {
    const args = TUNNEL_FIELDS.filter(([k]) => inputs[k].value.trim()).map(([k]) => `--${k}=${inputs[k].value.trim()}`)
      .concat(other);
    await api(cluster(c.name) + "/settings", { method: "PUT", body: {
      auto_approve_templates: mode.value, tunnel_args: args, connection_hours: Number(hours.value),
      rest_on: restOn.value } });
    return "Saved. Applies the next time the tunnel starts (the login hours: the next time you log in).";
  });
  return el("section", { class: "card" },
    el("h3", {}, "Tunnel ", el("span", { class: "muted" }, "· kept on this machine")),
    field("Template proposals", mode, "only while jobs are sandboxed"),
    field("Keep the ssh login for (hours)", hours, "after it was last used; 1 to 168, default 12"),
    field("Run the REST server", restOn, "on the login node where a site asks for it, or on a machine without SLURM "
      + "(its server config then runs jobs there too); setup_agents picks this for a machine without sbatch"),
    jobFields,
    other.length ? el("p", { class: "muted" }, `Also: ${other.join(" ")}`) : null,
    el("div", { class: "actions" }, save));
}

// The cluster's quota checks (lib/quota.sh): kept on the cluster in $HPCTUNNELS_DATA_DIR/settings/quota.sh, so
// hpclib_quota there, the REST server's GET /quota and the agents' check_quota use them too.
const QUOTA_FIELDS = [
  ["storage", "Storage quota check", "showquota, else myquota, else quota -s"],
  ["project", "Project (allocation) check", "myproject (TAMU HPRC), when the cluster has it"]];
async function quotaSettings(c) {
  const head = el("h3", {}, "Quota checks ", el("span", { class: "muted" }, "· saved on the cluster"));
  const st = await api(cluster(c.name) + "/storage");
  const q = st.quotas || {};
  const inputs = Object.fromEntries(QUOTA_FIELDS.map(([k, , dflt]) => [k, el("input", {
    value: (q[k] || {}).saved || "", size: 40, placeholder: `default: ${dflt}`, "aria-label": `${k} quota check`,
    maxlength: 1000 })]));
  const env = (q.environment || []).length ? el("p", { class: "muted" }, `This console was started with ` +
    `${q.environment.join(" and ")} set, which wins over these here (as the variables do on the cluster).`) : null;
  const known = st.quotas ? null : el("p", { class: "muted" },
    "What's saved shows here after the first check (Storage → Check quotas, or Save).");
  const needLogin = (c.login || {}).state !== "connected";
  const save = action("Save and check", async () => {
    const body = Object.fromEntries(QUOTA_FIELDS.map(([k]) => [k, inputs[k].value.trim()]));
    const out = await api(cluster(c.name) + "/quota/settings", { method: "PUT", body });
    return QUOTA_FIELDS.map(([k]) => { const r = out.quotas[k] || {};
      return `${k}: ${r.command ? `${r.command} (${r.source}${r.exit_code ? `, exit ${r.exit_code}` : ""})` : r.source === "none" ? "off" : "none found"}`; })
      .join(" · ");
  }, { primary: true, result: true, disabled: needLogin });
  return el("section", { class: "card" }, head,
    el("p", { class: "muted" }, "The commands that report your storage quota and your project's balance (on the " +
      "Storage tab, and to agents). Leave one empty for the default; none turns it off. On the command line: " +
      "hpclib_quota set storage|project COMMAND on the cluster, or HPCLIB_STORAGE_QUOTA_CHECK and " +
      "HPCLIB_PROJECT_QUOTA_CHECK, which win."),
    QUOTA_FIELDS.map(([k, label]) => field(label, inputs[k], "one line of shell, run with bash on the login node")),
    env, known,
    el("div", { class: "actions" }, save, needLogin ? el("span", { class: "muted small" }, "Log in to save: it's written on the cluster.") : null));
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
  const gpus = el("select", {}, [["auto", "For jobs that ask for GPUs (default)"], ["always", "For every job"],
    ["never", "Never"]].map(([v, label]) => el("option", { value: v, selected: v === (sandbox.gpus || "auto") }, label)));
  const flags = el("input", { value: (sandbox.flags || []).join(" "), size: 30, placeholder: "none" });
  const notes = el("textarea", { rows: 4 }, raw.cluster_notes || "");
  const envBoxes = Object.fromEntries(ENV_SCOPES.map(([k, label]) => {
    const box = el("hpc-env-editor", { label });
    box.rules = (conf.rules || {}).environment || null;   // older servers don't send them; they still check on save
    box.value = (raw.environment || {})[k];
    return [k, box];
  }));

  const sandboxSection = () => {
    const out = Object.assign({}, sandbox, { binds: lines(binds.value), writable: lines(writable.value),
                                             gpus: gpus.value, flags: words(flags.value) });
    // defaults stay out of the file (and away from servers older than the `gpus` setting)
    if (out.gpus === "auto" && !("gpus" in sandbox)) delete out.gpus;
    if (!out.flags.length && !("flags" in sandbox)) delete out.flags;
    return out;
  };
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
      sandbox: sandboxSection(),
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
    el("div", { class: "row" },
      field("NVIDIA GPUs in jobs", gpus, "Singularity/Apptainer's --nv; a job asks for GPUs with gres, e.g. gpu:a100:1"),
      field("Extra runtime flags", flags, "space-separated, for every job; podman GPUs: --device nvidia.com/gpu=all")),
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

// Each tunnel records its port on the login node it started on. A login that lands on another node leaves the
// earlier records (and whatever they still hold) there, which a start reports as "also started on login2".
async function portSettings(c) {
  const head = el("h3", {}, "Login-node ports ", el("span", { class: "muted" }, "· leftovers of earlier tunnels"));
  const { ports } = await api(cluster(c.name) + "/ports");
  const list = el("ul", {}, ports.map((p) => el("li", {}, `${p.port}: ${p.what}`,
    p.running ? el("span", { class: "muted" }, " (running; left alone)") : null)));
  const out = el("pre", { class: "file", hidden: true });
  const status = el("p", { class: "muted" });
  let timer = null;
  const follow = () => {
    clearInterval(timer);
    timer = setInterval(async () => {
      if (!out.isConnected) { clearInterval(timer); return; }
      let op;
      try { op = await api(cluster(c.name) + "/operation?lines=60"); } catch (err) { status.textContent = err.message; return; }
      out.hidden = false;
      out.textContent = (op.log || []).join("\n");
      out.scrollTop = out.scrollHeight;
      if (op.kind !== "clear_ports" || op.state !== "running") {
        clearInterval(timer);
        status.className = op.state === "succeeded" ? "saved" : "error-box";
        status.textContent = op.state === "succeeded" ? "Done: nothing is left on the ports." :
          "Not everything could be cleared; the end of the output says what is left.";
        clear.disabled = false;
      }
    }, 2000);
  };
  const clear = el("button", { type: "button" }, "Clear leftover ports…");
  clear.addEventListener("click", () => clearPortsDialog(c, ports, () => {
    clear.disabled = true;
    status.className = "muted";
    status.textContent = "Clearing…";
    follow();
  }));
  if (!ports.some((p) => !p.running)) clear.disabled = true;
  return el("section", { class: "card" }, head,
    el("p", { class: "muted" }, "A tunnel's port is recorded on the login node it started on. When a later login " +
      "lands on another node, the earlier tunnel's job and forwards can stay behind, and starting notes " +
      "“a tunnel on port … was also started on login2”. Clearing stops them on every login node."),
    list, el("div", { class: "actions" }, clear), status, out);
}

function clearPortsDialog(c, ports, onStarted) {
  const idle = ports.filter((p) => !p.running);
  const extra = el("input", { type: "checkbox" });
  const password = el("input", { type: "password", autocomplete: "current-password", size: 28, disabled: true });
  const count = el("input", { type: "number", min: 1, max: 10, value: 6, size: 4, disabled: true });
  extra.addEventListener("change", () => { password.disabled = count.disabled = !extra.checked; });
  const go = el("button", { class: "primary", type: "button" }, "Clear");
  const cancel = el("button", { type: "button" }, "Cancel");
  const status = el("p", { class: "muted" });
  const dialog = el("hpc-dialog", { heading: `Clear leftover ports: ${c.name}`, transient: true },
    el("p", {}, `Ports ${idle.map((p) => p.port).join(", ")} on every login node of ${c.name}: earlier tunnels' ` +
      "jobs are cancelled and what they left listening is stopped. Running tunnels are left alone. Through your " +
      "current login this reaches the login node it is on, and others the cluster lets it reach with its own SSH keys."),
    el("p", { class: "warning" }, "⚠ Nodes it can't reach that way can only be cleared by logging in again " +
      "until a login lands on them. Which node a login lands on is up to the cluster, so this can take many " +
      "logins, and every one asks for your password and sends a Duo push to approve. Leave it off to clear " +
      "only what your current login can reach; the result lists the nodes that are left."),
    el("label", {}, extra, " Allow extra logins to reach the other nodes"),
    el("div", { class: "row" }, field("Password (for the extra logins)", password, "kept in memory until they finish"),
      field("At most this many logins", count, "1 to 10")),
    status, el("div", { class: "actions" }, go, cancel));
  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { password.value = ""; });
  go.addEventListener("click", async () => {
    const body = { fresh_logins: extra.checked };
    if (extra.checked) {
      if (password.value) body.password = password.value;
      body.max_logins = Math.max(1, Math.min(10, Number(count.value) || 6));
    }
    password.value = "";
    go.disabled = true;
    try {
      await api(cluster(c.name) + "/ports/clear", { method: "POST", body });
    } catch (err) {
      status.className = "error-box";
      status.textContent = err.message;
      go.disabled = false;
      return;
    }
    dialog.close();
    onStarted();
  });
  dialog.showModal();
  cancel.focus();
}

async function settingsPage() {
  const { clusters } = await api("clusters");
  if (!clusters.length) return [el("h2", {}, "Settings"), el("p", { class: "muted" }, "No agent profiles yet; run setup_agents.")];
  const wanted = decodeURIComponent((location.hash.match(/^#\/settings\/(.+)$/) || [])[1] || "");
  const c = clusters.find((x) => x.name === wanted) || clusters[0];
  const pick = el("hpc-cluster-picker", { value: c.name });
  pick.clusters = clusters;
  pick.addEventListener("change", () => { location.hash = "#/settings/" + encodeURIComponent(pick.value); });
  const sections = await Promise.all([tunnelSettings(c), portSettings(c).catch(errorBox), serverSettings(c).catch(errorBox)]);
  return [el("h2", {}, "Settings"), el("div", { class: "toolbar" }, pick), sections];
}

// ---------------------------------------------------------------- JupyterLab

// sbatch options for the tunnel's job; empty: what its tunnel_config.sh asks for
const APP_TUNNEL_FIELDS = [["time", "Time limit", "e.g. 12:00:00"], ["mem", "Memory", "e.g. 16gb"],
                           ["cpus-per-task", "CPUs", "e.g. 4"], ["partition", "Partition", ""],
                           ["nodelist", "Node list (optional)", "empty: Slurm chooses; e.g. fc041 or fc[041-042]"]];

// An app's own bearer tokens (a package's `tokens` section: PAI's write tokens). Listed, made and revoked on the
// cluster by the app's tokens.sh over the console's login; a new token is shown once, or saved straight to a file on
// the cluster (what jobs read), and this machine never keeps it.
function tokensSection(app, s, tk) {
  const base = `apps/${app}/${encodeURIComponent(s.cluster)}/tokens`;
  const listing = el("div", {});
  const scopeBoxes = tk.scopes.map((name) => {
    const box = el("input", { type: "checkbox", value: name, checked: name === tk.default_scope });
    return [name, box, el("label", { class: "check" }, box, " " + name)];
  });
  const label = el("input", { size: 24, placeholder: "e.g. jenga-mcp", autocomplete: "off" });
  const days = el("input", { type: "number", min: 0, max: 3650, value: tk.days, size: 6 });
  const saveTo = el("input", { size: 44, autocomplete: "off",
                               placeholder: "/scratch/user/me/llm/tokens/jenga-mcp.token (empty: show it once here)" });
  const fmt = (t) => (t ? new Date(t * 1000).toLocaleDateString() : "never");
  const show = async () => {
    try {
      const { tokens } = await api(base);
      listing.replaceChildren(tokens.length ? el("table", { class: "rows" },
        el("thead", {}, el("tr", {}, ["Id", "Label", "Scopes", "Expires", "State", ""].map((h) => el("th", {}, h)))),
        el("tbody", {}, tokens.map((t) => {
          const expired = t.expires && t.expires * 1000 < Date.now();
          return el("tr", {}, el("td", {}, el("code", {}, t.id)), el("td", {}, t.label), el("td", {}, t.scopes.join(", ")),
            el("td", {}, fmt(t.expires)), el("td", {}, t.revoked ? "revoked" : expired ? "expired" : "active"),
            el("td", {}, t.revoked ? null : action("Revoke", async () => {
              if (!await confirmDialog(`Revoke token ${t.id}?`, `Anything using "${t.label}" is refused from now on ` +
                                       "(the database reads the change at once).", "Revoke")) return;
              await api(`${base}/${encodeURIComponent(t.id)}`, { method: "DELETE" });
              await show();
              return "Revoked.";
            }, { result: true })));
        }))) : el("p", { class: "muted" }, "No tokens yet."));
    } catch (err) {
      listing.replaceChildren(errorBox(err));
    }
  };
  const reveal = (made) => new Promise((resolve) => {
    const out = el("input", { readonly: true, size: 60, value: made.token, class: "token-secret" });
    const copy = el("button", { type: "button" }, "Copy");
    const done = el("button", { class: "primary", type: "button" }, "I have saved it");
    const dialog = el("hpc-dialog", { heading: `New token ${made.record.id}`, transient: true },
      el("p", {}, "This is the only time the token is shown; this machine doesn't keep it. Put it in a file only you " +
        "can read on the cluster, or use “Save to a file” next time."),
      out, el("div", { class: "actions" }, copy, done));
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(made.token); copy.textContent = "Copied"; }
      catch { out.select(); copy.textContent = "Press Ctrl/Cmd+C"; }
    });
    done.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => { out.value = ""; resolve(); });
    dialog.showModal();
    out.select();
  });
  const create = saveButton("Make a token", async () => {
    const scopes = scopeBoxes.filter(([, box]) => box.checked).map(([name]) => name);
    if (!label.value.trim()) throw new Error("Give the token a label (what will use it).");
    if (!scopes.length) throw new Error("Pick at least one scope.");
    const body = { label: label.value.trim(), scopes, days: Number(days.value) };
    if (saveTo.value.trim()) body.save_to = saveTo.value.trim();
    const made = await api(base, { method: "POST", body });
    label.value = "";
    await show();
    if (made.token) { await reveal(made); return `Made token ${made.record.id}.`; }
    return `Made token ${made.record.id}; its secret is in ${made.saved_to} on ${s.cluster} (mode 600).`;
  });
  show();
  return el("section", { class: "tokens" },
    el("h4", {}, tk.label),
    tk.description ? el("p", { class: "muted" }, tk.description) : null,
    el("p", { class: "muted" }, `Run on ${s.cluster} over your login (it must be logged in, and the app installed or ` +
      "updated there first, so its tokens.sh is on the cluster). Where the login node can't (no Singularity there, " +
      "only on compute nodes), they run where the cluster's REST server does: then the agent tunnel must be up too."),
    listing,
    el("div", { class: "row" },
      field("Label", label, "what uses it"),
      field("Valid for (days)", days, "0: never expires"),
      el("hpc-field", { label: "Scopes" }, el("div", { class: "checks" }, scopeBoxes.map(([, , l]) => l)))),
    field("Save to a file", saveTo, "a new file on the cluster, mode 600: for jobs (database.token_file, PAI_API_TOKEN_FILE)"),
    el("div", { class: "actions" }, create));
}

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
  const installModules = el("input", { value: (conf.install_modules || []).join(" "),
    placeholder: "e.g. WebProxy", size: 40 });
  const project = el("input", { value: conf.project, placeholder: "/scratch/user/me/llm/my-project", size: 40 });
  // empty: a package's value if one gives it (shown, with the package), else the tunnel's own default
  const defaults = conf.defaults || {};
  const fallback = (name) => defaults[name] ? `${defaults[name].value} (from ${defaults[name].package})`
                                            : "the tunnel's default";
  const own = Object.fromEntries(conf.fields.map((f) => [f.name, f.choices
    ? el("select", {}, el("option", { value: "" }, fallback(f.name)),
        f.choices.map((c) => el("option", { value: c, selected: conf.settings[f.name] === c }, c)))
    : el("input", { value: conf.settings[f.name] || "", placeholder: fallback(f.name), size: 44 })]));
  const save = saveButton("Save", async () => {
    const tunnel_args = APP_TUNNEL_FIELDS.filter(([k]) => inputs[k].value.trim())
      .map(([k]) => `--${k}=${inputs[k].value.trim()}`).concat(other);
    const body = { tunnel_args };
    if (conf.installable) body.install_modules = words(installModules.value);
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
  // secrets (a packaged app's tokens, OAuth client secrets): typed here, sent to the cluster over ssh, never kept
  // by the console or shown again; the panel only says whether each is set there
  const secrets = conf.secrets || [];
  const secretInputs = Object.fromEntries(secrets.map((f) => [f.name, el("input", {
    type: "password", autocomplete: "new-password", size: 44,
    placeholder: f.set === true ? "set on the cluster; type to replace" : f.set === false ? "not set" : "not checked yet" })]));
  const secretsPath = `apps/${app}/${encodeURIComponent(s.cluster)}/secrets`;
  const sendSecrets = saveButton("Send to the cluster", async () => {
    const values = Object.fromEntries(secrets.map((f) => [f.name, secretInputs[f.name].value.trim()]).filter(([, v]) => v));
    if (!Object.keys(values).length) throw new Error("Type at least one value (empty fields are left as they are).");
    const out = await api(secretsPath, { method: "PUT", body: { values } });
    for (const f of out.secrets) {
      secretInputs[f.name].value = "";
      secretInputs[f.name].placeholder = f.set ? "set on the cluster; type to replace" : "not set";
    }
    return "Sent. The app can use them on its next Install or Start.";
  });
  const clearSecrets = action("Remove them", async () => {
    if (!await confirmDialog("Remove the secrets?", `They are deleted on ${s.cluster}; the app runs without them ` +
                             "from its next Start.", "Remove")) return;
    const out = await api(secretsPath, { method: "PUT", body: { values: Object.fromEntries(secrets.map((f) => [f.name, ""])) } });
    for (const f of out.secrets) secretInputs[f.name].placeholder = f.set ? "set on the cluster; type to replace" : "not set";
    return "Removed.";
  }, { result: true });
  row.showDetail("settings", el("hpc-panel", { heading: `${APPS_UI[app].title} on ${s.cluster}`, dismissible: true },
    row.installBox,      // what the cluster says about the install, Check and Install: kept current by the row
    conf.job === false ? null : [      // runs on the login node: no job, so no sbatch options
      el("p", { class: "muted" }, "The tunnel's job (empty: its defaults):"),
      el("div", { class: "row" }, APP_TUNNEL_FIELDS.map(([k, label, hint]) => field(`Job: ${label}`, inputs[k], hint)))],
    conf.python_env ? [
      el("p", { class: "muted" }, "Where jupyter comes from (any of these; it needs jupyterlab installed):"),
      el("div", { class: "row" },
        field("Conda environment", conda, "empty: the tunnel's default; none: no conda"),
        field("Modules", modules, "loaded in the job, space-separated"),
        field("uv or pixi project", project, "its .venv or .pixi/envs/default; Install puts jupyterlab there"))] : null,
    conf.installable ? field("Installation modules", installModules,
      "loaded in order before Install, Update and Check, including on the REST node; space-separated; empty: none") : null,
    conf.fields.length ? [
      el("p", { class: "muted" }, "On the cluster (empty: an installed package's value, else the tunnel's default). " +
        (conf.job === false ? "Install and Start both use these." : "Install and the job both use these.")),
      el("div", { class: "row" }, conf.fields.map((f) => field(f.label, own[f.name], f.hint)))] : null,
    el("div", { class: "actions" }, save),
    secrets.length ? el("section", { class: "secrets" },
      el("h4", {}, "Secrets"),
      el("p", { class: "muted" }, `Sent to ${s.cluster} over your login and kept there in a file readable by your ` +
        "cluster account; this machine keeps only whether each is set. Needs the login."),
      el("div", { class: "row" }, secrets.map((f) => field(f.label, secretInputs[f.name], f.hint))),
      el("div", { class: "actions" }, sendSecrets, clearSecrets)) : null,
    conf.tokens ? tokensSection(app, s, conf.tokens) : null));
}

async function appPage(app) {
  const { sessions, title } = await api(`apps/${app}`);
  // each session's rows refresh themselves (components.js), so this page is drawn once
  const rows = sessions.map((s) => {
    const row = el("hpc-app-session", { app, "app-title": title, cluster: s.cluster });
    row.data = s;
    return row;
  });
  const { kind, description } = APPS_UI[app] || {};
  return [
    el("h2", {}, title),
    el("p", { class: "muted" }, description ? description : kind === "login"
      ? `A ${title} session per cluster, on its login node, reached through the console's ssh login. ` +
        "It runs as you, with your full permissions on the cluster (it is not the agents' sandbox)."
      : `A ${title} session per cluster, in a SLURM job reached through its own tunnel. ` +
        "It runs as you, with your full permissions on the cluster (it is not the agents' sandbox). " +
        "What it needs on the cluster is checked once you're logged in, and Install (under Settings…) sets it up there."),
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
             activity: ["Activity", activityPage], files: ["Files", filesPage], keys: ["SSH keys", sshKeysPage], settings: ["Settings", settingsPage] },
  },
  // Tunnel services come from GET /api/apps. A package may display a service as
  // a tab beside another app's Sessions page, retaining its own API and settings.
};

function addApp(id, title, kind = "tunnel", description = null, tab = null, registry = APPS_UI) {
  registry[id] = { title, kind, description, tab, default: "sessions",
                  pages: { sessions: ["Sessions", () => appPage(id)] } };
}
addApp("jupyter", "JupyterLab");     // until the console says which it has

async function loadApps() {
  if (!key) return;
  try {
    const { apps } = await api("apps");
    const next = { agents: APPS_UI.agents };
    for (const a of apps) if (a.id !== "agents") addApp(a.id, a.title, a.kind, a.description, a.tab, next);
    // Second pass: package order need not place the parent before its tabs.
    for (const a of apps) {
      if (!a.tab) continue;
      const parent = next[a.tab.parent];
      if (!parent || parent.tab || a.tab.id === "sessions" || Object.hasOwn(parent.pages, a.tab.id)) {
        throw new Error(`Invalid tab for ${a.id}`);
      }
      parent.pages[a.tab.id] = [a.tab.label, () => appPage(a.id)];
    }
    // Replace only after a complete response. Refresh also removes obsolete
    // entries/tabs after a package upgrade or removal.
    for (const id of Object.keys(APPS_UI)) if (id !== "agents") delete APPS_UI[id];
    Object.assign(APPS_UI, next);
  } catch { /* an older console: JupyterLab only */ }
}

function pageHref(app, page) {
  return app === "agents" ? `#/${page}` : `#/${app}/${page}`;
}

function route() {
  const segs = location.hash.replace(/^#\/?/, "").split("?")[0].split("/");
  // the cluster-first pages: #/c (the clusters) and #/c/NAME/TAB (one cluster)
  if (segs[0] === "c") {
    const tab = clusterUI.CLUSTER_TABS.some(([id]) => id === segs[2]) || segs[2] === "widget" ? segs[2] : "overview";
    return { app: "c", page: segs[1] ? "cluster" : "home", cluster: segs[1] ? decodeURIComponent(segs[1]) : null, tab,
             sub: decodeURIComponent(segs[3] || "") };
  }
  // links to a cluster's files from before the cluster pages (#/files?cluster=NAME, #/files/NAME) open its Files tab
  const oldFiles = segs[0] === "files" && (filesQuery().get("cluster") || decodeURIComponent(segs[1] || ""));
  if (oldFiles) return { app: "c", page: "cluster", cluster: oldFiles, tab: "files", sub: "" };
  if (segs[0] === "proposals") return { app: "proposals", page: "proposals" };
  // this machine's pages, from the console menu
  if (Object.hasOwn(MENU_PAGES, segs[0])) return { app: "menu", page: segs[0] };
  if (Object.hasOwn(APPS_UI, segs[0]) && segs[0] !== "agents") {
    const app = APPS_UI[segs[0]];
    // Bookmarks from before grouping still reach the same service's controls.
    if (app.tab) return { app: app.tab.parent, page: app.tab.id };
    return { app: segs[0], page: Object.hasOwn(app.pages, segs[1]) ? segs[1] : app.default };
  }
  if (segs[0] === "agents") segs.shift();
  if (!(segs[0] in APPS_UI.agents.pages)) return { app: "c", page: "home", cluster: null, tab: "overview", sub: "" };
  return { app: "agents", page: segs[0] };
}

const badge = el("span", { id: "badge", class: "badge", hidden: true });

// The top bar holds what spans clusters: Clusters (home), Proposals, and Packages (a button). Everything about one
// cluster, its apps included, is under #/c/NAME. The pages from before (#/clusters, #/activity, #/APP, ...) still
// open, under their own headers, from old links.
const MENU_PAGES = { interface: ["Interface Settings", () => settingsUI.interfacePage()],
                     console: ["Console Settings", () => settingsUI.consolePage()], about: ["About", () => settingsUI.aboutPage()] };

function drawChrome({ app, page, cluster: name, tab, sub }) {
  document.getElementById("apps").replaceChildren(
    el("a", { href: "#/c", class: !["proposals", "menu"].includes(app) ? "active" : null }, "Clusters"),
    el("a", { href: "#/proposals", class: app === "proposals" ? "active" : null }, "Proposals", " ", badge));
  const title = document.getElementById("app-title");
  if (app === "proposals" || app === "menu") {
    const heading = app === "menu" ? MENU_PAGES[page][0] : "Proposals";
    title.replaceChildren(heading);
    document.getElementById("pages").replaceChildren();
    document.title = `${heading} · hpclib console`;
    return;
  }
  if (app === "c") {
    // on a cluster's page, its name is a switcher: the same tab on another cluster
    if (name) {
      const pick = el("hpc-cluster-picker", { value: name, "aria-label": "Cluster", class: "cluster-switch" });
      pick.addEventListener("change", () => { location.hash = clusterUI.href(pick.value, tab, tab === "widget" ? sub : ""); });
      title.replaceChildren(pick);
    } else title.replaceChildren("Clusters");
    document.getElementById("pages").replaceChildren(...(name ? clusterUI.tabs().map(([id, label, key]) =>
      el("a", { href: clusterUI.href(name, id, key), class: id === tab && (id !== "widget" || key === sub) ? "active" : null },
         label)) : []));
    document.title = `${name || "Clusters"} · hpclib console`;
    return;
  }
  title.replaceChildren(APPS_UI[app].title);
  document.getElementById("pages").replaceChildren(...Object.entries(APPS_UI[app].pages).map(([id, [label]]) =>
    el("a", { href: pageHref(app, id), class: id === page ? "active" : null }, label)));
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
  try {
    const { proposals } = await api("proposals");
    setBadge(proposals.length);
    settingsUI.noteProposals(proposals);
  } catch { /* shown on the page */ }
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
      if (p) settingsUI.notePrompt(p, promptKey(p));
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
    content = where.app === "c"
      ? await (where.cluster ? clusterUI.clusterPage(where.cluster, where.tab, where.sub) : clusterUI.clustersHome())
      : where.app === "proposals" ? await proposalsPage()
      : where.app === "menu" ? await MENU_PAGES[where.page][1]()
      : await APPS_UI[where.app].pages[where.page][1]();
  } catch (err) {
    content = errorBox(err);
  }
  if (mine === renderCount) main.replaceChildren(...[content].flat(3));
}

takeKeyFromHash();
key = key || storedKey();
window.addEventListener("hashchange", () => { takeKeyFromHash(); render(); });
document.getElementById("refresh").addEventListener("click", () => Promise.all([loadApps(), widgetsUI.load()]).then(render));
document.getElementById("add-package").addEventListener("click", () => packagesDialog());

// ---------------------------------------------------------------- packages

// Apps and settings that aren't part of hpclib come as package zips (hpclib-package.json and its files; see
// console_packages.py). This dialog lists the installed ones, and checks a zip, shows what it adds, and installs it.
async function packagesDialog() {
  const list = el("div", {});
  const report = el("div", {});
  const file = el("input", { type: "file", accept: ".zip,application/zip" });
  const dialog = el("hpc-dialog", { heading: "Packages", transient: true },
    el("p", { class: "muted" }, "A package is a zip file with an hpclib-package.json: apps for this console, the " +
      "tunnels they run on your clusters, cluster configurations, and settings (such as a group's SMB server). Its scripts run on your " +
      "clusters as you, so install only packages you trust."),
    field("Package zip", file, "it is checked first; nothing is installed until you say so"),
    report, el("h4", {}, "Installed"), list);

  async function drawList() {
    const { packages } = await api("packages");
    list.replaceChildren(packages.length ? el("ul", { class: "packages" }, packages.map((p) => el("li", {},
      el("strong", {}, `${p.name} ${p.version}`), p.description ? el("span", { class: "muted" }, ` · ${p.description}`) : null,
      el("ul", {}, p.summary.map((line) => el("li", { class: "muted" }, line))),
      action("Remove", async () => {
        if (!await confirmDialog(`Remove ${p.name}?`, `Its apps leave the console. What it sent to clusters stays ` +
                                 `there; its settings files are cleared with each app's next Check. Added cluster profiles remain.`, "Remove")) return;
        await api(`packages/${encodeURIComponent(p.name)}`, { method: "DELETE" });
        location.reload();
      })))) : el("p", { class: "muted" }, "None yet."));
  }

  file.addEventListener("change", async () => {
    const chosen = file.files[0];
    if (!chosen) return;
    report.replaceChildren(el("p", { class: "muted" }, `Checking ${chosen.name}…`));
    const raw = await chosen.arrayBuffer();
    let checked;
    try {
      checked = await api("packages?dry_run=1", { method: "POST", raw, contentType: "application/zip" });
    } catch (err) {
      report.replaceChildren(errorBox(err));
      return;
    }
    const p = checked.package;
    const install = action(checked.replaces ? `Replace ${p.version === checked.replaces ? "it" : checked.replaces}`
                                            : "Install", async () => {
      await api("packages", { method: "POST", raw, contentType: "application/zip" });
      location.reload();     // its apps join the top bar
    }, { primary: true });
    report.replaceChildren(el("section", { class: "card" },
      el("h4", {}, `${p.name} ${p.version}`, checked.replaces ? el("span", { class: "muted" },
        ` (installed: ${checked.replaces})`) : null),
      p.description ? el("p", {}, p.description) : null,
      el("ul", {}, checked.summary.map((line) => el("li", {}, line))),
      el("p", { class: "muted hash" }, `sha256 ${checked.sha256}`),
      checked.problems.length
        ? el("div", { class: "error-box" }, "It can't be installed: ", checked.problems.join("; "))
        : el("div", { class: "actions" }, install)));
  });

  dialog.showModal();
  try { await drawList(); } catch (err) { list.replaceChildren(errorBox(err)); }
}
clusterUI.init({
  api, errorBox, render: () => render(),
  settingsCards: (c) => Promise.all([tunnelSettings(c), portSettings(c).catch(errorBox), quotaSettings(c).catch(errorBox),
                                     serverSettings(c).catch(errorBox)]),
  addCluster: addClusterControls,
  files: (name) => filesPage(name),
  activity: (name) => activityPage(name),
  sshKeys: (name) => sshKeysPage(name),
  appSettings: (app, s, row) => appSettingsForm(app, s, row),
});
settingsUI.init({ api, errorBox, field, key: () => key, render: () => render() });
widgetsUI.init({ api, errorBox, preferences: () => settingsUI.preferences() });
Promise.all([loadApps(), settingsUI.loadPreferences(), widgetsUI.load()]).then(render);
settingsUI.loadPreferences().then(settingsUI.restartWatch);
checkPrompts();
refreshBadge();
setInterval(refreshBadge, 60000);
