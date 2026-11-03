// The Filesystem diagnostics page (fsdiag_server.py serves it): four tabs, one file each in views/.
//
//   index.html          the layout (tabs and four panels) and the import map
//   app.js              this file: starts the page, owns what the views share (ctx), and the tabs
//   client.js           the token, the API call, and how numbers and times are written
//   views/probe.js      what to measure and where (login node, compute nodes, named nodes), and start it
//   views/results.js    the runs, a run's findings, latencies per place and node, context, and the text report
//   views/directories.js  the directory survey and the quotas of a run
//   views/requests.js   what the REST server's slow requests were waiting for
//   hpclib-ui/          the package's copy of hpclib/ui: elements and styles shared with the console
//   bridge.js           the console's theme and stylesheet, from the address
//
// ctx.run is the run the Results and Directories tabs show; ctx.open("results", id) switches to it.

import { el, toast } from "hpclib-ui/index.js";
import { api, token } from "./client.js";
import { mountProbe } from "./views/probe.js";
import { mountResults } from "./views/results.js";
import { mountDirectories } from "./views/directories.js";
import { mountRequests } from "./views/requests.js";

const $ = (id) => document.getElementById(id);
const TABS = [["probe", "Probe"], ["results", "Results"], ["requests", "Slow requests"], ["directories", "Directories"]];

async function start() {
  if (!token) {
    document.body.replaceChildren(el("p", { class: "note-box" }, "Open Filesystem diagnostics from the console (its Open button " +
      "carries the token it needs)."));
    return;
  }
  const ctx = { info: null, nodes: null, runId: null, views: {}, listeners: [] };
  try { ctx.info = await api("/api/info"); }
  catch (err) {
    document.body.replaceChildren(el("p", { class: "note-box error-text" }, err.status === 401
      ? "This page's token is wrong or has changed (the server was restarted): open it from the console again."
      : err.message));
    return;
  }
  document.title = `Filesystem diagnostics · ${ctx.info.host.split(".")[0]}`;
  $("host").textContent = `${ctx.info.user}@${ctx.info.host}`;

  const tabs = $("tabs");
  const show = (id) => {
    for (const [t] of TABS) $(`panel-${t}`).hidden = t !== id;
    tabs.active = id;
    const view = ctx.views[id];
    if (view && view.shown) view.shown();
  };
  tabs.tabs = TABS.map(([id, label]) => ({ id, label, controls: `panel-${id}`, closable: false }));
  tabs.addEventListener("tab-select", (e) => show(e.detail.id));
  /** Switch tabs; with a run id the Results and Directories tabs show that run. */
  ctx.open = (id, runId) => {
    if (runId) { ctx.runId = runId; for (const f of ctx.listeners) f(runId); }
    show(id);
  };
  /** Views that show the chosen run register here. */
  ctx.onRun = (f) => ctx.listeners.push(f);
  ctx.tab = (id) => tabs.active === id;

  ctx.views.probe = mountProbe($("panel-probe"), ctx);
  ctx.views.results = mountResults($("panel-results"), ctx);
  ctx.views.requests = mountRequests($("panel-requests"), ctx);
  ctx.views.directories = mountDirectories($("panel-directories"), ctx);
  show("probe");
  // the sinfo node list is slow on a big cluster, so it loads after the page is up; the Probe tab fills in when it arrives
  api("/api/nodes").then((n) => { ctx.nodes = n; ctx.views.probe.nodesLoaded(); })
    .catch((err) => toast(`Could not list the nodes: ${err.message}`, { tone: "error" }));
}
start();
