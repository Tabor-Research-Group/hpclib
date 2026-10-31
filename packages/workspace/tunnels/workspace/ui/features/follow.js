// Following a file as it grows: a dock tab that shows the end of a file and keeps adding what is written to it. For the
// output of a job it also shows the job's state and stops by itself once the job has ended; for any other file (a log,
// a build's output, a file another program is writing) it follows until it is closed. Asked for by id:
// ws.dockOpen("follow", { output, job?, label? }) (the guided Submit form, Runs), and from the file menus' "Follow".
//
//   follow({ job?, output, label })       returns the dock tab (the dock entry "follow", off the "+" menu)
//   GET /api/fs/tail?path=&offset=&id=    the bytes the file has gained; `id` names the file, so a log that is rotated or
//                                         recreated starts again at its end, and the tab says so
//   GET /api/job?id=                      the job's state;   POST /api/runs/ID/cancel
//
// The file may not exist until the job starts: the tab says so and keeps asking. Output past 400 KB scrolls off the top
// of the view (the file itself is whole; "Open the file" shows it). A hidden page asks less often.

import { el, toast } from "hpclib-ui/index.js";
import { api, q, basename, confirmAsk } from "../client.js";
import { ws } from "../shell.js";

const KEEP = 400 * 1024;
const ACTIVE = ["PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "SUSPENDED", "REQUEUED", "UNKNOWN"];

export function follow({ job = null, output, label = "" }) {
  const state = el("hpc-state", { state: "pending", label: "waiting" });
  const note = el("span", { class: "muted small" }, "waiting for the file…");
  const pre = el("pre", { class: "log follow-log" });
  let text = "", offset = null, fileId = null, timer = null, closed = false, ended = false, stick = true;
  pre.addEventListener("scroll", () => { stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24; });
  const cancel = job ? el("button", { type: "button", class: "small-button danger", onclick: async () => {
    if (!await confirmAsk(`Cancel job ${job}?`, "Cancel job")) return;
    try { await api(`/api/runs/${job}/cancel`, { method: "POST", body: {} }); poll(); }
    catch (err) { toast(err.message, { tone: "error" }); }
  } }, "Cancel job") : null;
  const open = el("button", { type: "button", class: "small-button", onclick: () => ws.open(output, {}, { opener: "text" }) }, "Open the file");
  const view = el("div", { class: "follow" },
    el("div", { class: "bar" }, el("strong", {}, job ? `Job ${job}` : basename(output)), job ? state : null,
      label ? el("span", { class: "muted" }, label) : null, note, el("span", { class: "push-right" }, open, cancel)),
    pre);

  async function poll() {
    clearTimeout(timer);
    if (closed) return;
    let tail = { exists: false, data: "" };
    try {
      tail = await api(`/api/fs/tail?${q(offset === null ? { path: output } : { path: output, offset, ...(fileId ? { id: fileId } : {}) })}`);
      if (tail.exists) {
        if (tail.restarted && text) text = "";                         // the file was replaced: what we had isn't it
        offset = tail.offset;
        fileId = tail.id;
        if (tail.data || tail.restarted) {
          text = (text + tail.data).slice(-KEEP);
          pre.textContent = text;
          if (stick) pre.scrollTop = pre.scrollHeight;
        }
        note.textContent = tail.restarted ? `${basename(output)} (replaced: showing it from its end)` : basename(output);
      } else if (offset !== null) {
        offset = null; fileId = null;                                  // it went away: start over if it comes back
        note.textContent = `${basename(output)} is gone`;
      }
      if (job) {
        const info = await api(`/api/job?${q({ id: job })}`);
        const word = info.state.split(" ")[0].toLowerCase();
        state.setAttribute("state", word);
        state.setAttribute("label", info.ended && info.exit_code ? `${word} (exit ${info.exit_code})` : word);
        ended = info.ended || !ACTIVE.includes(info.state.split(" ")[0]);
        cancel.hidden = ended;
        if (ended && (!tail.exists || !tail.data)) {            // the last bytes are in (or there never was a file): stop asking
          if (!tail.exists) note.textContent = "the job ended without an output file";
          return;
        }
      }
    } catch (err) {
      note.textContent = err.message;
    }
    timer = setTimeout(poll, ended ? 1000 : document.hidden ? 15000 : 2000);
  }

  const tab = ws.dock.add({ label: job ? `Job ${job}` : basename(output), title: output, path: output, persist: false, view,
    onclose: () => { closed = true; clearTimeout(timer); } });
  ws.showDock(true);
  poll();
  return tab;
}

ws.registerDock({ id: "follow", title: "Follow", hidden: true, create: (ctx) => follow(ctx) });
ws.registerAction({ id: "follow", group: "view", label: "Follow as it grows", when: (e) => !e.dir,
  run: (e) => ws.dockOpen("follow", { output: e.path }) });
