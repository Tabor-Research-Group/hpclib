// The cluster-first pages: #/c lists the clusters with what each needs, and #/c/NAME/TAB[/SUB] is one cluster's
// page (Overview, Jobs, Apps, Installed, Storage, Files, Agents, Settings); #/c/NAME/apps/APP is one app there, with
// its controls, tools and settings, and a package's tabs for it. They use the console's existing API, plus
// GET clusters/NAME/jobs, GET/POST clusters/NAME/storage[/scan] and GET clusters/NAME/quota. app.js passes what they share with it (init).
import { el, action, confirmDialog } from "./components.js";
import * as widgets from "./widgets.js";

// { api, errorBox, render, settingsCards: (c) => [cards], addCluster: () => [button, area], files: (name) => page,
//   activity: (name) => page, sshKeys: (name) => page, appSettings: (app, session, row) => (draws in row.showDetail) }
let ctx = null;
export function init(given) { ctx = given; }

const enc = encodeURIComponent;
const path = (name, rest = "") => `clusters/${enc(name)}${rest ? "/" + rest : ""}`;
export const CLUSTER_TABS = [["overview", "Overview"], ["jobs", "Jobs"], ["apps", "Apps"], ["installed", "Installed"],
                             ["storage", "Storage"], ["files", "Files"], ["agents", "Agents"], ["settings", "Settings"]];
// the tabs, with a packaged widget's own (slot cluster.tab): [id, label, sub]
export function tabs() {
  return [...CLUSTER_TABS.map(([id, label]) => [id, label, ""]),
          ...widgets.forSlot("cluster.tab").map((w) => ["widget", w.title, w.key])];
}
const AGENT_PAGES = [["", "Tunnel and setup"], ["activity", "Activity"], ["keys", "SSH keys"]];
export const href = (name, tab = "overview", sub = "") => `#/c/${enc(name)}/${tab}${sub ? "/" + enc(sub) : ""}`;
const appHref = (name, app) => href(name, "apps", app);
// the apps a cluster page lists: a package's tab apps too, after their parent
async function appList() {
  const { apps } = await ctx.api("apps");
  const own = apps.filter((a) => a.id !== "agents");
  const top = own.filter((a) => !a.tab);
  return top.flatMap((a) => [a, ...own.filter((x) => x.tab && x.tab.parent === a.id)])
    .concat(own.filter((x) => x.tab && !top.some((a) => a.id === x.tab.parent)));
}
function subTabs(items, active) {
  return el("nav", { class: "subtabs" }, items.map(([link, label, id]) =>
    el("a", { href: link, class: id === active ? "active" : null }, label)));
}

// ---------------------------------------------------------------- small pieces

function chip(label, tone = "down", title = null) {
  return el("span", { class: `chip ${tone}`, title }, label);
}

function versionCompare(a, b) {
  const pa = String(a || "0").split("."), pb = String(b || "0").split(".");
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = Number(pa[i] || 0), y = Number(pb[i] || 0);
    if (x !== y) return x < y ? -1 : 1;
  }
  return 0;
}

function size(kb) {
  if (kb === null || kb === undefined) return "—";
  const units = ["KB", "MB", "GB", "TB"];
  let v = kb, u = 0;
  while (v >= 1024 && u < units.length - 1) { v /= 1024; u++; }
  return `${v >= 100 || u === 0 ? Math.round(v) : v.toFixed(1)} ${units[u]}`;
}

function count(n) {
  if (n === null || n === undefined) return "—";
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}k` : String(n);
}

function ago(t) {
  if (!t) return "never";
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago`
    : `${Math.round(s / 86400)} days ago`;
}

const login = (c) => (c.login || {}).state === "connected";
const tunnelUp = (c) => (c.tunnel || {}).state === "up";
// starting: asked to start (here or elsewhere) and not up yet; Start again would only start a second one
const tunnelStarting = (c) => ["starting", "queued"].includes((c.tunnel || {}).state);
const tunnelTone = (c) => tunnelUp(c) ? "up" : tunnelStarting(c) ? "starting" : (c.tunnel || {}).state === "error" ? "error" : "down";

// Slurm's reasons, said plainly (the code stays in the tooltip)
const REASONS = {
  Priority: "behind higher-priority jobs", Resources: "waiting for free nodes", None: "about to start",
  QOSMaxJobsPerUserLimit: "at your QOS's job limit", QOSMaxCpuPerUserLimit: "at your QOS's CPU limit",
  AssocGrpGRES: "at your account's GPU limit", AssocMaxJobsLimit: "at your account's job limit",
  Dependency: "waiting for another job", BeginTime: "scheduled for later", ReqNodeNotAvail: "requested node unavailable",
  JobHeldUser: "held by you", JobHeldAdmin: "held by an administrator", PartitionTimeLimit: "over the partition's time limit",
};

const SOURCE_TONE = { agent: "agent", "agent-tunnel": "service", app: "service", shared: "service", other: "down" };

// What a cluster needs next, as one action: [label, fn, explanation]
function nextStep(c, local, go) {
  if (!login(c)) return ["Log in", () => go("login"), "Log in to see and do anything on this cluster."];
  if (c.tunnel && c.tunnel.hpclib_version && local && versionCompare(c.tunnel.hpclib_version, local) < 0) {
    return ["Update hpclib", () => go("update"), `The cluster runs hpclib ${c.tunnel.hpclib_version}; this console has ${local}.`];
  }
  if (!c.set_up) return ["Set up agents", () => go("setup"), "Agents can't use this cluster until it is set up."];
  if (tunnelStarting(c) && c.tunnel.prompt) {
    const hostkey = c.tunnel.prompt.kind === "hostkey";
    return [hostkey ? "Review host key…" : "Enter password…", () => go("answer"),
            `The agent tunnel is starting and ${hostkey ? "asks you to trust a node's host key" : "asks for your password"}.`];
  }
  if (tunnelStarting(c)) {
    return ["Starting agent tunnel…", null, "The agent tunnel is starting: its job may wait in the queue before the " +
            "REST server answers. This page follows it.", { disabled: true }];
  }
  if (!tunnelUp(c)) {
    return ["Start agent tunnel", () => go("start"), "Agents, jobs history, server settings and installs that need the cluster's REST server wait for it."];
  }
  return null;
}

// the step's button: a waiting step's is disabled (and does nothing)
function stepAction(step, primary = true) {
  const extra = step[3] || {};
  return action(step[0], step[1] || (() => {}), { primary, ...(extra.disabled ? { disabled: true, title: "starting" } : {}) });
}

// while a tunnel starts, its state is asked for every 5 s (cheap: the local port and its health) and the page is
// drawn again once it changes (up, a password prompt, or stopped)
let followTimer = null;
function followWhileStarting(clusters) {
  clearTimeout(followTimer);
  const starting = clusters.filter(tunnelStarting);
  if (!starting.length) return;
  const here = location.hash;
  const was = (c) => `${c.tunnel.state}|${!!c.tunnel.prompt}`;
  const check = async () => {
    if (location.hash !== here) return;
    try {
      const now = await Promise.all(starting.map((c) => ctx.api(path(c.name))));
      if (now.some((c, i) => was(c) !== was(starting[i]))) return ctx.render();
    } catch { /* try again */ }
    followTimer = setTimeout(check, 5000);
  };
  followTimer = setTimeout(check, 5000);
}

async function runStep(c, what) {
  const { api } = ctx;
  if (what === "login") {
    document.dispatchEvent(new CustomEvent("hpc-login", { detail: { cluster: c, then: () => ctx.render() } }));
    return;
  }
  if (what === "answer") {
    document.dispatchEvent(new CustomEvent("hpc-prompt", { detail: { label: c.name, answer: path(c.name, "tunnel/answer"),
                                                                     prompt: c.tunnel.prompt } }));
    return;
  }
  if (what === "update") await api(path(c.name, "install"), { method: "POST", body: {} });
  if (what === "setup") { location.hash = href(c.name, "agents"); return; }
  if (what === "start") await api(path(c.name, "tunnel/start"), { method: "POST", body: {} });
  if (what === "stop") await api(path(c.name, "tunnel/stop"), { method: "POST" });
  ctx.render();
}

function stepButton(c, local, primary = true) {
  const step = nextStep(c, local, (what) => runStep(c, what));
  if (!step) return null;
  return stepAction(step, primary);
}

async function jobsSummary(name) {
  try {
    const j = await ctx.api(path(name, "jobs"));
    return j.counts;
  } catch { return null; }
}

function countsText(n) {
  if (!n) return el("span", { class: "muted" }, "—");
  const parts = [];
  if (n.running) parts.push(chip(`${n.running} running`, "up"));
  if (n.queued) parts.push(chip(`${n.queued} queued`, "starting"));
  if (n.failed) parts.push(chip(`${n.failed} failed today`, "error"));
  return parts.length ? el("span", { class: "chips" }, parts) : el("span", { class: "muted" }, "no jobs");
}

function storageTotal(st) {
  const dirs = (st && st.dirs || []).filter((d) => !d.missing && !d.path.includes("/.local/tunnels"));
  if (!st || !st.scanned || !dirs.length) return null;
  return `${size(dirs.reduce((a, d) => a + (d.kb || 0), 0))} · ${count(dirs.reduce((a, d) => a + (d.files || 0), 0))} files`;
}

function storageText(st) {
  const dirs = (st && st.dirs || []).filter((d) => !d.missing && !d.path.includes("/.local/tunnels"));
  if (!st || !st.scanned || !dirs.length) return el("span", { class: "muted" }, "not scanned");
  const kb = dirs.reduce((a, d) => a + (d.kb || 0), 0), files = dirs.reduce((a, d) => a + (d.files || 0), 0);
  return el("span", {}, `${size(kb)} · ${count(files)} files`, el("span", { class: "muted" }, ` · ${ago(st.scanned)}`));
}

// ---------------------------------------------------------------- #/c: the clusters

export async function clustersHome() {
  const { api } = ctx;
  const [{ clusters }, health] = await Promise.all([api("clusters"), api("health")]);
  const local = health.hpclib_version;
  const rows = clusters.map((c) => {
    const jobsCell = el("td", {}, el("span", { class: "muted" }, login(c) ? "…" : "—"));
    const storeCell = el("td", {}, el("span", { class: "muted" }, "…"));
    if (login(c)) jobsSummary(c.name).then((n) => jobsCell.replaceChildren(countsText(n)));
    api(path(c.name, "storage")).then((st) => storeCell.replaceChildren(storageText(st)))
      .catch(() => storeCell.replaceChildren(el("span", { class: "muted" }, "—")));
    const older = c.tunnel && c.tunnel.hpclib_version && local && versionCompare(c.tunnel.hpclib_version, local) < 0;
    return el("tr", {},
      el("td", {}, el("a", { href: `#/c/${enc(c.name)}/overview`, class: "cluster-link" }, c.name),
        el("div", { class: "muted small" }, c.host)),
      el("td", {}, chip(login(c) ? "logged in" : "not logged in", login(c) ? "up" : "down")),
      el("td", {}, chip(tunnelUp(c) ? "up" : (c.tunnel || {}).state || "down", tunnelTone(c))),
      el("td", {}, c.tunnel && c.tunnel.hpclib_version ? [c.tunnel.hpclib_version, older ? [" ", chip("update", "starting")] : null]
        : el("span", { class: "muted" }, "—")),
      jobsCell, storeCell,
      el("td", { class: "next" }, stepButton(c, local) ||
        el("a", { class: "button", href: `#/c/${enc(c.name)}/overview` }, "Open")));
  });
  followWhileStarting(clusters);
  return [
    el("div", { class: "toolbar" }, el("h2", {}, "Clusters"),
      el("span", { class: "muted" }, local ? `hpclib ${local} on this machine` : ""),
      ...ctx.addCluster()),
    widgets.strip("home"),
    clusters.length ? el("table", { class: "overview-table" },
      el("thead", {}, el("tr", {}, ["Cluster", "Login", "Agent tunnel", "hpclib", "Jobs", "Storage", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, rows))
      : el("p", { class: "muted" }, "No clusters yet; add one."),
  ];
}

// ---------------------------------------------------------------- #/c/NAME/TAB

export async function clusterPage(name, tab, sub = "") {
  const { api } = ctx;
  const [c, health] = await Promise.all([api(path(name)), api("health")]);
  const local = health.hpclib_version;
  const head = el("div", { class: "cluster-head" },
    el("div", {}, el("h2", {}, c.name), el("div", { class: "muted" }, `${c.host} · MCP ${c.mcp_name || "—"}`)),
    el("div", { class: "chips" },
      chip(login(c) ? "logged in" : "not logged in", login(c) ? "up" : "down"),
      chip(`agent tunnel ${tunnelUp(c) ? "up" : (c.tunnel || {}).state || "down"}`, tunnelTone(c))),
    el("div", { class: "actions" }, login(c)
      ? action("Log out", async () => { await api(path(name, "logout"), { method: "POST" }); ctx.render(); })
      : action("Log in", () => runStep(c, "login"), { primary: true })));
  const pages = { overview, jobs: jobsTab, apps: appsTab, installed, storage: storageTab, files, agents, settings,
                  widget: widgetTab };
  const body = await (tab === "apps" && sub ? appPage(c, sub) : (pages[tab] || overview)(c, local, sub));
  followWhileStarting([c]);
  return [head, body];
}

async function overview(c, local) {
  const { api } = ctx;
  const step = nextStep(c, local, (what) => runStep(c, what));
  const jobs = login(c) ? await jobsSummary(c.name) : null;
  let st = null;
  try { st = await api(path(c.name, "storage")); } catch { /* an older console */ }
  const apps = await appList();
  const appIds = apps.map((a) => a.id);
  const states = await Promise.all(appIds.map((id) => api(`apps/${id}/${enc(c.name)}`).catch(() => null)));
  const older = c.tunnel && c.tunnel.hpclib_version && local && versionCompare(c.tunnel.hpclib_version, local) < 0;
  const tile = (title, value, detail, link) => el("a", { class: "tile", href: link || null },
    el("div", { class: "tile-title" }, title), el("div", { class: "tile-value" }, value),
    detail ? el("div", { class: "muted small" }, detail) : null);
  const running = states.filter((s) => s && s.state === "up");
  const missing = states.filter((s) => s && s.install && s.install.state === "missing");
  return el("div", {},
    step ? el("section", { class: "next-step" }, el("div", {}, el("strong", {}, "Next: "), step[2]),
      stepAction(step)) : null,
    el("div", { class: "tiles" },
      tile("Agent tunnel", el("hpc-state", { state: (c.tunnel || {}).state || "down" }),
        tunnelUp(c) ? `REST server on ${(c.tunnel || {}).hostname || "a compute node"}` : "agents can't reach the cluster",
        null),
      tile("hpclib", c.tunnel && c.tunnel.hpclib_version ? c.tunnel.hpclib_version : "—",
        older ? `this console has ${local}: update` : (local ? `this console: ${local}` : null), `#/c/${enc(c.name)}/installed`),
      tile("Jobs", countsText(jobs), login(c) ? "queued, running, last 24 h" : "log in to see them", `#/c/${enc(c.name)}/jobs`),
      tile("Storage", storageTotal(st) || el("span", { class: "muted" }, "not scanned"),
        `${(c.work_dirs || []).join(", ") || "no work directory"}${st && st.scanned ? ` · scanned ${ago(st.scanned)}` : ""}`,
        `#/c/${enc(c.name)}/storage`),
      tile("Apps", running.length ? `${running.length} running` : "none running",
        missing.length ? `${missing.length} not installed` : `${states.filter(Boolean).length} available`, `#/c/${enc(c.name)}/apps`)),
    widgets.strip("cluster.overview", { cluster: c.name }),
    el("h3", {}, "Apps"),
    el("table", { class: "compact" }, el("tbody", {}, states.filter(Boolean).map((s) => {
      const title = (apps.find((a) => a.id === s.app) || {}).title || s.app;
      const notInstalled = s.install && s.install.state === "missing";
      return el("tr", {},
        el("td", {}, el("a", { href: appHref(c.name, s.app), class: "cluster-link" }, title)),
        el("td", {}, notInstalled ? chip("not installed", "starting")
          : el("hpc-state", { state: s.state, label: s.state === "queued" ? `queued: ${s.status || ""}`
            : { up: "running", down: "stopped", starting: "starting", error: "error" }[s.state] || s.state })),
        el("td", { class: "next" }, s.state === "up" && s.url
          ? el("a", { class: "button primary", href: s.url, target: "_blank", rel: "noopener noreferrer" }, "Open")
          : el("a", { class: "button", href: notInstalled ? href(c.name, "installed") : appHref(c.name, s.app) },
              notInstalled ? "Install…" : s.state === "down" ? "Start…" : "View…")));
    }))));
}

// ---------------------------------------------------------------- Jobs

async function jobsTab(c) {
  const { api } = ctx;
  if (!login(c)) return el("p", { class: "muted" }, "Log in to see this cluster's jobs.");
  let j;
  try { j = await api(path(c.name, "jobs")); } catch (err) { return ctx.errorBox(err); }
  const n = j.counts;
  const cancel = (job) => (["agent", "other"].includes(job.source.kind) ? action("Cancel", async () => {
    if (!await confirmDialog(`Cancel job ${job.id}?`, `${job.name} (${job.source.label}) is stopped with scancel; its ` +
                             "output so far stays where it is.", "Cancel job")) return;
    await api(path(c.name, "jobs/cancel"), { method: "POST", body: { job: job.id } });
    ctx.render();
  }) : el("a", { class: "button", href: `#/c/${enc(c.name)}/apps` }, job.source.kind === "agent-tunnel" ? "Overview" : "Apps"));
  const source = (job) => chip(job.source.label, SOURCE_TONE[job.source.kind] || "down", job.source.token ? `token ${job.source.token}` : null);
  const requested = (job) => [job.cpus && `${job.cpus} CPU`, job.memory && job.memory !== "0" && `${job.memory}`, job.gres]
    .filter(Boolean).join(" · ");
  const table = (heads, rows, empty) => rows.length ? el("table", { class: "jobs" },
    el("thead", {}, el("tr", {}, heads.map((h) => el("th", {}, h)))), el("tbody", {}, rows))
    : el("p", { class: "muted" }, empty);
  return el("div", {},
    el("div", { class: "toolbar" },
      el("div", { class: "counts" },
        el("a", { href: "#queued", class: "count starting" }, el("strong", {}, String(n.queued)), " queued"),
        el("a", { href: "#running", class: "count up" }, el("strong", {}, String(n.running)), " running"),
        el("a", { href: "#finished", class: "count" }, el("strong", {}, String(n.finished)), " finished today"),
        n.failed ? el("a", { href: "#finished", class: "count error" }, el("strong", {}, String(n.failed)), " failed") : null),
      el("span", { class: "muted" }, `as of ${new Date(j.at * 1000).toLocaleTimeString()}`),
      action("Refresh", () => ctx.render())),
    el("h3", { id: "queued" }, `Queued (${n.queued})`),
    table(["Job", "From", "Partition", "Asked for", "Waiting because", "Expected start", ""], j.queued.map((job) => el("tr", {},
      el("td", {}, el("strong", {}, job.name), el("div", { class: "muted small" }, job.id)), el("td", {}, source(job)),
      el("td", {}, job.partition), el("td", {}, requested(job)),
      el("td", { title: job.reason }, REASONS[job.reason] || job.reason),
      el("td", {}, job.start ? job.start.replace("T", " ") : el("span", { class: "muted" }, "not estimated")),
      el("td", { class: "next" }, cancel(job)))), "Nothing is waiting."),
    el("h3", { id: "running" }, `Running (${n.running})`),
    table(["Job", "From", "Node", "Time used", "Asked for", ""], j.running.map((job) => el("tr", {},
      el("td", {}, el("strong", {}, job.name), el("div", { class: "muted small" }, job.id)), el("td", {}, source(job)),
      el("td", {}, job.node_list), el("td", {}, `${job.elapsed} of ${job.time_limit}`), el("td", {}, requested(job)),
      el("td", { class: "next" }, cancel(job)))), "Nothing is running."),
    el("h3", { id: "finished" }, `Finished in the last 24 hours (${n.finished})`),
    table(["Job", "From", "Result", "Took", "Peak memory", "Ended"], j.finished.map((job) => el("tr", {},
      el("td", {}, el("strong", {}, job.name), el("div", { class: "muted small" }, job.id)), el("td", {}, source(job)),
      el("td", {}, job.state === "COMPLETED" ? chip("completed", "up")
        : chip(`${job.state.toLowerCase().replaceAll("_", " ")}${job.exit_code && job.exit_code !== "0:0" ? ` (${job.exit_code})` : ""}`, "error")),
      el("td", {}, job.elapsed),
      el("td", {}, job.peak_memory_mb === null ? el("span", { class: "muted" }, "—")
        : `${job.peak_memory_mb >= 1024 ? (job.peak_memory_mb / 1024).toFixed(1) + " GB" : Math.round(job.peak_memory_mb) + " MB"}` +
          (job.requested_memory ? ` of ${job.requested_memory}` : "")),
      el("td", {}, (job.end || "").replace("T", " ")))), "Nothing finished in the last day."));
}

// ---------------------------------------------------------------- Apps

async function appsTab(c) {
  const { api } = ctx;
  const ids = await appList();
  const rows = await Promise.all(ids.map(async (a) => {
    const row = el("hpc-app-session", { app: a.id, "app-title": a.title, cluster: c.name, "by-app": "",
                                        "app-href": appHref(c.name, a.id) });
    try { row.data = await api(`apps/${a.id}/${enc(c.name)}`); } catch { return null; }
    return row;
  }));
  const table = el("table", { class: "rows" }, el("thead", {}, el("tr", {}, ["App", "State", ""].map((h) => el("th", {}, h)))),
    rows.filter(Boolean));
  // an app's settings are on its own page here
  table.addEventListener("hpc-app-settings", (e) => { e.stopPropagation(); location.hash = appHref(c.name, e.detail.app); });
  return el("div", {}, table,
    el("p", { class: "muted small" }, "Each app's page has its settings, its tools and what it has installed here."));
}

// ---------------------------------------------------------------- one app: #/c/NAME/apps/APP

async function appPage(c, id) {
  const { api } = ctx;
  const apps = await appList();
  const a = apps.find((x) => x.id === id);
  if (!a) return ctx.errorBox(new Error(`No app called ${id}; packages add apps (Packages, at the top).`));
  // a package's tab apps sit beside their parent: one row of tabs for the family
  const parentId = a.tab ? a.tab.parent : a.id;
  const parent = apps.find((x) => x.id === parentId) || a;
  const family = [parent, ...apps.filter((x) => x.tab && x.tab.parent === parentId)];
  const s = await api(`apps/${id}/${enc(c.name)}`);
  const row = el("hpc-app-session", { app: id, "app-title": a.title, cluster: c.name, "by-app": "", "app-page": "" });
  row.data = s;
  // its settings, drawn as the row's settings panel would be, but in place on the page
  const settingsBox = el("div", { class: "app-settings" }, el("p", { class: "muted" }, "Loading settings…"));
  const inline = {
    installBox: row.installBox,
    showDetail: (kind, panel) => { panel.removeAttribute("dismissible"); settingsBox.replaceChildren(panel); return panel; },
    hideDetail: () => {},
    refresh: () => row.refresh(),
  };
  ctx.appSettings(id, s, inline).catch((err) => settingsBox.replaceChildren(ctx.errorBox(err)));
  const kindText = a.kind === "login" ? "Runs on the login node, through the console's ssh login."
    : "Runs in a SLURM job reached through its own tunnel.";
  return el("div", { class: "app-page" },
    family.length > 1 ? subTabs(family.map((x) => [appHref(c.name, x.id), x.tab ? x.tab.label : x.title, x.id]), id) : null,
    el("div", { class: "toolbar" }, el("h3", {}, a.title),
      el("a", { class: "muted small", href: `#/${parentId}` }, `${parent.title} on all clusters`)),
    el("p", { class: "muted" }, a.description || `${kindText} It runs as you, with your full permissions on ` +
      "the cluster (it is not the agents' sandbox)."),
    el("table", { class: "rows" }, el("thead", {}, el("tr", {}, ["App", "State", ""].map((h) => el("th", {}, h)))), row),
    el("h3", {}, "Settings"),
    settingsBox);
}

// ---------------------------------------------------------------- Installed

async function installed(c, local) {
  const { api } = ctx;
  const ids = await appList();
  const states = await Promise.all(ids.map((a) => api(`apps/${a.id}/${enc(c.name)}`).then((s) => [a, s]).catch(() => null)));
  const remote = c.tunnel && c.tunnel.hpclib_version;
  const older = remote && local && versionCompare(remote, local) < 0;
  const needLogin = !login(c);
  const label = { installed: ["installed", "up"], missing: ["not installed", "starting"], unknown: ["can't check", "down"],
                  unchecked: ["not checked", "down"], nothing: ["nothing to install", "down"] };
  const rows = [
    el("tr", {}, el("td", {}, el("strong", {}, "hpclib")),
      el("td", {}, remote ? chip(older ? "out of date" : "current", older ? "starting" : "up") : chip("unknown", "down")),
      el("td", { class: "muted" }, remote ? `${remote} on the cluster · ${local} here` : "start the agent tunnel to see its version"),
      el("td", { class: "next" }, action(older ? "Update" : "Reinstall", async () => {
        await api(path(c.name, "install"), { method: "POST", body: {} }); ctx.render(); }, { primary: !!older, disabled: needLogin }))),
    el("tr", {}, el("td", {}, el("strong", {}, "Agent setup")),
      el("td", {}, chip(c.set_up ? "set up" : "not set up", c.set_up ? "up" : "starting")),
      el("td", { class: "muted" }, c.set_up ? `work directories: ${(c.work_dirs || []).join(", ")}` : "templates, server config and the agent token"),
      el("td", { class: "next" }, el("a", { class: "button", href: href(c.name, "agents") }, c.set_up ? "Set up again…" : "Set up…"))),
  ];
  for (const item of states.filter(Boolean)) {
    const [a, s] = item;
    const st = (s.install || {}).state || "unchecked";
    if (st === "nothing") continue;
    const [text, tone] = label[st] || [st, "down"];
    const verb = st === "installed" ? ((s.reinstall || {}).label || "Reinstall") : "Install";
    rows.push(el("tr", {}, el("td", {}, el("a", { href: appHref(c.name, a.id), class: "cluster-link" }, a.title)),
      el("td", {}, chip(text, tone)),
      el("td", { class: "muted" }, (s.install || {}).message || ""),
      el("td", { class: "next" }, el("div", { class: "actions" },
        action("Check", async () => { await api(`apps/${a.id}/${enc(c.name)}/check`, { method: "POST" }); ctx.render(); },
               { disabled: needLogin }),
        action(verb, async () => {
          await api(`apps/${a.id}/${enc(c.name)}/install`, { method: "POST", body: { force: st === "installed" } });
          ctx.render();
        }, { primary: st !== "installed", disabled: needLogin })))));
  }
  const checkAll = action("Check all", async () => {
    await Promise.all(states.filter(Boolean).filter(([, s]) => (s.install || {}).state !== "nothing")
      .map(([a]) => api(`apps/${a.id}/${enc(c.name)}/check`, { method: "POST" }).catch(() => null)));
    ctx.render();
  }, { disabled: needLogin });
  return el("div", {},
    el("div", { class: "toolbar" }, el("h3", {}, "What hpclib has put on this cluster"), checkAll),
    el("table", { class: "compact installed" }, el("thead", {}, el("tr", {}, ["", "State", "Details", ""].map((h) => el("th", {}, h)))),
      el("tbody", {}, rows)),
    el("p", { class: "muted small" }, "Install output and progress appear on each app's page (follow its name)."));
}

// ---------------------------------------------------------------- Storage

async function storageTab(c) {
  const { api } = ctx;
  let st;
  try { st = await api(path(c.name, "storage")); } catch (err) { return ctx.errorBox(err); }
  const scanning = (st.scan || {}).state === "running";
  const scan = action(scanning ? "Scanning…" : "Scan now", async () => {
    await api(path(c.name, "storage/scan"), { method: "POST" });
    ctx.render();
  }, { primary: !st.scanned, disabled: scanning || !login(c) });
  if (scanning) setTimeout(() => { if (location.hash.endsWith("/storage")) ctx.render(); }, 5000);
  const dirs = st.dirs || [];
  const cards = dirs.map((d) => {
    if (d.missing) return el("section", { class: "card" }, el("h3", {}, d.path), el("p", { class: "muted" }, "doesn't exist"));
    const top = d.children[0] ? d.children[0].kb : 0;
    return el("section", { class: "card storage" },
      el("div", { class: "storage-head" }, el("h3", {}, d.path),
        el("div", { class: "storage-total" }, el("strong", {}, size(d.kb)), el("span", { class: "muted" }, ` · ${count(d.files)} files`),
          d.partial ? chip("partial: the scan hit its time limit", "starting") : null)),
      el("table", { class: "bars" }, el("tbody", {}, d.children.map((ch) => el("tr", {},
        el("td", { class: "bar-name" }, ch.path.slice(d.path.replace(/\/$/, "").length + 1) || ch.path),
        el("td", { class: "bar-cell" }, el("div", { class: "bar", style: `width: ${top ? Math.max(1, 100 * ch.kb / top) : 0}%` })),
        el("td", { class: "num" }, size(ch.kb)), el("td", { class: "num muted" }, d.kb ? `${Math.round(100 * ch.kb / d.kb)}%` : ""))))));
  });
  return el("div", {},
    el("div", { class: "toolbar" },
      el("span", {}, st.scanned ? `Scanned ${ago(st.scanned)}` : "Not scanned yet", (st.scan || {}).state === "failed"
        ? el("span", { class: "error-box" }, ` · the last scan failed: ${st.scan.error}`) : null),
      scan, el("span", { class: "muted small" }, "du over your login, at low priority, up to 5 minutes per directory")),
    quotaCard(c, st),
    cards.length ? cards : el("p", { class: "muted" }, `Work directories: ${(st.work_dirs || []).join(", ") || "none set"}.`));
}

// the quota checks (lib/quota.sh on the cluster): the storage quota and the project's balance, each the site's own
// text; which commands run is the cluster's Settings → Quota checks
function quotaCard(c, st) {
  const q = st.quotas || (st.quota ? { storage: { command: st.quota.command, source: "default", output: st.quota.lines } } : null);
  const check = action(q ? "Check again" : "Check quotas", async () => {
    await ctx.api(path(c.name, "quota"));
    ctx.render();
  }, { disabled: !login(c) });
  const one = (kind, label) => {
    const r = (q || {})[kind];
    if (!r) return null;
    const meta = r.command ? `${r.command} · ${{ env: "from the environment", settings: "set for this cluster",
      default: "default" }[r.source] || r.source}${r.exit_code ? ` · exit ${r.exit_code}` : ""}` : null;
    return el("div", { class: `quota-check quota-${kind}` },
      el("h4", {}, label, meta ? el("span", { class: "muted" }, ` · ${meta}`) : null),
      r.command ? el("pre", { class: "quota" }, (r.output || []).join("\n") || "(no output)")
        : el("p", { class: "muted" }, r.source === "none" ? "Turned off." :
          "No check found on this cluster: set one in Settings → Quota checks."));
  };
  return el("section", { class: "card" },
    el("div", { class: "toolbar" }, el("h3", {}, "Quotas"),
      q && q.checked ? el("span", { class: "muted small" }, `checked ${ago(q.checked)}`) : null, check,
      el("a", { href: href(c.name, "settings"), class: "small" }, "Change the checks")),
    q ? [one("storage", "Storage"), one("project", "Project")]
      : el("p", { class: "muted" }, "Not checked yet: Check quotas (or Scan now) runs the cluster's commands over your login."));
}

// ---------------------------------------------------------------- Settings

async function settings(c) {
  return el("div", {}, await ctx.settingsCards(c));
}

// ---------------------------------------------------------------- a packaged widget's tab

async function widgetTab(c, local, key) {
  const w = widgets.forSlot("cluster.tab").find((x) => x.key === key);
  if (!w) return ctx.errorBox(new Error(`No widget ${key}: it was removed or hidden (Interface Settings → Widgets).`));
  return el("div", {}, w.description ? el("p", { class: "muted" }, w.description) : null,
    widgets.frame(w, { cluster: c.name }, { bare: true }));
}

// ---------------------------------------------------------------- Files, Agents

async function files(c) {
  return el("div", {}, await ctx.files(c.name));
}

async function agents(c, local, sub) {
  const page = AGENT_PAGES.some(([id]) => id === sub) ? sub : "";
  const tabs = subTabs(AGENT_PAGES.map(([id, label]) => [href(c.name, "agents", id), label, id]), page);
  if (page === "activity") return el("div", {}, tabs, await ctx.activity(c.name));
  if (page === "keys") return el("div", {}, tabs, await ctx.sshKeys(c.name));
  // the agent profile's row: its tunnel (Start, Stop, Log), Update hpclib and setup_agents (Set up), as on the old
  // Clusters page; its panels open under it
  const row = el("hpc-cluster-row", { cluster: c.name, local });
  row.data = c;
  return el("div", {}, tabs,
    el("p", { class: "muted" }, "Agents reach this cluster through its REST server, which runs in a job behind the " +
      "agent tunnel. Setup installs the job templates, the sandboxed server's config and the agent's token."),
    el("table", { class: "rows" },
      el("thead", {}, el("tr", {}, ["Cluster", "Tunnel", "Login", "Port", "Token", ""].map((h) => el("th", {}, h)))), row),
    mcpNameCard(c), tunnelActionsCard(c));
}

// Tunnel actions (rest_actions.py on the cluster): what each tunnel offers agents, which are on, and whether each
// can run now. Off until turned on here; an action that writes outside the cluster (data-transfer's put) is off
// even then until turned on by itself.
function tunnelActionsCard(c) {
  const box = el("section", { class: "card", id: "tunnel-actions" });
  const head = el("h3", {}, "Tunnel actions");
  const intro = el("p", { class: "muted" }, "Operations a tunnel runs for agents with your access, such as copying " +
    "files to and from your group's SMB server with your sign-in. They run as you, outside the agents' sandbox, " +
    "with typed, checked parameters, and each call is in the audit log (Activity). Agents see only what is on.");
  if (!tunnelUp(c) || !c.has_owner_token) {
    box.append(head, intro, el("p", { class: "muted" }, "Start the agent tunnel to see and change them."));
    return box;
  }
  const notice = el("p", { class: "notice", role: "status" });
  const draw = async (refresh = false) => {
    const [{ tunnels }, conf] = await Promise.all([
      ctx.api(path(c.name, `rest/actions${refresh ? "?refresh=1" : ""}`)), ctx.api(path(c.name, "rest/admin/config"))]);
    const section = JSON.parse(JSON.stringify((conf.config || {}).tunnel_actions || {}));
    const save = async (message) => {
      await ctx.api(path(c.name, "rest/admin/config"), { method: "PUT", body: { changes: { tunnel_actions: section } } });
      notice.textContent = message;
      await draw();
    };
    const cards = tunnels.map((t) => {
      if (t.error) return el("div", { class: "error-box small" }, `${t.tunnel}: ${t.error}`);
      const entry = section[t.tunnel] = section[t.tunnel] || { enabled: false };
      const avail = t.availability;
      const status = !t.enabled ? chip("off", "down")
        : !avail ? chip("checking…", "down")
        : avail.available ? chip("available", "up") : chip("not available", "starting");
      const detail = avail && avail.available && avail.detail
        ? Object.entries(avail.detail).filter(([, v]) => v).map(([k, v]) =>
          `${k}: ${typeof v === "object" ? Object.values(v).filter(Boolean).join(" until ") : v}`).join(" · ")
        : avail && avail.reason;
      const on = el("input", { type: "checkbox", checked: t.enabled, "aria-label": `Turn on ${t.title} actions` });
      on.addEventListener("change", () => {
        entry.enabled = on.checked;
        save(on.checked ? `${t.title}: agents may use its actions that are on.` : `${t.title}: off for agents.`)
          .catch((err) => box.prepend(ctx.errorBox(err)));
      });
      const rows = t.actions.map((a) => {
        const box2 = el("input", { type: "checkbox", checked: a.enabled, disabled: !t.enabled,
                                   "aria-label": `Turn on ${t.tunnel}/${a.name}` });
        box2.addEventListener("change", async () => {
          if (box2.checked && a.writes === "remote" && !await confirmDialog(`Let agents ${a.title.toLowerCase()}?`,
              `${a.title} writes outside the cluster with your access (${t.title}). Agents could add or replace ` +
              "files there. Turn it on only while you want agents to do this.", "Turn on")) {
            box2.checked = false;
            return;
          }
          entry.actions = entry.actions || {};
          if (box2.checked === a.default) delete entry.actions[a.name]; else entry.actions[a.name] = box2.checked;
          save(`${t.tunnel}/${a.name}: ${box2.checked ? "on" : "off"}.`).catch((err) => box.prepend(ctx.errorBox(err)));
        });
        const params = Object.entries(a.params).map(([k, p]) => `${k} (${p.type}${p.access ? `, ${p.access}` : ""})`).join(", ");
        return el("tr", {},
          el("td", {}, box2),
          el("td", {}, el("strong", {}, a.title), " ", el("span", { class: "muted small" }, `${t.tunnel}/${a.name}`),
            el("div", { class: "muted small" }, a.description),
            el("div", { class: "muted small" }, `${a.mode === "job" ? "runs as a SLURM job" : "answers at once"} · ` +
              `needs the ${a.scope} scope · ${params || "no parameters"}`)),
          el("td", {}, el("span", { class: "chips" },
            a.writes === "remote" ? chip("writes outside the cluster", "error") : null,
            a.writes === "cluster" ? chip("writes to your directories", "starting") : null,
            !a.default ? chip("off by default", "down") : null)));
      });
      return el("div", { class: "action-tunnel" },
        el("div", { class: "toolbar" }, el("label", {}, on, " ", el("strong", {}, t.title)), status,
          el("span", { class: "muted small" }, detail || "")),
        t.description ? el("p", { class: "muted small" }, t.description) : null,
        el("table", { class: "compact" }, el("tbody", {}, rows)));
    });
    box.replaceChildren(head, intro, notice,
      el("div", { class: "toolbar" }, action("Check availability", () => draw(true))),
      ...(cards.length ? cards : [el("p", { class: "muted" }, "No installed tunnel offers actions on this cluster " +
        "(update hpclib there: Installed).")]));
  };
  box.append(head, intro, el("p", { class: "muted" }, "Loading…"));
  draw().catch((err) => box.replaceChildren(head, intro, ctx.errorBox(err)));
  return box;
}

// What LLM clients call this cluster's MCP server; renaming it updates mcp.json and the clients kept up to date
function mcpNameCard(c) {
  const input = el("input", { value: c.mcp_name || "", size: 28, "aria-label": "MCP server name",
                              pattern: "[A-Za-z0-9][A-Za-z0-9_-]{0,63}" });
  const save = action("Rename", async () => {
    const out = await ctx.api(path(c.name, "mcp-name"), { method: "PUT", body: { name: input.value.trim() } });
    c.mcp_name = out.mcp_name;
    const clients = Object.entries(out.llm_clients || {});
    const failed = clients.filter(([, r]) => typeof r === "string" && r.startsWith("error"));
    return `Now ${out.mcp_name}.` + (clients.length ? ` Updated in ${clients.length} LLM client${clients.length > 1 ? "s" : ""}` +
      (failed.length ? ` (${failed.map(([id, r]) => `${id}: ${r}`).join("; ")})` : "") + "."
      : " Write it to your LLM clients on Console Settings.");
  }, { primary: true, result: true });
  return el("section", { class: "card" },
    el("h3", {}, "MCP server name"),
    el("p", { class: "muted" }, "What your LLM clients call this cluster (a tool list shows it as the server's " +
      "name). Letters, digits, - and _. LLM clients kept up to date on Console Settings get the new name; the old " +
      "one is removed from them."),
    el("div", { class: "toolbar" }, input, save));
}
