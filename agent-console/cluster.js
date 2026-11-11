// The cluster-first pages: #/c lists the clusters with what each needs, and #/c/NAME/TAB[/SUB] is one cluster's
// page (Overview, Jobs, Apps, Installed, Storage, Files, SSH keys, Agents, Settings); #/c/NAME/apps/APP is one app there, with
// its controls, tools and settings, and a package's tabs for it. They use the console's existing API, plus
// GET clusters/NAME/jobs[/ID], GET/POST clusters/NAME/storage[/scan] and GET clusters/NAME/quota. app.js passes what they share with it (init).
import { el, action, confirmDialog, connectClients, ownerRecovery, appUrl } from "./components.js";
import * as widgets from "./widgets.js";
import { syncEntry } from "./sync.js";

// { api, errorBox, render, settingsCards: (c) => [cards], addCluster: () => [button, area], files: (name) => page,
//   activity: (name) => page, sshKeys: (name) => page, appSettings: (app, session, row) => (draws in row.showDetail) }
let ctx = null;
export function init(given) { ctx = given; }

const enc = encodeURIComponent;
const path = (name, rest = "") => `clusters/${enc(name)}${rest ? "/" + rest : ""}`;
export const CLUSTER_TABS = [["overview", "Overview"], ["jobs", "Jobs"], ["apps", "Apps"], ["installed", "Installed"],
                             ["storage", "Storage"], ["files", "Files"], ["ssh", "SSH keys"], ["agents", "Agents"], ["settings", "Settings"]];
// the tabs, with a packaged widget's own (slot cluster.tab): [id, label, sub]
export function tabs() {
  return [...CLUSTER_TABS.map(([id, label]) => [id, label, ""]),
          ...widgets.forSlot("cluster.tab").map((w) => ["widget", w.title, w.key])];
}
const AGENT_PAGES = [["", "Tunnel and setup"], ["activity", "Activity"]];
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

export function chip(label, tone = "down", title = null) {
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
  if (what === "start") {
    // Reuse the Agents page's Stop action and wait for it before starting again.
    await api(path(c.name, "tunnel/stop"), { method: "POST" });
    await api(path(c.name, "tunnel/start"), { method: "POST", body: {} });
  }
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
  const dirs = (st && st.dirs || []).filter((d) => !d.missing && !d.custom && !d.path.includes("/.local/tunnels"));
  if (!st || !st.scanned || !dirs.length) return null;
  const some = dirs.some((d) => d.partial);       // folders still unscanned or timed out: the totals are a lower bound
  return `${some ? "≥ " : ""}${size(dirs.reduce((a, d) => a + (d.kb || 0), 0))} · ${count(dirs.reduce((a, d) => a + (d.files || 0), 0))} files`;
}

function storageText(st) {
  const dirs = (st && st.dirs || []).filter((d) => !d.missing && !d.custom && !d.path.includes("/.local/tunnels"));
  if (!st || !st.scanned || !dirs.length) return el("span", { class: "muted" }, "not scanned");
  const kb = dirs.reduce((a, d) => a + (d.kb || 0), 0), files = dirs.reduce((a, d) => a + (d.files || 0), 0);
  const some = dirs.some((d) => d.partial);
  return el("span", {}, `${some ? "≥ " : ""}${size(kb)} · ${count(files)} files`,
    el("span", { class: "muted" }, ` · ${some ? "partial, " : ""}${ago(st.scanned)}`));
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
  // the Add cluster form opens under the toolbar, full width (inside it, it was squeezed beside the heading)
  const [addBtn, addArea] = ctx.addCluster();
  return [
    el("div", { class: "toolbar" }, el("h2", {}, "Clusters"),
      el("span", { class: "muted" }, local ? `hpclib ${local} on this machine` : ""), addBtn),
    addArea,
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
  const pages = { overview, jobs: jobsTab, apps: appsTab, installed, storage: storageTab, files, ssh: sshTab, agents, settings,
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
  const apps = (await appList()).filter((a) => appPlace(c, a.id) !== "hidden");
  const appIds = apps.map((a) => a.id);
  const states = await Promise.all(appIds.map((id) => api(`apps/${id}/${enc(c.name)}`).catch(() => null)));
  // the Overview lists the pinned apps and running ones; the rest are on the Apps tab
  const listed = states.filter((s) => s && (appPlace(c, s.app) === "pinned" || ACTIVE_STATES.includes(s.state)));
  const unlisted = states.filter(Boolean).length - listed.length;
  const older = c.tunnel && c.tunnel.hpclib_version && local && versionCompare(c.tunnel.hpclib_version, local) < 0;
  const tile = (title, value, detail, link) => el("a", { class: "tile", href: link || null },
    el("div", { class: "tile-title" }, title), el("div", { class: "tile-value" }, value),
    detail ? el("div", { class: "muted small" }, detail) : null);
  const running = states.filter((s) => s && s.state === "up");
  const missing = states.filter((s) => s && s.install && s.install.state === "missing");
  return el("div", {},
    login(c) && !c.ssh_login_key && c.preset?.ssh_keys !== "site-managed"
      ? el("section", { class: "card", id: "ssh-first-login" }, el("h3", {}, "Set up SSH for future logins"),
          el("p", {}, "Now that you're logged in, you can install a public key from this computer without starting agents."),
          el("a", { class: "button", href: href(c.name, "ssh") }, "Set up login key…")) : null,
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
    !listed.length ? el("p", { class: "muted" }, "No app is pinned or running here. ",
      el("a", { href: `#/c/${enc(c.name)}/apps` }, `All ${unlisted} on the Apps tab`)) : null,
    el("table", { class: "compact" }, el("tbody", {}, listed.map((s) => {
      const title = (apps.find((a) => a.id === s.app) || {}).title || s.app;
      const notInstalled = s.install && s.install.state === "missing";
      return el("tr", {},
        el("td", {}, el("a", { href: appHref(c.name, s.app), class: "cluster-link" }, title)),
        el("td", {}, notInstalled ? chip("not installed", "starting")
          : el("hpc-state", { state: s.state, label: s.state === "queued" ? `queued: ${s.status || ""}`
            : { up: "running", down: "stopped", starting: "starting", error: "error" }[s.state] || s.state })),
        el("td", { class: "next" }, s.state === "up" && s.url
          ? el("a", { class: "button primary", href: appUrl(s.url, s), target: "_blank", rel: "noopener noreferrer" }, "Open")
          : el("a", { class: "button", href: notInstalled ? href(c.name, "installed") : appHref(c.name, s.app) },
              notInstalled ? "Install…" : s.state === "down" ? "Start…" : "View…")));
    }))),
    listed.length && unlisted ? el("p", { class: "muted small" },
      el("a", { href: `#/c/${enc(c.name)}/apps` }, `${unlisted} more on the Apps tab`)) : null);
}

// ---------------------------------------------------------------- Jobs

async function jobsTab(c, _local, sub = "") {
  const { api } = ctx;
  if (!login(c)) return el("p", { class: "muted" }, "Log in to see this cluster's jobs.");
  if (sub === "queue") return queuePage(c);
  if (sub) return jobPage(c, sub);
  let j;
  try { j = await api(path(c.name, "jobs")); } catch (err) { return ctx.errorBox(err); }
  const n = j.counts;
  const cancel = (job) => (["agent", "other"].includes(job.source.kind) ? action("Cancel", async () => {
    if (!await confirmDialog(`Cancel job ${job.id}?`, `${job.name} (${job.source.label}) is stopped with scancel; its ` +
                             "output so far stays where it is.", "Cancel job")) return;
    await api(path(c.name, "jobs/cancel"), { method: "POST", body: { job: job.id } });
    ctx.render();
  }) : null);
  // every job has an overview page (its settings, script and output); an app's or the tunnel's job also links to its app
  const overview = (job) => el("a", { class: "button", href: href(c.name, "jobs", job.id) }, "Overview");
  const jobCell = (job) => el("td", {}, el("a", { class: "job-link", href: href(c.name, "jobs", job.id) }, job.name),
    el("div", { class: "muted small" }, job.id));
  const actions = (job) => el("td", { class: "next" }, overview(job), cancel(job));
  const source = (job) => chip(job.source.label, SOURCE_TONE[job.source.kind] || "down", job.source.token ? `token ${job.source.token}` : null);
  const requested = (job) => [job.cpus && `${job.cpus} CPU`, job.memory && job.memory !== "0" && `${job.memory}`, job.gres]
    .filter(Boolean).join(" · ");
  const table = (heads, rows, empty) => rows.length ? el("table", { class: "jobs" },
    el("thead", {}, el("tr", {}, heads.map((h) => el("th", {}, h)))), el("tbody", {}, rows))
    : el("p", { class: "muted" }, empty);
  const failedState = (job) => job.state !== "COMPLETED";
  let v = j;                                         // the jobs that match the filter
  const finishedRows = (only) => v.finished.filter((job) => !only || failedState(job)).map((job) => el("tr", {},
    jobCell(job), el("td", {}, source(job)),
    el("td", {}, job.state === "COMPLETED" ? chip("completed", "up")
      : chip(`${job.state.toLowerCase().replaceAll("_", " ")}${job.exit_code && job.exit_code !== "0:0" ? ` (${job.exit_code})` : ""}`, "error")),
    el("td", {}, job.elapsed),
    el("td", {}, job.peak_memory_mb === null ? el("span", { class: "muted" }, "—")
      : `${job.peak_memory_mb >= 1024 ? (job.peak_memory_mb / 1024).toFixed(1) + " GB" : Math.round(job.peak_memory_mb) + " MB"}` +
        (job.requested_memory ? ` of ${job.requested_memory}` : "")),
    el("td", {}, (job.end || "").replace("T", " "))));
  const sections = {
    queued: () => [el("h3", { id: "queued" }, `Queued (${n.queued})`),
      table(["Job", "From", "Partition", "Asked for", "Waiting because", "Expected start", ""], v.queued.map((job) => el("tr", {},
        jobCell(job), el("td", {}, source(job)),
        el("td", {}, job.partition), el("td", {}, requested(job)),
        el("td", { title: job.reason }, REASONS[job.reason] || job.reason),
        el("td", {}, job.start ? job.start.replace("T", " ") : el("span", { class: "muted" }, "not estimated")),
        actions(job))), "Nothing is waiting.")],
    running: () => [el("h3", { id: "running" }, `Running (${n.running})`),
      table(["Job", "From", "Node", "Time used", "Asked for", ""], v.running.map((job) => el("tr", {},
        jobCell(job), el("td", {}, source(job)),
        el("td", {}, job.node_list), el("td", {}, `${job.elapsed} of ${job.time_limit}`), el("td", {}, requested(job)),
        actions(job))), "Nothing is running.")],
    finished: () => [el("h3", { id: "finished" }, `Finished in the last 24 hours (${n.finished})`),
      table(["Job", "From", "Result", "Took", "Peak memory", "Ended"], finishedRows(false), "Nothing finished in the last day.")],
    failed: () => [el("h3", { id: "failed" }, `Failed in the last 24 hours (${n.failed})`),
      table(["Job", "From", "Result", "Took", "Peak memory", "Ended"], finishedRows(true), "Nothing failed in the last day.")],
  };
  // the counts filter the page to one kind of job (click again for all); not links: the page's own address is
  // the route (#/c/NAME/jobs), and a fragment there would leave it
  const body = el("div", {});
  const buttons = [];
  const draw = () => {
    const only = jobFilter[c.name];
    const keep = jobMatcher(jobText[c.name] || "");
    v = { queued: j.queued.filter(keep), running: j.running.filter(keep), finished: j.finished.filter(keep) };
    body.replaceChildren(...(only ? sections[only]() : ["queued", "running", "finished"].flatMap((k) => sections[k]())));
    for (const b of buttons) b.setAttribute("aria-pressed", String(b.dataset.kind === only));
  };
  const count = (kind, tone, label, value) => {
    const b = el("button", { type: "button", class: `count ${tone}`, "data-kind": kind,
      title: `Show only ${label.trim()} jobs (click again for all)`,
      onclick: () => { jobFilter[c.name] = jobFilter[c.name] === kind ? null : kind; draw(); } },
      el("strong", {}, String(value)), label);
    buttons.push(b);
    return b;
  };
  const filter = el("input", { type: "search", size: 36, value: jobText[c.name] || "", "aria-label": "Filter jobs",
                               placeholder: "Filter by job name, node, partition or what started it" });
  filter.addEventListener("input", () => { jobText[c.name] = filter.value; draw(); });
  draw();
  return el("div", {}, jobViews(c, "mine"), jobOpener(c),
    el("div", { class: "toolbar" },
      el("div", { class: "counts", role: "group", "aria-label": "Show jobs" },
        count("queued", "starting", " queued", n.queued),
        count("running", "up", " running", n.running),
        count("finished", "", " finished today", n.finished),
        n.failed ? count("failed", "error", " failed", n.failed) : null),
      filter,
      el("span", { class: "muted" }, `as of ${new Date(j.at * 1000).toLocaleTimeString()}`),
      action("Refresh", () => ctx.render())),
    body);
}

// Slurm's host lists, written out: "c[01-03,05],gpu1" -> c01 c02 c03 c05 gpu1, so a filter for one node finds the jobs on it
function expandHosts(list) {
  const out = [];
  const walk = (text) => {
    if (out.length > 500) return;
    const open = text.indexOf("["), close = text.indexOf("]", open);
    if (open < 0 || close < 0) { if (text) out.push(text); return; }
    const head = text.slice(0, open), tail = text.slice(close + 1);
    for (const part of text.slice(open + 1, close).split(",")) {
      const [a, b = a] = part.split("-"), from = Number(a), to = Number(b);
      if (!/^\d+$/.test(a) || !/^\d+$/.test(b) || to < from || to - from > 500) { walk(head + part + tail); continue; }
      for (let i = from; i <= to && out.length <= 500; i++) walk(head + String(i).padStart(a.length, "0") + tail);
    }
  };
  let depth = 0, start = 0;
  for (let i = 0; i <= list.length; i++) {          // commas inside [] belong to the range, the others separate hosts
    const ch = list[i];
    if (ch === "[") depth++;
    else if (ch === "]") depth--;
    else if (i === list.length || (ch === "," && depth === 0)) { walk(list.slice(start, i)); start = i + 1; }
  }
  return out;
}

// A filter over jobs: every word typed must be somewhere in the job's user, name, ID, partition, nodes (host lists
// written out) or what started it (an agent's template and token, an app's name)
// What a job asked sbatch for (queue_view.py's resource_term: the same rules, so the page and the agents' queue_status agree).
// `gpus=1:a100`, `--gres=gpu:a100:2`, `gpus-per-node=a100`: that option as passed to sbatch; `gpu:a100:2`, `2:a100` and
// `gpu` look at every GPU request; `constraint=a100` at the features. A type matches as part of the job's; a count is exact.
const REQUEST_KEYS = ["gpus", "gres", "gpus-per-socket", "gpus-per-task", "constraint"];
const REQUEST_OPTIONS = { gpus: "gpus", gres: "gres", "gpus-per-node": "gres", "gpus-per-socket": "gpus-per-socket",
                          "gpus-per-task": "gpus-per-task", constraint: "constraint", c: "constraint" };
const requestText = (requests) => Object.entries(requests || {}).map(([k, v]) => `${k}=${v}`).join(" ").toLowerCase();
function resourceTerm(term) {
  const text = term.trim().replace(/^-+/, "").trim();
  let option = null, value = text.toLowerCase();
  if (text.includes("=")) {
    const at = text.indexOf("=");
    option = text.slice(0, at).trim().toLowerCase();
    value = text.slice(at + 1).trim().toLowerCase();
    if (!(option in REQUEST_OPTIONS)) return (requests) => requestText(requests).includes(text.toLowerCase());
  }
  const keys = option ? [REQUEST_OPTIONS[option]] : REQUEST_KEYS;
  if (keys.length === 1 && keys[0] === "constraint") return (requests) => !!value && ((requests || {}).constraint || "").toLowerCase().includes(value);
  const tokens = value.split(":").filter(Boolean);
  let name = "gpu";
  if (tokens.length && (option === "gres" || tokens[0] === "gpu") && !/^\d+$/.test(tokens[0])) name = tokens.shift();
  const counts = tokens.filter((t) => /^\d+$/.test(t)), kinds = tokens.filter((t) => !/^\d+$/.test(t));
  const count = counts.length ? Number(counts[counts.length - 1]) : null, kind = kinds.join(":") || null;
  const gres = (requests) => keys.some((key) => key !== "constraint" && ((requests || {})[key] || "").split(",").some((item) => {
    if (!item) return false;
    const parts = item.toLowerCase().split(":");
    const itemCount = parts.length > 1 && /^\d+$/.test(parts[parts.length - 1]) ? Number(parts.pop()) : null;
    const itemKind = parts.slice(1).join(":") || null;
    return parts[0] === name && (!kind || (itemKind || "").includes(kind)) && (count === null || count === (itemCount === null ? 1 : itemCount));
  }));
  return (requests) => {
    if (gres(requests)) return true;
    if (option) return false;
    if (kind && count === null && ((requests || {}).constraint || "").toLowerCase().includes(kind)) return true;
    return requestText(requests).includes(text.toLowerCase());
  };
}
// a word with = in it, starting with - or gpu:, or like 2:a100, is a request; the other words look through the job's user, name, node,
// partition and what it asked for
const isRequestWord = (w) => w.includes("=") || w.startsWith("-") || /^(gpu:|\d+:[a-z])/.test(w);
function jobMatcher(text) {
  const words = text.toLowerCase().split(/\s+/).filter(Boolean);
  const asked = words.filter(isRequestWord).map(resourceTerm);
  const plain = words.filter((w) => !isRequestWord(w));
  return (job) => {
    if (!words.length) return true;
    if (!asked.every((check) => check(job.requests))) return false;
    const hay = [job.user, job.name, job.id, job.partition, job.node_list, job.node_list ? expandHosts(job.node_list).join(" ") : "",
      job.source && job.source.label, job.source && job.source.token, requestText(job.requests)].filter(Boolean).join(" ").toLowerCase();
    return plain.every((w) => hay.includes(w));
  };
}
const jobText = {};        // cluster -> the Jobs tab's filter text, kept while the page is open
const queueText = {};      // the same for the whole queue
const queueHpclib = {};    // cluster -> only the jobs hpclib started, in the whole queue

// Open any job's page by its ID, yours or someone else's (what Slurm shows about it; their script and output stay unread)
function jobOpener(c) {
  const input = el("input", { type: "text", size: 14, placeholder: "e.g. 164071", "aria-label": "Job ID", inputmode: "numeric" });
  const problem = el("span", { class: "error-text small", "aria-live": "polite" });
  const open = () => {
    const id = input.value.trim();
    if (!/^[0-9]+(_[0-9]+)?$/.test(id)) { problem.textContent = "A job ID is digits (an array task: 123_4)."; return; }
    problem.textContent = "";
    location.hash = href(c.name, "jobs", id);
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); open(); } });
  return el("div", { class: "job-open" }, el("label", {}, "Open job info ", input),
    el("button", { type: "button", class: "primary", onclick: open }, "Open"), problem,
    el("span", { class: "muted small" }, "any job on the cluster, someone else's too"));
}

// Your jobs, or the whole queue (everyone else's)
const jobViews = (c, active) => subTabs([[href(c.name, "jobs"), "My jobs", "mine"], [href(c.name, "jobs", "queue"), "Whole queue", "queue"]], active);

// Why other people's jobs wait, in general terms (the account and QOS limits are theirs, not yours)
const QUEUE_REASONS = {
  Priority: "behind higher-priority jobs", Resources: "waiting for free nodes", None: "about to start",
  Dependency: "waiting for another job", BeginTime: "scheduled for later", ReqNodeNotAvail: "requested node unavailable",
  JobHeldUser: "held by its owner", JobHeldAdmin: "held by an administrator", PartitionTimeLimit: "over the partition's time limit",
  AssocGrpGRES: "at an account GPU limit", AssocMaxJobsLimit: "at an account job limit",
  QOSMaxJobsPerUserLimit: "at a QOS job limit", QOSMaxCpuPerUserLimit: "at a QOS CPU limit",
};

// Everyone else's jobs on the cluster: how busy each partition is, who is using it, why jobs wait, and the jobs
// themselves (the first few hundred of each); your own are on My jobs. One squeue over your login.
async function queuePage(c) {
  const { api } = ctx;
  let q;
  try { q = await api(path(c.name, "queue")); } catch (err) { return ctx.errorBox(err); }
  const n = q.counts;
  const cpus = (v) => `${count(v)} CPU${v === 1 ? "" : "s"}`;
  const table = (heads, rows, empty) => rows.length ? el("table", { class: "jobs" },
    el("thead", {}, el("tr", {}, heads.map((h) => el("th", {}, h)))), el("tbody", {}, rows))
    : el("p", { class: "muted" }, empty);
  const reason = (r) => el("span", { title: r }, QUEUE_REASONS[r] || r);
  // the name opens the job's page (what Slurm shows about it; not their script or output)
  const jobName = (j) => el("td", {}, el("a", { class: "job-link", href: href(c.name, "jobs", j.id) }, j.name), el("div", { class: "muted small" }, j.id));
  // CPUs and memory, then what it asked sbatch for (the GPUs and constraint; hover for the option)
  const asked = (j) => el("span", {}, [`${j.cpus} CPU`, j.memory && j.memory !== "0" && j.memory].filter(Boolean).join(" · "),
    ...Object.entries(j.requests || {}).map(([k, v]) => el("div", { class: "small", title: `--${k}=${v}` }, `${k}=${v}`)));
  const source = (j) => (j.source.kind === "other" ? el("span", { class: "muted" }, "—")
    : chip(j.source.label, SOURCE_TONE[j.source.kind] || "down", j.source.token ? `token ${j.source.token}` : null));
  const partitions = table(["Partition", "CPUs in use", "", "Running", "Waiting"], q.partitions.map((p) => {
    const t = p.cpus, busy = t && t.total ? 100 * t.allocated / t.total : 0;
    return el("tr", {}, el("td", {}, p.name),
      el("td", {}, t ? `${count(t.allocated)} of ${count(t.total)} · ${count(t.idle)} idle` : el("span", { class: "muted" }, "unknown")),
      el("td", { class: "bar-cell" }, el("div", { class: "util", role: "img", "aria-label": `${Math.round(busy)}% of ${p.name}'s CPUs in use` },
        el("span", { style: `width: ${busy}%` }))),
      el("td", {}, p.running === null ? el("span", { class: "muted" }, "—") : `${p.running} job${p.running === 1 ? "" : "s"} · ${cpus(p.running_cpus)}`),
      el("td", {}, p.queued === null ? el("span", { class: "muted" }, "—") : `${p.queued} job${p.queued === 1 ? "" : "s"} · ${cpus(p.queued_cpus)}`));
  }), "Slurm listed no partitions.");
  // GPUs by partition and type, from sinfo's per-node GRES: free ones are on nodes that take jobs, "most on one node" is what one job can have
  const gpuRows = (q.gpus || []).flatMap((g) => g.types.map((t) => el("tr", {},
    el("td", {}, g.partition), el("td", {}, t.type),
    el("td", {}, `${count(t.used)} of ${count(t.total)} in use`),
    el("td", { class: "bar-cell" }, el("div", { class: "util", role: "img", "aria-label": `${t.type} GPUs in ${g.partition}: ${t.total ? Math.round(100 * t.used / t.total) : 0}% in use` },
      el("span", { style: `width: ${t.total ? 100 * t.used / t.total : 0}%` }))),
    el("td", {}, el("strong", {}, count(t.free)), " free"),
    el("td", {}, `${count(t.nodes_free)} of ${count(t.nodes)} node${t.nodes === 1 ? "" : "s"}`),
    el("td", {}, t.max_free ? `${t.max_free} on one node` : el("span", { class: "muted" }, "none")),
    el("td", {}, t.unavailable ? `${count(t.unavailable)} out of service` : el("span", { class: "muted" }, "—")))));
  const gpus = gpuRows.length ? el("div", {}, el("h3", {}, "GPUs"),
    el("p", { class: "muted small" }, "Free GPUs are on nodes that take jobs now; a node in several partitions is counted in each."),
    el("table", { class: "jobs gpus" }, el("thead", {}, el("tr", {}, ["Partition", "GPU", "In use", "", "Free", "Nodes with a free one", "Most free", "Out of service"].map((h) => el("th", {}, h)))),
      el("tbody", {}, gpuRows))) : null;
  const filter = el("input", { type: "search", size: 36, value: queueText[c.name] || "", "aria-label": "Filter jobs",
                               placeholder: "Filter by user, job name, node, partition or request (gpus=1:a100)",
                               title: "Words must all match. A request is written like the sbatch option: gpus=1:a100, --gres=gpu:a100:2, " +
                                 "gpus-per-node=a100, constraint=a100; gpu:a100 or 2:a100 looks at every GPU request" });
  const hpclibOnly = el("input", { type: "checkbox", checked: Boolean(queueHpclib[c.name]), "aria-label": "Only jobs started by hpclib" });
  const users = table(["User", "Running", "Waiting", "Started by hpclib"], q.users.map((u) => el("tr", {},
    el("td", {}, el("button", { type: "button", class: "link-button", title: `Show only ${u.user}'s jobs`,
      onclick: () => { filter.value = queueText[c.name] = u.user; redraw(); } }, u.user)),
    el("td", {}, u.running ? `${u.running} · ${cpus(u.running_cpus)}` : el("span", { class: "muted" }, "—")),
    el("td", {}, u.queued ? `${u.queued} · ${cpus(u.queued_cpus)}` : el("span", { class: "muted" }, "—")),
    el("td", {}, u.hpclib ? chip(`${u.hpclib} job${u.hpclib === 1 ? "" : "s"}`, "agent", "agents, apps and tunnels hpclib started") : el("span", { class: "muted" }, "—")))),
    "No one else has jobs.");
  const reasons = Object.keys(q.reasons).length ? el("p", { class: "chips" }, Object.entries(q.reasons).map(
    ([r, k]) => chip(`${k} ${QUEUE_REASONS[r] || r}`, "starting", r))) : el("p", { class: "muted" }, "Nothing is waiting.");
  const lists = el("div", {});
  const SHOW = 300;                                  // rows drawn per list at a time: the filter looks through every job, the table shows the first matches
  let showRunning = SHOW, showQueued = SHOW;
  const more = (shown, total, grow) => (total > shown ? el("p", {}, el("span", { class: "muted" }, `Showing ${shown} of ${total}. `),
    el("button", { type: "button", class: "link-button", onclick: () => { grow(); draw(); } }, `Show ${Math.min(SHOW, total - shown)} more`)) : null);
  const draw = () => {
    const words = filter.value.trim() || hpclibOnly.checked;
    const match = jobMatcher(filter.value);
    const keep = (j) => match(j) && (!hpclibOnly.checked || j.source.kind !== "other");
    const queued = q.queued.filter(keep), running = q.running.filter(keep);
    const cut = (total, list) => (total > list.length ? ` (the first ${list.length} of ${total})` : "");
    const matching = (total, list) => (words ? ` · ${list.length} match${list.length === 1 ? "" : "es"}` : "");
    lists.replaceChildren(...[      // more() is null when everything is shown: replaceChildren would write "null"
      el("h3", { id: "queue-running" }, `Running (${n.running})${cut(n.running, q.running)}${matching(n.running, running)}`),
      table(["Job", "User", "From", "Partition", "Node", "Time used", "Asked for"], running.slice(0, showRunning).map((j) => el("tr", {},
        jobName(j), el("td", {}, j.user), el("td", {}, source(j)), el("td", {}, j.partition),
        el("td", {}, j.node_list), el("td", {}, `${j.elapsed} of ${j.time_limit}`),
        el("td", {}, asked(j)))),
        words ? "No running job matches." : "Nothing is running."),
      more(showRunning, running.length, () => { showRunning += SHOW; }),
      el("h3", { id: "queue-waiting" }, `Waiting (${n.queued})${cut(n.queued, q.queued)}${matching(n.queued, queued)}`),
      table(["Job", "User", "From", "Partition", "Asked for", "Waiting because", "Submitted", "Expected start"], queued.slice(0, showQueued).map((j) => el("tr", {},
        jobName(j), el("td", {}, j.user), el("td", {}, source(j)), el("td", {}, j.partition),
        el("td", {}, asked(j)),
        el("td", {}, reason(j.reason)), el("td", {}, (j.submitted || "").replace("T", " ")),
        el("td", {}, j.start ? j.start.replace("T", " ") : el("span", { class: "muted" }, "not estimated")))),
        words ? "No waiting job matches." : "Nothing is waiting."),
      more(showQueued, queued.length, () => { showQueued += SHOW; })].filter(Boolean));
  };
  const redraw = () => { showRunning = showQueued = SHOW; draw(); };
  filter.addEventListener("input", () => { queueText[c.name] = filter.value; redraw(); });
  hpclibOnly.addEventListener("change", () => { queueHpclib[c.name] = hpclibOnly.checked; redraw(); });
  draw();
  // squeue failed (a busy or rate-limiting controller, say): say so, and don't show an empty queue as if it were the answer
  const problem = q.error ? el("div", { class: "error-box", role: "alert" },
    `squeue failed: ${q.error}. ` + (q.stale ? `Showing the last answer it gave, from ${new Date(q.at * 1000).toLocaleTimeString()}.`
      : "The jobs and users can't be shown, which is not the same as an empty queue; Refresh tries again.")) : null;
  if (q.error && !q.stale) {
    return el("div", {}, jobViews(c, "queue"), jobOpener(c), problem,
      el("div", { class: "toolbar" }, action("Refresh", () => ctx.render())),
      el("h3", {}, "Partitions"), partitions, gpus);
  }
  return el("div", {}, jobViews(c, "queue"), jobOpener(c), problem,
    el("div", { class: "toolbar" },
      el("div", { class: "counts" }, el("span", {}, el("strong", {}, String(n.users)), " other users"),
        el("span", {}, el("strong", {}, String(n.hpclib || 0)), " jobs from hpclib"),
        el("span", {}, el("strong", {}, String(n.running)), ` running · ${cpus(n.running_cpus)}`),
        el("span", {}, el("strong", {}, String(n.queued)), ` waiting · ${cpus(n.queued_cpus)}`)),
      el("span", { class: "muted" }, `as of ${new Date(q.at * 1000).toLocaleTimeString()}`),
      action("Refresh", () => ctx.render())),
    el("h3", {}, "Partitions"), el("p", { class: "muted small" }, "CPUs in use and waiting jobs count everyone's jobs, yours too."), partitions, gpus,
    el("h3", {}, "Who is using the cluster"), users,
    el("h3", {}, "Why jobs are waiting"), reasons,
    el("div", { class: "toolbar" }, filter,
      el("label", { class: "inline-check", title: "Agents' jobs, apps and agent tunnels: the jobs hpclib started" }, hpclibOnly, " Only hpclib jobs")),
    lists);
}

// One job: what was asked of Slurm, the batch script, and what the job wrote, wherever each still exists
async function jobPage(c, id) {
  const { api } = ctx;
  let d;
  try { d = await api(path(c.name, `jobs/${enc(id)}`)); } catch (err) {
    return el("div", {}, el("p", {}, el("a", { href: href(c.name, "jobs") }, "← Jobs")), ctx.errorBox(err));
  }
  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "");
  const appLink = d.source.kind === "agent-tunnel" ? el("a", { class: "button", href: href(c.name, "agents") }, "Agent tunnel")
    : d.source.app ? el("a", { class: "button", href: href(c.name, "apps", d.source.app) }, d.source.label) : null;
  const file = (heading, f, missing) => {
    if (!f) return missing ? [el("h3", {}, heading), el("p", { class: "muted" }, missing)] : [];
    const where = f.from === "file" ? `${f.path} (the file as it is now; it may have changed since the job)`
      : f.from === "slurm" && f.path === "(kept by Slurm)" ? "the copy Slurm kept" : f.from === "accounting" ? "the copy Slurm's accounting kept" : f.path;
    if (!f.found) return [el("h3", {}, heading), el("p", { class: "muted" },
      f.path && f.path !== "(kept by Slurm)" ? `${f.path} no longer exists, or isn't readable.` : (missing || "Not available."))];
    return [el("h3", {}, heading),
      el("p", { class: "muted small" }, `${where}${f.size ? ` · ${size(f.size / 1024)}` : ""}${f.mtime ? ` · written ${when(f.mtime)}` : ""}` +
        (f.truncated ? ` · showing the last ${size(d.limit / 1024)}` : "")),
      el("pre", { class: "log" }, f.text || "(empty)")];
  };
  const stateTone = d.state === "COMPLETED" ? "up" : ["RUNNING", "PENDING"].includes(d.state) ? "starting" : d.state ? "error" : "down";
  return el("div", { class: "job-page" },
    el("p", {}, el("a", { href: href(c.name, "jobs") }, "← Jobs")),
    el("div", { class: "toolbar" },
      el("h3", {}, d.name || `Job ${d.id}`, " ", el("span", { class: "muted small" }, d.id)),
      d.state ? chip(d.state.toLowerCase().replaceAll("_", " "), stateTone) : null,
      chip(d.source.label, SOURCE_TONE[d.source.kind] || "down"),
      appLink,
      el("span", { class: "muted" }, `as of ${new Date(d.at * 1000).toLocaleTimeString()}`),
      action("Refresh", () => ctx.render())),
    d.mine === false ? el("p", { class: "muted" }, `${d.owner ? `${d.owner}'s job` : "Someone else's job"}: this is what Slurm shows about it. ` +
      "Its script and output are not read.") : null,
    el("h3", {}, "Settings"),
    d.info.length ? el("table", { class: "compact job-info" }, el("tbody", {}, d.info.map(([k, v]) =>
      el("tr", {}, el("td", { class: "muted" }, k), el("td", {}, v)))))
      : el("p", { class: "muted" }, "Slurm no longer has this job's settings."),
    ...(d.mine === false ? [] : [
      ...file("Script", d.files.script, "Slurm kept no copy of the script, and the file it was submitted from is gone."),
      ...file("Output", d.files.stdout, "No output file was found."),
      ...file("Error output", d.files.stderr, null)]));
}
const jobFilter = {};      // cluster -> the kind of job its Jobs tab shows (null: all), while the page is open

// ---------------------------------------------------------------- Apps

// Which apps a cluster lists (its profile's app_view): pinned ones always, hidden ones not at all, the rest under
// "More apps", collapsed. A running app shows with the pinned ones wherever it is kept. Set per cluster from each
// row (Pin, Hide), the hidden list (Show, Pin), or Settings -> Apps on this cluster.
const APP_PLACES = [["pinned", "Pinned"], ["more", "Under More apps"], ["hidden", "Hidden"]];
function appPlace(c, id) {
  const v = c.app_view || {};
  return (v.pinned || []).includes(id) ? "pinned" : (v.hidden || []).includes(id) ? "hidden" : "more";
}
async function setAppPlace(c, id, place) {
  const v = c.app_view || {};
  const pinned = (v.pinned || []).filter((a) => a !== id), hidden = (v.hidden || []).filter((a) => a !== id);
  if (place === "pinned") pinned.push(id);
  if (place === "hidden") hidden.push(id);
  const out = await ctx.api(path(c.name, "app-view"), { method: "PUT", body: { pinned, hidden } });
  c.app_view = out.app_view;
}
const ACTIVE_STATES = ["up", "starting", "queued"];
const moreOpen = {};       // cluster -> "More apps" left open, while the page is open

async function appsTab(c) {
  const { api } = ctx;
  const apps = await appList();
  const shown = apps.filter((a) => appPlace(c, a.id) !== "hidden");
  const hidden = apps.filter((a) => appPlace(c, a.id) === "hidden");
  const move = (id, place) => async () => { await setAppPlace(c, id, place); ctx.render(); };
  const rows = await Promise.all(shown.map(async (a) => {
    const row = el("hpc-app-session", { app: a.id, "app-title": a.title, cluster: c.name, "by-app": "",
                                        "app-href": appHref(c.name, a.id) });
    try { row.data = await api(`apps/${a.id}/${enc(c.name)}`); } catch { return null; }
    const place = appPlace(c, a.id);
    row.viewControls = () => el("div", { class: "view-controls" },
      el("button", { type: "button", class: "link-button", onclick: move(a.id, place === "pinned" ? "more" : "pinned"),
                     title: place === "pinned" ? "Move it under More apps" : "Keep it in view on this cluster" },
        place === "pinned" ? "Unpin" : "Pin"),
      el("button", { type: "button", class: "link-button", onclick: move(a.id, "hidden"),
                     title: "Don't list it on this cluster (Hidden apps, at the end, brings it back)" }, "Hide"));
    return { row, place, active: ACTIVE_STATES.includes(row.data.state) };
  }));
  const ok = rows.filter(Boolean);
  const front = ok.filter((r) => r.place === "pinned" || r.active);
  const back = ok.filter((r) => !(r.place === "pinned" || r.active));
  const table = (items) => {
    const t = el("table", { class: "rows" }, el("thead", {}, el("tr", {}, ["App", "State", ""].map((h) => el("th", {}, h)))),
      items.map((r) => r.row));
    // an app's settings are on its own page here
    t.addEventListener("hpc-app-settings", (e) => { e.stopPropagation(); location.hash = appHref(c.name, e.detail.app); });
    return t;
  };
  const more = back.length ? el("details", { class: "more-apps", open: !!moreOpen[c.name] || !front.length && !(c.app_view || {}).pinned },
    el("summary", {}, `More apps (${back.length})`, el("span", { class: "muted small" }, " · Pin the ones you use here")),
    table(back)) : null;
  if (more) more.addEventListener("toggle", () => { moreOpen[c.name] = more.open; });
  const hiddenBox = hidden.length ? el("details", { class: "more-apps hidden-apps" },
    el("summary", {}, `Hidden apps (${hidden.length})`),
    el("table", { class: "compact" }, el("tbody", {}, hidden.map((a) => el("tr", {},
      el("td", {}, a.title), el("td", { class: "next" }, el("div", { class: "actions" },
        action("Show", move(a.id, "more")), action("Pin", move(a.id, "pinned"))))))))) : null;
  return el("div", {},
    front.length ? table(front) : el("p", { class: "muted" }, back.length
      ? "No app is pinned on this cluster: Pin the ones you use, and the rest stay under More apps."
      : "Every app is hidden on this cluster."),
    more, hiddenBox,
    el("p", { class: "muted small" }, "Each app's page has its settings, its tools and what it has installed here. Pin, " +
      "Hide and Settings → Apps on this cluster choose what this cluster lists."));
}

// Settings -> Apps on this cluster: every app's place in one list
async function appViewCard(c) {
  const apps = await appList();
  const card = el("section", { class: "card", id: "app-view" });
  const draw = () => card.replaceChildren(
    el("h3", {}, "Apps on this cluster ", el("span", { class: "muted" }, "· kept on this machine")),
    el("p", { class: "muted" }, "Pinned apps are always on the Apps tab and the Overview; the rest wait under More apps " +
      "(a running one shows anyway); hidden ones aren't listed."),
    el("table", { class: "compact" }, el("tbody", {}, apps.map((a) => {
      const select = el("select", { "aria-label": `${a.title} on ${c.name}` },
        APP_PLACES.map(([v, label]) => el("option", { value: v, selected: appPlace(c, a.id) === v }, label)));
      select.addEventListener("change", async () => {
        select.disabled = true;
        try { await setAppPlace(c, a.id, select.value); } catch (err) { card.append(ctx.errorBox(err)); }
        draw();
      });
      return el("tr", {}, el("td", {}, a.title), el("td", {}, select));
    }))));
  draw();
  return card;
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
  // a link to a file (#/c/NAME/apps/workspace?file=/path/to/file[&line=N]) for an app that can open one: the Open
  // button then goes to that file, and a running app is opened on it at once (not when coming back to this page)
  const query = new URLSearchParams(location.hash.split("?")[1] || "");
  const link = s.theme_link && query.get("file") ? { file: query.get("file"), line: Math.floor(Number(query.get("line"))) || 0 } : null;
  let linkNote = null;
  if (link) {
    row.link = link;
    linkNote = el("p", { class: "notice" }, `Opening ${link.file}${link.line ? ` at line ${link.line}` : ""} in ${a.title}. `,
      s.state === "up" ? "If this page stays, press Open." : `${a.title} isn't running on ${c.name}: start it here, then press Open.`);
    const back = (performance.getEntriesByType("navigation")[0] || {}).type === "back_forward";
    if (s.state === "up" && s.url && !back) {
      api(`apps/${id}/${enc(c.name)}/open`, { method: "POST" })
        .then((out) => location.replace(appUrl(out.url, out, link)))
        .catch((err) => linkNote.append(` ${err.message}`));
    }
  }
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
    id === "transfer" ? [syncEntry(c.name), el("h3", {}, "SMB transfers")] : null,
    linkNote,
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

let rescanRecent = false;       // "Rescan recent folders", kept while the console page is open
let folderMinutes = 5, totalMinutes = 30;     // the scan's time limits (a folder, the whole scan), kept the same way
// The roots left out of the next scan, as "CLUSTER\nPATH": for this round only (starting a scan clears them), kept
// while the console page is open.
const skipRoots = new Set();
const rootKey = (c, root) => `${c.name}\n${root.path}`;

// What the scan is doing, in words: where it is, what it skipped, and how much of its time is gone.
function scanStatus(st, scan) {
  const minutes = (seconds) => Math.max(0, Math.floor(seconds / 60));
  if (scan.state === "running") {
    if (scan.listing) return "Listing the folders…";
    const elapsed = Date.now() / 1000 - scan.started, allowed = (scan.deadline || scan.started) - scan.started;
    const some = scan.roots && st.roots && scan.roots.length < st.roots.length;     // not every root this round
    return `Scanning folder ${Math.min(scan.done + 1, scan.total)} of ${scan.total}` +
      (some ? ` in ${scan.roots.length} of ${st.roots.length} directories` : "") +
      (scan.skipped ? ` (${scan.skipped} scanned in the last 5 min skipped)` : "") +
      ` · ${minutes(elapsed)} of ${minutes(allowed)} min`;
  }
  const last = st.last_scan;
  if (!last) return st.scanned ? `Scanned ${ago(st.scanned)}` : "Not scanned yet";
  return `Last scan ended ${ago(last.finished)}: ${last.scanned} of ${last.folders} folders` +
    (last.skipped ? `, ${last.skipped} skipped as recent` : "") +
    (last.stopped ? `, stopped at its time limit: Scan now carries on with the rest` : "");
}

async function storageTab(c) {
  const { api } = ctx;
  let st;
  try { st = await api(path(c.name, "storage")); } catch (err) { return ctx.errorBox(err); }
  const scan = st.scan || {};
  const scanning = scan.state === "running";
  const roots = st.roots || [];
  const custom = st.custom_roots || [];
  const scanRoots = new Set(scan.roots || []);
  const chosen = () => roots.filter((r) => !skipRoots.has(rootKey(c, r))).map((r) => r.path);
  const scanWith = async (paths, retry = false) => {
    const folder = Math.round(folderMinutes * 60), total = Math.max(folder, Math.round(totalMinutes * 60));
    await api(path(c.name, "storage/scan"), { method: "POST", body: {
      rescan_recent: rescanRecent, roots: paths, folder_timeout: folder, total_timeout: total, ...(retry ? { retry_timed_out: true } : {}) } });
    for (const r of roots) skipRoots.delete(rootKey(c, r));      // a skip lasts one round
    ctx.render();
  };
  const button = action(scanning ? "Scanning…" : "Scan now", () => scanWith(chosen()),
    { primary: !st.scanned, disabled: scanning || !login(c) || !chosen().length });
  const syncButton = () => {
    const none = !chosen().length;
    button.disabled = scanning || !login(c) || none;
    if (none && !scanning) button.title = "Tick at least one folder to scan"; else button.removeAttribute("title");
  };
  syncButton();
  const toggle = el("label", { class: "inline-check", title: "A folder scanned in the last 5 minutes is skipped; tick this to scan it again" },
    el("input", { type: "checkbox", checked: rescanRecent, disabled: scanning,
                  onchange: (e) => { rescanRecent = e.target.checked; } }), " Rescan recent folders");
  // the time limits: a folder that doesn't finish in its limit is marked timed out; raise it, then retry those folders
  const minutes = (label, get, max, set) => el("label", { class: "inline-check" }, label,
    el("input", { type: "number", min: 1, max, step: 1, value: get(), disabled: scanning, class: "minutes", "aria-label": `${label} (minutes)`,
                  onchange: (e) => { const v = Number(e.target.value); if (v >= 1 && v <= max) set(v); else e.target.value = get(); } }), " min");
  const limits = el("span", { class: "scan-limits" },
    minutes("Per folder", () => folderMinutes, 240, (v) => { folderMinutes = v; if (totalMinutes < v) totalMinutes = v; }),
    minutes("Whole scan", () => totalMinutes, 1440, (v) => { totalMinutes = v; }));
  const timedOut = (st.dirs || []).reduce((n, d) => n + ((d.folder_counts || {}).timed_out || 0), 0);
  const retry = timedOut ? action(`Retry ${timedOut} timed-out folder${timedOut === 1 ? "" : "s"}`,
    () => scanWith(roots.map((r) => r.path), true),
    { disabled: scanning || !login(c), title: "Scan just the folders that ran out of time, with the limits set here: raise Per folder first" }) : null;
  // follow a running scan: the page fills in as folders finish
  if (scanning) setTimeout(() => { if (location.hash.endsWith("/storage")) ctx.render(); }, 3000);
  // add a directory to the scan: any full path on the cluster, or one from its home directory (~/...)
  const newRoot = el("input", { type: "text", placeholder: "/scratch/shared/data or ~/projects", size: 36,
                                "aria-label": "Folder to add to the scan", disabled: scanning });
  const add = action("Add folder", async () => {
    if (!newRoot.value.trim()) throw new Error("type the folder's full path, or ~/ for your home directory there");
    await api(path(c.name, "storage/roots"), { method: "PUT", body: { custom: [...custom, newRoot.value] } });
    ctx.render();
  }, { disabled: scanning });
  newRoot.addEventListener("keydown", (e) => { if (e.key === "Enter") add.button.click(); });
  // each directory's own controls: leave it out of this round, scan just it, take an added one away
  const controls = (r) => el("div", { class: "root-controls" },
    el("label", { class: "inline-check", title: "Scan this directory in the next scan; unticked, it is skipped this round and keeps its last results" },
      el("input", { type: "checkbox", checked: scanning ? scanRoots.has(r.path) : !skipRoots.has(rootKey(c, r)), disabled: scanning,
                    "aria-label": `Scan ${r.path}`,
                    onchange: (e) => { if (e.target.checked) skipRoots.delete(rootKey(c, r)); else skipRoots.add(rootKey(c, r)); syncButton(); } }),
      " Scan"),
    action("Scan only this", () => scanWith([r.path]), { disabled: scanning || !login(c) }),
    r.kind === "custom" ? action("Remove", async () => {
      await api(path(c.name, "storage/roots"), { method: "PUT", body: { custom: custom.filter((p) => p !== r.path) } });
      ctx.render();
    }, { disabled: scanning, title: "Stop scanning this directory and forget its results" }) : null,
    r.kind === "custom" ? el("span", { class: "muted small" }, "added by you") : null);
  const byRoot = new Map((st.dirs || []).map((d) => [d.root || d.path, d]));
  const cards = roots.map((r) => {
    const d = byRoot.get(r.path);
    if (!d) return el("section", { class: "card" }, el("h3", {}, r.path), controls(r), el("p", { class: "muted" }, "Not scanned yet."));
    if (d.missing) return el("section", { class: "card" }, el("h3", {}, d.path), controls(r), el("p", { class: "muted" }, "doesn't exist"));
    return storageCard(d, scan, controls(r));
  });
  return el("div", {},
    el("div", { class: "toolbar" },
      el("span", { class: "scan-status", "aria-live": "polite" }, scanning ? el("span", { class: "spinner", "aria-hidden": "true" }) : null,
        scanStatus(st, scan), scan.state === "failed"
          ? el("span", { class: "error-box" }, ` · the last scan failed: ${scan.error}`) : null),
      button, retry, toggle, limits,
      el("span", { class: "muted small" }, "du over your login at low priority; results are saved as each folder finishes")),
    el("div", { class: "add-root" }, el("label", {}, "Add a directory to scan ", newRoot), add),
    quotaCard(c, st),
    cards);
}

// One work directory's scan: its folders by size (du) or by file count (du --inodes: files, folders and links, what
// a file quota counts), with bars against the largest; the top 12, or all that were kept. Folders the running scan
// is on or has queued are marked, and the card says it is being scanned.
const storageSort = {};          // path -> "kb" | "files", kept while the console page is open
const storageAll = {};           // path -> whether every folder is shown
function storageCard(d, scan = {}, controls = null) {
  const has = (key) => d.children.some((ch) => ch[key] !== null && ch[key] !== undefined);
  let key = storageSort[d.path] || "kb";
  const running = scan.state === "running";
  const queued = new Set(scan.queued || []);
  const busy = (ch) => running && (ch.path === scan.current || queued.has(ch.path));
  const table = el("table", { class: "bars" });
  const sortBy = el("div", { class: "segmented", role: "radiogroup", "aria-label": `Sort ${d.path}` });
  const more = el("button", { type: "button", class: "link-button small" });
  // a number, or what stands in for it: scanning, queued, timed out, not scanned
  const value = (ch, k) => {
    const v = ch[k], shown = v === null || v === undefined ? "—" : k === "kb" ? size(v) : count(v);
    if (running && ch.path === scan.current) return el("span", { class: "scanning" }, v === null || v === undefined ? "scanning…" : shown);
    if (busy(ch) && (v === null || v === undefined)) return el("span", { class: "muted" }, "queued");
    const timedOut = (k === "kb" ? ch.state : ch.files_state) === "timeout" && (k === "kb" || ch.state !== "timeout");   // said once
    if (timedOut && (v === null || v === undefined)) return el("span", { class: "timed-out", title: "no answer within the time limit: raise Per folder and use Retry timed-out folders (or Scan now, which tries it again)" }, "timed out");
    return shown;
  };
  const draw = () => {
    const all = Boolean(storageAll[d.path]);
    const kids = d.children.slice().sort((a, b) => (b[key] || 0) - (a[key] || 0));
    // the folders being scanned always show, whatever their size so far
    const shown = all ? kids : kids.slice(0, 12).concat(kids.slice(12).filter((ch) => running && ch.path === scan.current));
    const top = shown[0] ? shown[0][key] || 0 : 0;
    const total = key === "kb" ? d.kb : d.files;
    table.replaceChildren(
      el("thead", {}, el("tr", {}, el("th", {}, "Folder"), el("th", {}), el("th", { class: "num" }, "Size"),
        el("th", { class: "num" }, "Files"), el("th", { class: "num" }, "Scanned"), el("th", { class: "num" }, ""))),
      el("tbody", {}, shown.map((ch) => el("tr", { class: running && ch.path === scan.current ? "scanning-row" : null },
        el("td", { class: "bar-name" }, ch.path.slice(d.path.replace(/\/$/, "").length + 1) || ch.path),
        el("td", { class: "bar-cell" }, el("div", { class: "bar", style: `width: ${top ? Math.max(1, 100 * (ch[key] || 0) / top) : 0}%` })),
        el("td", { class: `num${key === "kb" ? "" : " muted"}` }, value(ch, "kb")),
        el("td", { class: `num${key === "files" ? "" : " muted"}` }, value(ch, "files")),
        el("td", { class: "num muted", title: ch.scanned ? new Date(ch.scanned * 1000).toLocaleString() : "" }, ch.scanned ? ago(ch.scanned) : "never"),
        el("td", { class: "num muted" }, total && ch[key] ? `${Math.round(100 * ch[key] / total)}%` : "")))));
    sortBy.replaceChildren(...[["kb", "By size"], ["files", "By files"]].map(([k, label]) => el("button", {
      type: "button", role: "radio", "aria-checked": String(k === key), disabled: !has(k),
      onclick: () => { key = storageSort[d.path] = k; draw(); } }, label)));
    more.hidden = kids.length <= 12;
    more.textContent = all ? "Show the top 12" : `Show all ${kids.length}`;
  };
  more.addEventListener("click", () => { storageAll[d.path] = !storageAll[d.path]; draw(); });
  draw();
  const counts = d.folder_counts || { total: d.children.length, sized: d.children.length, timed_out: 0, unscanned: 0 };
  const waiting = d.children.filter(busy).length;
  const scanningHere = running && (scan.listing || waiting > 0);
  const where = scan.current && d.children.some((ch) => ch.path === scan.current) ? scan.current.split("/").pop() : null;
  return el("section", { class: `card storage${scanningHere ? " is-scanning" : ""}`, "aria-busy": String(scanningHere) },
    el("div", { class: "storage-head" }, el("h3", {}, d.path),
      el("div", { class: "storage-total" },
        el("strong", {}, `${d.partial && d.kb !== null ? "≥ " : ""}${size(d.kb)}`),
        el("span", { class: "muted" }, ` · ${d.partial && d.files !== null ? "≥ " : ""}${count(d.files)} files`),
        d.partial ? chip(`${counts.sized} of ${counts.total} folders sized${counts.timed_out ? `, ${counts.timed_out} timed out` : ""}`, "starting",
          "The totals add up the folders scanned so far; the rest are queued, timed out or not scanned yet.") : null,
        d.files_partial && !d.partial ? chip("file counts partial", "starting", "Some folders have a size but no file count yet.") : null),
      sortBy),
    controls,
    scanningHere ? el("div", { class: "scan-progress", role: "progressbar", "aria-label": `Scanning ${d.path}` },
      el("span", { class: "scan-bar" }),
      el("span", { class: "scan-text" }, scan.listing ? "Listing folders…" : `Scanning${where ? ` ${where}` : ""} · ${waiting} folder${waiting === 1 ? "" : "s"} left here`)) : null,
    d.oldest && d.scanned && d.scanned - d.oldest > 3600
      ? el("p", { class: "muted small" }, `Folder results range from ${ago(d.oldest)} to ${ago(d.scanned)}.`) : null,
    !has("files") && d.children.length && !running ? el("p", { class: "muted small" }, "Scan again for each folder's file count.") : null,
    table, more);
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

async function settings(c, local, sub) {
  // tunnel, server, port clearing, quota checks; then the MCP entry in the LLM clients; removal last
  const page = el("div", {}, await ctx.settingsCards(c), llmClientsCard(c), await appViewCard(c), removeCard(c));
  if (sub === "llm-clients") requestAnimationFrame(() => page.querySelector("#cluster-llm-clients")?.scrollIntoView());
  return page;
}

// The cluster's MCP entry in the LLM clients on this machine, always here (and in setup's panel right after it)
function llmClientsCard(c) {
  return el("section", { class: "card", id: "cluster-llm-clients" },
    el("h3", {}, "LLM clients ", el("span", { class: "muted" }, `· ${c.mcp_name || "its MCP entry"}`)),
    c.set_up ? connectClients(c.name, { heading: "", local: true })
      : el("p", { class: "muted" }, "Set the cluster up first (Agents → Setup…): setup writes the MCP entry the " +
        "clients run."));
}

// Remove this cluster (DELETE clusters/NAME): forget it on this machine. Its profile and tokens move to
// agents/.removed/ (so it can be put back); nothing on the cluster changes except, if asked, the agent token.
function removeCard(c) {
  const canRevoke = tunnelUp(c) && c.has_owner_token && c.set_up;
  const box = (label, checked, disabled = false, title = null) => {
    const input = el("input", { type: "checkbox", checked, disabled });
    return [input, el("label", { class: "check", title }, input, " ", label)];
  };
  const [revoke, revokeLabel] = box("Revoke its agent token on the cluster first", canRevoke, !canRevoke,
    canRevoke ? null : "needs the agent tunnel up and the owner token");
  const [stop, stopLabel] = box("Stop its agent tunnel and apps", true);
  const [logout, logoutLabel] = box("Log out", true);
  const [clients, clientsLabel] = box(`Take ${c.mcp_name || "its MCP entry"} out of your LLM clients`, true);
  const remove = action("Remove cluster…", async () => {
    if (!await confirmDialog(`Remove ${c.name}?`, `The console forgets ${c.name}: its login, ports, tokens and ` +
        "settings here. They are kept in a folder of removed profiles, from where they can be moved back. Files, " +
        "jobs and hpclib on the cluster stay as they are.", "Remove")) return null;
    const out = await ctx.api(path(c.name), { method: "DELETE", body: {
      revoke_token: revoke.checked, stop_tunnels: stop.checked, log_out: logout.checked, llm_clients: clients.checked } });
    const done = [out.token_revoked ? "its agent token was revoked" : null, out.tunnel_stopped ? "its tunnel was stopped" : null,
      Object.keys(out.llm_clients || {}).length ? `its MCP entry was taken out of ${Object.keys(out.llm_clients).join(", ")}` : null]
      .filter(Boolean);
    card.replaceChildren(...[
      el("h3", {}, `${c.name} was removed`),
      el("p", {}, done.length ? `${done.join("; ")}. ` : "", "Its profile and tokens are in ", el("code", {}, out.kept_in),
        " (move that folder back into ~/.config/hpclib/agents/ to restore it)."),
      out.warnings && out.warnings.length ? el("ul", { class: "error-box" }, out.warnings.map((w) => el("li", {}, w))) : null,
      el("div", { class: "actions" }, el("a", { class: "button primary", href: "#/c" }, "Back to Clusters")),
    ].filter(Boolean));
    return null;
  }, { primary: true });
  remove.classList.add("danger");
  const card = el("section", { class: "card danger-zone", id: "remove-cluster" },
    el("h3", {}, "Remove this cluster"),
    el("p", { class: "muted" }, "For a cluster you no longer use, or one to set up again from scratch. The profile " +
      "(login, ports, tokens, mcp.json) moves to ~/.config/hpclib/agents/.removed/; on the cluster, hpclib, your " +
      "files and the REST server's settings stay. On the command line: agent_remove NAME."),
    el("div", { class: "checks" }, revokeLabel, stopLabel, logoutLabel, clientsLabel),
    el("div", { class: "actions" }, remove));
  return card;
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

async function sshTab(c) {
  const siteManaged = c.preset?.ssh_keys === "site-managed";
  const local = el("section", { class: "card", id: "ssh-login-key" }, el("h3", {}, "This computer → cluster"));
  const draw = (info, message = "") => {
    const keys = (info.keys || []).filter((key) => key.private_available && !key.error && !key.options);
    const choice = el("select", { "aria-label": "Login identity" },
      keys.map((key) => el("option", { value: key.name }, `${key.name} · ${key.fingerprint}`)),
      el("option", { value: "" }, "Create a dedicated Ed25519 login key"));
    choice.value = info.configured?.name || (keys.some((key) => key.name === info.suggested_key)
      ? info.suggested_key : keys[0]?.name || "");
    if (!keys.some((key) => key.name === choice.value)) choice.value = "";
    const name = el("input", { value: info.suggested_key, "aria-label": "New login key name", size: 40 });
    const creation = el("div", {}, el("label", {}, "Key name ", name),
      el("p", { class: "muted small" }, "The new private key stays on this computer, without a passphrase. Existing key files are never replaced."));
    const install = action("Install login key", async () => {
      const create = choice.value === "";
      const result = await ctx.api(path(c.name, "ssh/login-key"), {
        method: "POST", body: { key: create ? name.value : choice.value, create },
      });
      draw(await ctx.api(path(c.name, "ssh/login-keys")), result.message + (result.restrictions_preserved
        ? " Existing authorization restrictions were preserved." : ""));
    }, { primary: true, disabled: !login(c) });
    const update = () => {
      creation.hidden = choice.value !== "";
      install.setAttribute("label", choice.value === "" ? "Create and install login key" : "Install login key");
    };
    choice.addEventListener("change", update);
    update();
    local.replaceChildren(...[el("h3", {}, "This computer → cluster"),
      el("p", {}, "Use your current login to add this computer's public key to the cluster. Future hpclib logins will use the selected identity. No agent tunnel is needed."),
      info.configured ? el("p", {}, `Installed: ${info.configured.name} · ${info.configured.fingerprint}`) : null,
      !login(c) ? el("p", { class: "muted" }, "Log in once with your existing credentials to enable setup.") : null,
      el("label", {}, "Identity on this computer ", choice), creation,
      el("p", { class: "muted small" }, "Only the public key is sent. Encrypted identities use your SSH agent or normal passphrase prompt. The cluster may still require MFA or use a separate key registration service."),
      el("div", { class: "actions" }, install, action("Reload identities", async () => draw(await ctx.api(path(c.name, "ssh/login-keys"))))),
      el("p", { role: "status", class: "saved" }, message),
      (info.errors || []).map((error) => el("p", { class: "muted" }, `${error.name}: ${error.error}`))
    ].flat(Infinity).filter((node) => node !== null && node !== undefined && node !== false));
  };
  if (siteManaged) {
    local.append(el("p", { class: "muted" }, "SSH keys are managed by this site. Use its key registration process; hpclib won't change its authorized keys."));
  } else {
    try { draw(await ctx.api(path(c.name, "ssh/login-keys"))); }
    catch (error) { local.append(ctx.errorBox(error)); }
  }
  const operation = el("div", {});
  const setup = action("Set up compute SSH", async () => {
    showOperation(await ctx.api(path(c.name, "ssh/setup"), { method: "POST", body: {} }));
  }, { disabled: siteManaged || !login(c) || c.operation?.state === "running" });
  const showOperation = (op) => {
    setup.disabled = siteManaged || !login(c) || op.state === "running";
    const title = op.kind === "ssh_setup" ? "Compute SSH setup" : op.title || op.kind;
    const state = el("p", { role: "status" }, `${title}: ${op.state}`);
    const log = el("hpc-log", { src: path(c.name, "operation?lines=200"), field: "log", every: 2 });
    log.addEventListener("update", ({ detail: current }) => {
      setup.disabled = siteManaged || !login(c) || current.state === "running";
      state.textContent = `${title}: ${current.state}`;
      if (current.state !== "running") log.setAttribute("every", "0");
    });
    operation.replaceChildren(state, log);
  };
  if (c.operation) showOperation(c.operation);
  const compute = el("section", { class: "card", id: "ssh-compute-key" }, el("h3", {}, "Login node → compute nodes"),
    el("p", {}, "Create or reuse a key in the cluster's shared home for connections between its nodes. Your active login is enough; agents can be set up afterward."),
    siteManaged ? el("p", { class: "muted" }, "Compute SSH keys are managed by this site.") : null,
    setup, operation);
  return el("div", {}, local, compute,
    el("section", { class: "card" }, el("h3", {}, "Keys stored on the cluster"), await ctx.sshKeys(c.name)));
}

async function agents(c, local, sub) {
  const page = AGENT_PAGES.some(([id]) => id === sub) ? sub : "";
  const tabs = subTabs(AGENT_PAGES.map(([id, label]) => [href(c.name, "agents", id), label, id]), page);
  if (page === "activity") return el("div", {}, tabs, await ctx.activity(c.name));
  // the agent profile's row: its tunnel (Start, Stop, Log), Update hpclib and setup_agents (Set up), as on the old
  // Clusters page; its panels open under it
  const row = el("hpc-cluster-row", { cluster: c.name, local });
  row.data = c;
  let actionsCard = tunnelActionsCard(c);
  let actionsState = JSON.stringify([c.tunnel?.state, c.tunnel?.owner_access, c.has_owner_token]);
  row.addEventListener("update", ({ detail: current }) => {
    const next = JSON.stringify([current.tunnel?.state, current.tunnel?.owner_access, current.has_owner_token]);
    if (next === actionsState) return;
    actionsState = next;
    const replacement = tunnelActionsCard(current);
    actionsCard.replaceWith(replacement);
    actionsCard = replacement;
  });
  return el("div", {}, tabs,
    el("p", { class: "muted" }, "Agents reach this cluster through its REST server, which runs in a job behind the " +
      "agent tunnel. Setup installs the job templates, the sandboxed server's config and the agent's token."),
    el("table", { class: "rows" },
      el("thead", {}, el("tr", {}, ["Cluster", "Tunnel", "Login", "Port", "Token", ""].map((h) => el("th", {}, h)))), row),
    mcpNameCard(c), actionsCard);
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
  if (!tunnelUp(c)) {
    const state = c.tunnel?.state || "down";
    const message = state === "starting" ? "The agent tunnel is connecting. Its actions will appear when it is ready."
      : state === "error" ? "The agent tunnel check failed: " + (c.tunnel?.error || "see the tunnel log for details.")
      : "Start the agent tunnel to see and change them.";
    box.append(head, intro, el("p", { class: "muted", role: "status" }, message));
    return box;
  }
  if (!c.has_owner_token || c.tunnel?.owner_access === "rejected") {
    box.append(head, intro, el("p", { class: "notice", role: "status" },
      "The tunnel is connected for agents. This console has no verified owner credential (missing, unusable or rejected). " +
      "Enabled actions can still run; recovering owner access allows their settings to be changed."),
      ownerRecovery(c, () => ctx.render()));
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
            a.availability ? chip(a.availability.available ? "available" : "not available",
                                  a.availability.available ? "up" : "starting") : null,
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
