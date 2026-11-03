// The Results tab: the runs on the left; on the right, the chosen run's findings first (what is wrong and what to do),
// then the per-call timings of each place side by side for each target, and what else was going on at the time.
//
// Findings come from fsfindings.py, not from this page, so the text report (Download report) says the same.

import { el, toast } from "hpclib-ui/index.js";
import { api, bytes, download, fill, logPercent, ms, tone, when } from "../client.js";
import { fetchRun, findingList, runState, stateChip, targetName } from "./shared.js";

export function mountResults(root, ctx) {
  const list = el("div", { id: "run-list" });
  const detail = el("div", { id: "run-detail" });
  root.append(el("div", { class: "split" }, el("div", {}, el("h2", {}, "Runs"), list), detail));
  let runs = [], timer = null, busy = false;

  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      runs = (await api("/api/runs")).runs;
      if (!ctx.runId && runs.length) ctx.runId = runs[0].id;
      drawList();
      if (ctx.runId) await drawDetail();
      else fill(detail, el("p", { class: "muted" }, "No runs yet: start one on the Probe tab."));
    } catch (err) { fill(detail, el("p", { class: "error-text" }, err.message)); }
    finally { busy = false; }
    clearTimeout(timer);
    // follow a run that is still going (the tab being hidden costs one cheap request every few seconds); stop when none is
    if (runs.some((r) => ["pending", "running"].includes(runState(r)))) timer = setTimeout(refresh, 3000);
  }
  ctx.onRun(() => refresh());

  function drawList() {
    fill(list, runs.length ? runs.map((r) => {
      const state = runState(r);
      return el("button", { type: "button", class: "run-item", "aria-current": String(r.id === ctx.runId), onclick: () => { ctx.runId = r.id; ctx.open("results", r.id); } },
        el("div", { class: "line" }, el("strong", {}, when(r.created)), stateChip(state)),
        el("div", { class: "muted small" }, r.targets.map((t) => targetName(t.key)).join(", ")));
    }) : el("p", { class: "muted small" }, "Nothing yet."));
  }

  async function drawDetail() {
    const d = await fetchRun(ctx.runId, true);
    const { run, results, findings } = d;
    const state = runState(run);
    const nodes = el("div", { class: "chips" }, run.targets.map((t) => el("span", { class: `chip ${t.state === "done" ? "good" : t.state === "failed" ? "bad" : ""}`,
      title: t.detail || "" }, `${targetName(t.key)}${t.node_used && t.node_used !== t.node ? ` (${t.node_used})` : ""}: ${t.state}${t.detail && t.state !== "done" ? ` – ${t.detail}` : ""}`)));
    fill(detail,
      el("div", { class: "chips" }, el("h2", { style: "margin-right:auto" }, `Run of ${when(run.created)}`), stateChip(state),
        el("button", { type: "button", onclick: () => download(`/api/report?id=${encodeURIComponent(run.id)}&requests=1`, `fsdiag-${run.id}.txt`).catch((e) => toast(e.message, { tone: "error" })) }, "Download report"),
        ["pending", "running"].includes(state) ? el("button", { type: "button", onclick: async () => { await api("/api/run/cancel", { method: "POST", body: { id: run.id } }); refresh(); } }, "Cancel") : null,
        el("button", { type: "button", onclick: async () => { await api("/api/run/delete", { method: "POST", body: { id: run.id } }); ctx.runId = null; refresh(); } }, "Delete")),
      nodes,
      el("h3", {}, "What it found"), findingList(findings),
      results.length ? [el("h3", {}, "Call latency"), el("p", { class: "muted small" }, "Median per call; the title of a cell has the 95th percentile and the slowest. Red is over 100 ms (fsync and a sqlite commit: 250), amber over 20 ms (50)."),
        places(results), el("h3", {}, "What else was going on"), context(results)] : (!["pending", "running"].includes(state) ? el("p", { class: "muted" }, "No measurements were collected.") : el("p", { class: "muted" }, "Waiting for the probes…")));
  }

  // one table per place: operations down, a column per target
  function places(results) {
    const labels = [];
    for (const r of results) for (const p of r.paths || []) if (!labels.includes(p.label)) labels.push(p.label);
    const describe = Object.fromEntries(ctx.info.ops.map((o) => [o.name, o.info]));
    return labels.map((label) => {
      const per = results.map((r) => ({ r, p: (r.paths || []).find((p) => p.label === label) }));
      const first = per.find((x) => x.p);
      const names = Object.keys((first && first.p.ops) || {});
      const any = per.filter((x) => x.p && x.p.ops && Object.keys(x.p.ops).length);
      if (!any.length) return null;
      return el("div", { class: "panel-block" },
        el("div", { class: "chips" }, el("strong", {}, label), el("code", {}, first.p.real || first.p.path),
          first.p.mount ? el("span", { class: "chip" }, `${first.p.mount.fstype}${first.p.mount.network ? " (network)" : ""}`) : null,
          first.p.read_only ? el("span", { class: "chip warn" }, "read-only: write calls skipped") : null),
        el("table", { class: "ops matrix" }, el("thead", {}, el("tr", {}, el("th", {}, "Call"), per.map((x) => el("th", { class: "right" }, targetName(x.r.target))), el("th", {}, ""))),
          el("tbody", {}, names.map((op) => {
            const vals = per.map((x) => x.p && x.p.ops && x.p.ops[op]);
            const worst = Math.max(...vals.map((v) => (v ? v.median : 0)));
            return el("tr", { title: describe[op] || "" }, el("td", {}, op),
              vals.map((v) => el("td", { class: `right ${v ? tone(op, v.median) : ""}`, title: v ? `p95 ${ms(v.p95)}, slowest ${ms(v.max)}, ${v.n} calls` : "" }, v ? ms(v.median) : "–")),
              el("td", { class: `bar-cell` }, el("div", { class: `bar ${tone(op, worst)}` }, el("span", { style: `width:${logPercent(worst)}%` }))));
          }))),
        concurrency(per));
    });
  }
  const concurrency = (per) => {
    const rows = per.filter((x) => x.p && x.p.concurrent && x.p.concurrent.operations);
    return rows.length ? el("p", { class: "muted small" }, "With several threads at once: ", rows.map((x) => `${targetName(x.r.target)} ${Math.round(x.p.concurrent.per_second).toLocaleString()} calls/s`).join(", "),
      rows.some((x) => x.p.concurrent.errors && x.p.concurrent.errors.length) ? " (some failed)" : "") : null;
  };

  function context(results) {
    return el("div", { class: "cards" }, results.map((r) => {
      const c = r.context || {};
      const psi = c.io_pressure && c.io_pressure.some ? c.io_pressure.some.avg10 : null;
      return el("div", { class: "card" }, el("strong", {}, `${targetName(r.target)}${r.node && r.node !== r.target ? ` · ${r.node}` : ""}`),
        el("div", { class: "small" }, `${c.cpus || "?"} cpus, load ${(c.loadavg || []).join(" ") || "?"}`),
        el("div", { class: "small" }, psi === null ? "I/O pressure: not reported here" : `I/O pressure: ${psi}% of time stalled (10 s)`),
        el("div", { class: "small" }, c.iowait_percent_during_probe === undefined || c.iowait_percent_during_probe === null ? "" : `iowait during the probe: ${c.iowait_percent_during_probe}%`),
        el("div", { class: `small ${c.d_state_processes > 3 ? "bad-text" : ""}`, title: (c.d_state_names || []).join(", ") }, `${c.d_state_processes || 0} processes blocked on I/O`),
        c.mem_available_kb ? el("div", { class: "small" }, `${bytes(c.mem_available_kb * 1024)} memory available`) : null);
    }));
  }

  return { shown: refresh };
}
