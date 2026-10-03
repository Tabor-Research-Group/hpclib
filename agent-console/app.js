// Agent Console front end. Talks only to the agent_console HTTP API (see
// hpclib/servers/agent_console.py); nothing in hpclib imports this folder.
//
// Serve it with   agent_console --static agent-console --open
// or from elsewhere with   agent_console --allow-origin http://127.0.0.1:8000
// and open   http://127.0.0.1:8000/?api=http://127.0.0.1:27180

const params = new URLSearchParams(location.search);
const API = (params.get("api") || "").replace(/\/$/, "");
const main = document.getElementById("main");
const KEY_ITEM = "agent-console-key";
let key = null;
let followTimer = null;

// ---------------------------------------------------------------- session key

function storedKey() {
  try { return sessionStorage.getItem(KEY_ITEM); } catch { return null; }
}
function storeKey(value) {
  key = value;
  try { sessionStorage.setItem(KEY_ITEM, value); } catch { /* memory only */ }
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
  render();
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
  if (res.status === 401) askForKey();
  if (!res.ok) throw new ApiError(res.status, payload);
  return payload;
}

const cluster = (name) => "clusters/" + encodeURIComponent(name);

// ---------------------------------------------------------------- DOM helpers

function el(tag, attrs = {}, ...children) {
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

function busy(button, fn) {
  return async () => {
    button.disabled = true;
    try { await fn(); } catch (err) { alertError(err); } finally { button.disabled = false; }
  };
}

function alertError(err) {
  main.prepend(errorBox(err));
}

function diffView(text) {
  return el("pre", {}, text.split("\n").map((line) => el("span", {
    class: line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : null,
  }, line + "\n")));
}

// ---------------------------------------------------------------- pages

async function clustersPage() {
  const { clusters } = await api("clusters");
  const rows = clusters.map((c) => {
    const t = c.tunnel || {};
    const logRow = el("tr", { hidden: true }, el("td", { colspan: 5 }));
    const showLog = async () => {
      if (!logRow.hidden) { logRow.hidden = true; return; }
      const { lines } = await api(cluster(c.name) + "/tunnel/log?lines=200");
      logRow.firstChild.replaceChildren(el("pre", {}, lines.join("\n") || "(no log yet)"));
      logRow.hidden = false;
    };
    const start = el("button", {}, "Start");
    const stop = el("button", {}, "Stop");
    const log = el("button", {}, "Log");
    start.addEventListener("click", busy(start, async () => {
      await api(cluster(c.name) + "/tunnel/start", { method: "POST", body: {} });
      setTimeout(render, 3000);
    }));
    stop.addEventListener("click", busy(stop, async () => {
      await api(cluster(c.name) + "/tunnel/stop", { method: "POST" });
      render();
    }));
    log.addEventListener("click", busy(log, showLog));
    start.disabled = t.state === "up" || t.state === "starting";
    stop.disabled = t.state === "down" && !t.started_here;
    return [
      el("tr", {},
        el("td", {}, c.name, el("div", { class: "muted" }, c.mcp_name || "")),
        el("td", {},
          el("span", { class: `state ${t.state}` }, t.state),
          t.state === "up" ? el("div", { class: "muted" },
            `${t.hostname || ""}${t.slurm_job_id ? ` · job ${t.slurm_job_id}` : ""}`) : null,
          t.error ? el("div", { class: "muted" }, t.error) : null),
        el("td", { class: "num" }, c.port),
        el("td", {}, c.has_owner_token ? "owner" : el("span", { class: "muted" }, "agent only")),
        el("td", {}, el("div", { class: "actions" }, start, stop, log))),
      logRow,
    ];
  });
  return [
    el("h2", {}, "Clusters"),
    clusters.length ? el("table", {},
      el("thead", {}, el("tr", {}, ["Cluster", "Tunnel", "Port", "Token", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, rows.flat()))
      : el("p", { class: "muted" }, "No agent profiles yet; run setup_agents."),
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
    const diff = el("button", {}, "Diff");
    const approve = el("button", { class: "primary" }, p.replaces_existing ? "Approve, replacing" : "Approve");
    const reject = el("button", {}, "Reject");
    diff.addEventListener("click", busy(diff, async () => {
      if (!detailRow.hidden) { detailRow.hidden = true; return; }
      const d = await api(base + "/diff?name=" + encodeURIComponent(p.name));
      const files = Object.entries(d.diff);
      detailRow.firstChild.replaceChildren(el("div", { class: "detail" },
        d.valid ? null : errorBox(new Error(`not valid: ${d.error}`)),
        files.length ? files.map(([f, text]) => [el("div", { class: "muted" }, f), diffView(text)])
          : el("p", { class: "muted" }, "Identical to the current template.")));
      detailRow.hidden = false;
    }));
    approve.addEventListener("click", busy(approve, async () => {
      await api(base + "/approve", { method: "POST", body: { name: p.name, replace: !!p.replaces_existing } });
      render();
    }));
    reject.addEventListener("click", busy(reject, async () => {
      const reason = prompt(`Reject ${p.name}? Reason (shown in the cluster's record):`, "");
      if (reason === null) return;
      await api(base + "/reject", { method: "POST", body: { name: p.name, reason } });
      render();
    }));
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
  const live = clusters.filter((c) => c.tunnel && c.tunnel.state === "up" && c.has_owner_token);
  if (!live.length) {
    return [el("h2", {}, "Activity"), el("p", { class: "muted" }, "No cluster with a live tunnel and an owner token.")];
  }
  const pick = el("select", {}, live.map((c) => el("option", { value: c.name }, c.name)));
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
      el("td", { class: "num" }, el("span", { class: (e.status || 0) >= 400 ? "state error" : "" }, e.status ?? "")),
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
const envText = (vars) => Object.entries(vars || {}).map(([k, v]) => `${k}=${v}`).join("\n");
function envParse(text, label) {
  const out = {};
  for (const line of lines(text)) {
    if (line.startsWith("#")) continue;
    const i = line.indexOf("=");
    if (i < 1) throw new Error(`${label}: "${line}" is not NAME=value`);
    out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}

const words = (text) => text.split(/[\s,]+/).map((w) => w.trim()).filter(Boolean);
const lines = (text) => text.split("\n").map((w) => w.trim()).filter(Boolean);

function field(label, input, hint) {
  return el("label", { class: "field" }, el("span", {}, label), input, hint ? el("small", { class: "muted" }, hint) : null);
}

function saveButton(label, fn, out) {
  const b = el("button", { class: "primary", type: "button" }, label);
  b.addEventListener("click", busy(b, async () => {
    out.replaceChildren();
    try {
      out.replaceChildren(el("span", { class: "saved" }, await fn()));
    } catch (err) {
      out.replaceChildren(errorBox(err));
    }
  }));
  return b;
}

async function tunnelSettings(c) {
  const s = await api(cluster(c.name) + "/settings");
  const mode = el("select", {}, s.modes.map((m) => el("option", { value: m, selected: m === s.auto_approve_templates },
    { all: "Approve every valid proposal", new: "Approve new templates; review replacements",
      review: "Review every proposal" }[m] || m)));
  const given = Object.fromEntries(s.tunnel_args.map((a) => a.replace(/^--/, "").split(/=(.*)/s).slice(0, 2)));
  const inputs = Object.fromEntries(TUNNEL_FIELDS.map(([k]) => [k, el("input", { value: given[k] || "", size: 16 })]));
  const other = s.tunnel_args.filter((a) => !TUNNEL_FIELDS.some(([k]) => a.startsWith(`--${k}=`)));
  const out = el("div");
  const save = saveButton("Save tunnel settings", async () => {
    const args = TUNNEL_FIELDS.filter(([k]) => inputs[k].value.trim()).map(([k]) => `--${k}=${inputs[k].value.trim()}`)
      .concat(other);
    await api(cluster(c.name) + "/settings", { method: "PUT", body: { auto_approve_templates: mode.value, tunnel_args: args } });
    return "Saved. Applies the next time the tunnel starts.";
  }, out);
  return el("section", { class: "card" },
    el("h3", {}, "Tunnel ", el("span", { class: "muted" }, "· kept on this machine")),
    field("Template proposals", mode, "only while jobs are sandboxed"),
    el("div", { class: "row" }, TUNNEL_FIELDS.map(([k, label, hint]) => field(`Tunnel job: ${label}`, inputs[k], hint))),
    other.length ? el("p", { class: "muted" }, `Also: ${other.join(" ")}`) : null,
    el("div", { class: "actions" }, save), out);
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
  const envBoxes = Object.fromEntries(ENV_SCOPES.map(([k]) =>
    [k, el("textarea", { rows: 3, placeholder: "NAME=value, one per line" }, envText((raw.environment || {})[k]))]));

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
    for (const [k, label] of ENV_SCOPES) {
      const vars = envParse(envBoxes[k].value, label);
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
  const out = el("div");
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
  }, out);

  const jsonBox = el("textarea", { rows: 14, class: "code" },
    JSON.stringify(Object.fromEntries(conf.editable.filter((k) => k in raw).map((k) => [k, raw[k]])), null, 2));
  const jsonOut = el("div");
  const saveJson = saveButton("Save JSON", async () => {
    let parsed;
    try { parsed = JSON.parse(jsonBox.value); } catch (e) { throw new Error(`not valid JSON: ${e.message}`); }
    const changes = Object.fromEntries(conf.editable.map((k) => [k, k in parsed ? parsed[k] : null]));
    const res = await api(base, { method: "PUT", body: { changes } });
    return `Saved; in effect now.${res.backup ? ` The previous file is ${res.backup}.` : ""}`;
  }, jsonOut);

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
    el("div", { class: "actions" }, save), out,
    el("details", {}, el("summary", {}, "Edit the editable sections as JSON"), jsonBox,
      el("div", { class: "actions" }, saveJson), jsonOut));
}

async function settingsPage() {
  const { clusters } = await api("clusters");
  if (!clusters.length) return [el("h2", {}, "Settings"), el("p", { class: "muted" }, "No agent profiles yet; run setup_agents.")];
  const wanted = decodeURIComponent((location.hash.match(/^#\/settings\/(.+)$/) || [])[1] || "");
  const c = clusters.find((x) => x.name === wanted) || clusters[0];
  const pick = el("select", {}, clusters.map((x) => el("option", { value: x.name, selected: x.name === c.name }, x.name)));
  pick.addEventListener("change", () => { location.hash = "#/settings/" + encodeURIComponent(pick.value); });
  const sections = await Promise.all([tunnelSettings(c), serverSettings(c).catch(errorBox)]);
  return [el("h2", {}, "Settings"), el("div", { class: "toolbar" }, pick), sections];
}

// ---------------------------------------------------------------- shell

const PAGES = { clusters: clustersPage, proposals: proposalsPage, activity: activityPage, settings: settingsPage };

function stopFollowing() {
  if (followTimer) clearInterval(followTimer);
  followTimer = null;
}

function setBadge(n) {
  const badge = document.getElementById("badge");
  badge.hidden = !n;
  badge.textContent = n || "";
}

async function refreshBadge() {
  try { setBadge((await api("proposals")).proposals.length); } catch { /* shown on the page */ }
}

let renderCount = 0;
async function render() {
  stopFollowing();
  const page = (location.hash.match(/^#\/(\w+)/) || [])[1] || "clusters";
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  const mine = ++renderCount;
  main.replaceChildren(el("p", { class: "muted" }, "Loading…"));
  let content;
  try {
    content = await (PAGES[page] || clustersPage)();
  } catch (err) {
    content = errorBox(err);
  }
  if (mine === renderCount) main.replaceChildren(...[content].flat(3));
}

takeKeyFromHash();
key = key || storedKey();
window.addEventListener("hashchange", () => { takeKeyFromHash(); render(); });
document.getElementById("refresh").addEventListener("click", render);
render();
refreshBadge();
setInterval(refreshBadge, 60000);
