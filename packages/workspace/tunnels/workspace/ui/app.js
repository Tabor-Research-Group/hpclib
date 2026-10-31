// The Workspace page (workspace_server.py serves it): the layout, and the features that plug into it.
//
//   index.html     the layout's elements (two <hpc-split>s, two <hpc-tabs>) and the import map
//   app.js         this file: starts the page, mounts the shell, loads the features
//   shell.js       the registry, the tabs and the dock (ws, also window.ws)
//   client.js      the token, the API call, paths and formatting
//   features/      files (the file manager), editor, submit (and classic, follow), usage, terminals, runs: each registers itself with ws
//   hpclib-ui/     the package's copy of hpclib/ui: elements and styles shared with the console
//   bridge.js      the console's theme and stylesheet, from the address
//
// CodeMirror and xterm.js load from the CDN the server names (info.cdn) the first time an editor or terminal needs
// them; without them the editor is a plain text box and a terminal says why it can't open.

import { el, configureLibs, toast } from "hpclib-ui/index.js";
import { api, token } from "./client.js";
import { ws } from "./shell.js";
import "./features/editor.js";
import "./features/classic.js";
import "./features/submit.js";
import "./features/usage.js";
import "./features/follow.js";
import { openTerminal, restoreTerminals } from "./features/terminals.js";
import { mountFiles } from "./features/files.js";
import { mountRuns } from "./features/runs.js";

const $ = (id) => document.getElementById(id);

async function start() {
  if (!token) {
    document.body.replaceChildren(el("p", { class: "note-box" }, "Open the Workspace from the console (its Open button " +
      "carries the token it needs)."));
    return;
  }
  try { ws.info = await api("/api/info"); }
  catch (err) {
    document.body.replaceChildren(el("p", { class: "note-box error-text" }, err.status === 401
      ? "This Workspace's token is wrong or has changed (it was restarted): open it from the console again."
      : err.message));
    return;
  }
  const info = ws.info;
  configureLibs({ cdn: info.cdn });
  document.title = `Workspace · ${info.host.split(".")[0]}`;
  $("host").textContent = `${info.user}@${info.host}`;
  ws.mount({ tabsBar: $("tabs"), views: $("views"), empty: $("empty"), dockBar: $("dock-tabs"), dockViews: $("dock-views"),
             dockPane: $("dock"), dockSplit: $("dock-split") });
  $("top-actions").append(el("button", { type: "button", class: "primary", id: "terminal-here",
    onclick: () => openTerminal(ws.cwd || info.home) }, "Terminal here"));
  $("dock-new").addEventListener("click", () => ws.docks.find((d) => !d.hidden).create({ cwd: ws.cwd }));
  $("dock-hide").addEventListener("click", () => ws.showDock(false));
  const files = mountFiles($("files-slot"));
  mountRuns($("runs-slot"));
  await files.ready;
  ws.restoreTabs().catch((err) => toast(err.message, { tone: "error" }))
    .then(() => files.link && ws.open(files.link.path, {}, { line: files.link.line }))
    .catch((err) => toast(err.message, { tone: "error" }));
  restoreTerminals().catch((err) => toast(err.message, { tone: "error" }));
}
start();
