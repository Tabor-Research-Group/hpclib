// Runs from here: the jobs this Workspace submitted, newest first, under the file list. It shows the runs whose working
// directory is the one the file manager shows or below it (or all of them), each with its state, how long it has run,
// and what you do with a run: follow its output, open the script or the output, show its folder, cancel it. Only jobs
// submitted from this Workspace appear (their records are what the list is made from), so it is a record of your own
// work and not a browser of the queue: `squeue` is a terminal away.
//
//   GET /api/runs[?dir=] -> {runs[{job_id, name, state, reason, exit_code, elapsed, ended, submitted, workdir, script,
//                                  tasks (state counts) and task_total for an array or sweep,
//                                  output, resources, environment, git}], at}
//   POST /api/runs/ID/cancel
//
// The list asks the server every 15 s while something is running (a minute when nothing is, two minutes while the page
// is hidden) and at once when a job is submitted or the folder changes. The server asks Slurm once for all of them.

import { el, toast } from "hpclib-ui/index.js";
import { api, q, when, confirmAsk, dirname } from "../client.js";
import { ws } from "../shell.js";

const WORKING = ["RUNNING", "COMPLETING", "CONFIGURING"];

export function mountRuns(host) {
  let scope = "here", cwd = null, timer = null, last = null;
  const at = el("span", { class: "muted small" });
  const list = el("div", { class: "muted small" }, "…");
  const here = el("button", { type: "button", class: "small-button", "aria-pressed": "true", onclick: () => choose("here") }, "From here");
  const all = el("button", { type: "button", class: "small-button", "aria-pressed": "false", onclick: () => choose("all") }, "All");
  host.append(el("div", { class: "runs-head" }, el("strong", {}, "Runs"), el("span", { class: "scope" }, here, all), at,
    el("button", { type: "button", class: "small-button", onclick: () => refresh() }, "Refresh")), list);

  function choose(next) {
    scope = next;
    here.setAttribute("aria-pressed", String(scope === "here"));
    all.setAttribute("aria-pressed", String(scope === "all"));
    refresh();
  }

  /** "3/10 done" for an array or sweep: how many of its tasks have ended well, with the others named in the tooltip. */
  function tasksOf(run) {
    if (!run.task_total && !run.tasks) return null;
    const counts = run.tasks || {}, total = run.task_total || Object.values(counts).reduce((a, b) => a + b, 0);
    const done = counts.COMPLETED || 0, bad = Object.entries(counts).filter(([k]) => !["COMPLETED", "RUNNING", "PENDING", "COMPLETING"].includes(k)).reduce((a, [, n]) => a + n, 0);
    return el("span", { class: "muted small runs-tasks", title: Object.entries(counts).map(([k, n]) => `${n} ${k.toLowerCase()}`).join(", ") },
      `${done}/${total} done${bad ? `, ${bad} failed` : ""}`);
  }

  function row(run) {
    const word = (run.state || "unknown").toLowerCase();
    const status = el("hpc-state", { state: word, label: run.ended && run.exit_code && run.exit_code !== "0:0" ? `${word} (${run.exit_code})` : word,
      title: run.reason || "" });
    const act = (label, fn, cls = "") => el("button", { type: "button", class: `small-button ${cls}`.trim(), onclick: fn }, label);
    const cancel = async () => {
      if (!await confirmAsk(`Cancel job ${run.job_id} (${run.name})?`, "Cancel job")) return;
      try { await api(`/api/runs/${run.job_id}/cancel`, { method: "POST", body: {} }); refresh(); }
      catch (err) { toast(err.message, { tone: "error" }); }
    };
    return el("div", { class: "run", "data-job": run.job_id },
      el("div", { class: "run-line" },
        el("a", { href: "#", class: "run-name", title: run.workdir, onclick: (e) => { e.preventDefault(); if (run.workdir) ws.reveal(run.workdir); } }, run.name),
        status, el("span", { class: "muted small" }, `#${run.job_id}`), tasksOf(run),
        el("span", { class: "muted small", title: run.submitted ? new Date(run.submitted * 1000).toLocaleString() : "" },
          [run.elapsed, run.submitted ? when(run.submitted) : null].filter(Boolean).join(" · "))),
      el("div", { class: "run-actions" },
        run.output ? act("Follow", () => ws.dockOpen("follow", { job: run.job_id, output: run.output, label: run.name })) : null,
        run.script ? act("Script", () => ws.open(run.script)) : null,
        run.output ? act("Output", () => ws.open(run.output, {}, { opener: "text" })) : null,
        run.ended ? null : act("Cancel", cancel, "danger")));
  }

  function draw(out) {
    last = out;
    at.textContent = new Date(out.at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    list.replaceChildren(...(out.runs.length ? out.runs.map(row)
      : [el("span", { class: "muted small" }, scope === "here" ? "No runs from this folder or below it yet. Submit a script with Submit job…, or choose All." : "No runs yet.")]));
  }

  async function refresh() {
    clearTimeout(timer);
    if (scope === "here" && !cwd) { timer = setTimeout(() => { cwd = cwd || ws.cwd || ws.info.home; refresh(); }, 3000); return; }   // wait to learn the folder
    try { draw(await api(`/api/runs${scope === "here" && cwd ? `?${q({ dir: cwd })}` : ""}`)); }
    catch (err) { list.textContent = err.message; }
    const active = last && last.runs.some((r) => !r.ended);
    timer = setTimeout(refresh, document.hidden ? 120000 : active ? 15000 : 60000);
  }

  document.addEventListener("ws:job-submitted", () => refresh());
  document.addEventListener("ws:cwd", (e) => { if (e.detail.path !== cwd) { cwd = e.detail.path; if (scope === "here") refresh(); } });
  refresh();
  return { refresh };
}
