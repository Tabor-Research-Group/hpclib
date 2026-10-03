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

// ---------------------------------------------------------------- shell

const PAGES = { clusters: clustersPage, proposals: proposalsPage, activity: activityPage };

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
