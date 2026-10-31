// Terminals in the dock: one <hpc-terminal> per tab, each a shell on the login node in the directory it was opened
// from. Registers the dock entry "terminal" and the action "Terminal here" (on a folder) or "Terminal in its
// directory" (on a file); the top bar's "Terminal here" button calls openTerminal(ws.cwd).
//
//   POST /api/term/ticket -> {ticket}     WS /terminal?ticket=&cwd=&cols=&rows=[&session=]
//   GET  /api/terms -> {terminals[{id, cwd, title, alive}]}        POST /api/term/close {id}
//
// A shell outlives its page: closing the tab (or the browser) detaches, and the shell keeps running on the login node
// with its recent output kept. This browser remembers its terminals' ids (per user and host, so two clusters don't
// mix), and restoreTerminals() attaches to the ones still running after a reload; a dropped connection is picked up
// again by <hpc-terminal> itself. Closing a terminal's tab hangs its shell up, as it always did.
//
// A ticket works once and for ten seconds, so the page asks for one each time a terminal connects and the token
// never goes in the address.

import { el, remember, toast } from "hpclib-ui/index.js";
import { api, q, basename, dirname } from "../client.js";
import { ws } from "../shell.js";

let count = 0;

const storeKey = () => `ws.terminals:${ws.info.user}@${ws.info.host}`;
const saved = () => remember.get(storeKey(), []);
const keep = (list) => remember.set(storeKey(), list.slice(0, 16));

/** Show a terminal in the dock: a new shell in `cwd` (with `command` typed into it), or (with `session`) the one that is already running. */
export function openTerminal(cwd = ws.cwd || ws.info.home, { session = null, label = null, command = null } = {}) {
  const term = el("hpc-terminal");
  term.connect = async ({ cols, rows, session: id }) => {
    if (id) {                                      // still there? the WebSocket can't say why it was refused
      const { terminals } = await api("/api/terms");
      if (!terminals.some((t) => t.id === id && t.alive)) throw Object.assign(new Error("that terminal has ended"), { gone: true });
    }
    const { ticket } = await api("/api/term/ticket", { method: "POST" });
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return new WebSocket(`${proto}//${location.host}/terminal?${q(id ? { ticket, session: id } : { ticket, cwd, cols, rows })}`);
  };
  const title = label || `${basename(cwd) || "/"} (${++count})`;
  const entry = { id: session, cwd, label: title };
  const tab = ws.dock.add({ label: title, title: cwd, path: cwd, persist: false, view: el("div", { class: "term" }, term),
    terminalId: session, onshow: () => { term.fit(); term.focus(); }, onresize: () => term.fit(),
    onclose: () => {
      const id = entry.id;
      term.dispose();
      if (id) {                                    // closing the tab is the person's way to end the shell
        keep(saved().filter((s) => s.id !== id));
        api("/api/term/close", { method: "POST", body: { id } }).catch(() => {});
      }
    } });
  term.addEventListener("terminal-session", (e) => {
    entry.id = tab.terminalId = e.detail.id;
    keep([...saved().filter((s) => s.id !== entry.id), entry]);
  });
  term.addEventListener("terminal-state", (e) => {
    ws.dock.update(tab.id, { suffix: e.detail.state === "reconnecting" ? " (reconnecting…)" : e.detail.state === "ended" ? " (ended)" : "" });
  });
  term.addEventListener("terminal-end", () => {
    if (entry.id) keep(saved().filter((s) => s.id !== entry.id));
  });
  ws.showDock(true);
  term.start({ session }).then(() => { if (command && !session) term.type(command + "\n"); })     // typed, so it shows in the shell's history
    .catch((err) => toast(err.message, { tone: "error" }));
  return tab;
}

/** Attach to the shells this browser had open that are still running (after a reload, or coming back later). */
export async function restoreTerminals() {
  const mine = saved();
  if (!mine.length) return 0;
  let running;
  try { running = new Set((await api("/api/terms")).terminals.filter((t) => t.alive).map((t) => t.id)); }
  catch { return 0; }
  keep(mine.filter((s) => running.has(s.id)));
  let n = 0;
  for (const s of mine) {
    if (!running.has(s.id)) continue;
    openTerminal(s.cwd, { session: s.id, label: s.label });
    n += 1;
  }
  return n;
}

ws.registerDock({ id: "terminal", title: "Terminal", create: (ctx = {}) => openTerminal(ctx.cwd || undefined, { label: ctx.label, command: ctx.command }) });
ws.registerAction({ id: "terminal", group: "run", label: "Terminal here", when: (e) => e.dir, run: (e) => openTerminal(e.path) });
ws.registerAction({ id: "terminal-dir", group: "run", label: "Terminal in its directory", when: (e) => !e.dir,
  run: (e) => openTerminal(dirname(e.path)) });
