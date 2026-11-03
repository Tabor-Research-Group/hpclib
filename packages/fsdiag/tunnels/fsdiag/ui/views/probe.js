// The Probe tab: what to measure, and where. It builds the spec fsruns.validate() accepts and POSTs it to /api/runs.
//
//   where     the login node, a compute node (any, or ones you name), or both
//   latency   per-call timings (stat, open, create, fsync, sqlite commit...) at the places listed
//   survey    how many entries the folders hold, and which listings are slow
//   quota     what the cluster's own quota commands say
//
// Naming nodes is for a node you suspect: pick it from the list, or from "where your jobs are" (the node a tunnel's
// REST server runs on), or type it.

import { el, toast } from "hpclib-ui/index.js";
import { api, fill } from "../client.js";

export function mountProbe(root, ctx) {
  const form = { where: "both", nodes: new Set(), latency: true, survey: true, quota: true, repeats: 15, threads: 4,
                 partition: "", account: "", time_limit: "", mem: "", paths: "", survey_roots: "" };
  form.partition = ctx.info.settings.partition;
  form.account = ctx.info.settings.account;
  form.time_limit = ctx.info.settings.time_limit;
  form.mem = ctx.info.settings.mem;
  form.survey_roots = ctx.info.settings.survey_roots.join("\n");
  form.paths = ctx.info.settings.paths.join("\n");
  let nodeFilter = "";
  const body = el("div", { class: "form" });
  root.append(
    el("h2", {}, "Why are my tunnels slow?"),
    el("p", { class: "lede" }, "Tunnel requests are slow when file system calls are slow: the REST server stat-s, opens and lists " +
      "files on a shared file system, and each call can wait on a busy server. This measures those calls where you choose, counts what " +
      "your folders hold, and reads what slow requests were waiting for. Results appear under Results; the probes are small and are " +
      "cleaned up after themselves."),
    body);

  const radio = (name, value, label, hint) => el("label", { class: "choice", title: hint || "" },
    el("input", { type: "radio", name, value, checked: form[name] === value, onchange: () => { form[name] = value; draw(); } }), label);
  const check = (name, label, hint) => el("label", { class: "choice", title: hint || "" },
    el("input", { type: "checkbox", checked: form[name], onchange: (e) => { form[name] = e.target.checked; draw(); } }), label);
  const number = (name, label, min, max) => el("label", {}, label,
    el("input", { type: "number", min, max, value: form[name], style: "width:6em", oninput: (e) => { form[name] = Number(e.target.value) || form[name]; } }));
  const text = (name, label, hint, width = "10em") => el("label", {}, label,
    el("input", { type: "text", value: form[name], placeholder: hint, style: `width:${width}`, oninput: (e) => { form[name] = e.target.value.trim(); } }));
  const lines = (name, label, hint, rows = 4) => el("label", {}, label,
    el("textarea", { class: "paths", rows, spellcheck: "false", placeholder: hint, oninput: (e) => { form[name] = e.target.value; } }, form[name]));

  function nodePicker() {
    const nodes = ctx.nodes;
    if (!nodes) return el("p", { class: "muted small" }, "Loading the node list…");
    if (!nodes.slurm) return el("p", { class: "muted small" }, "SLURM isn't available here, so only the login node can be probed.");
    const mine = nodes.mine || [];
    const wanted = nodeFilter.toLowerCase();
    const rows = nodes.nodes.filter((n) => !wanted || n.name.toLowerCase().includes(wanted) || n.partitions.join(",").includes(wanted));
    const toggle = (name) => { form.nodes.has(name) ? form.nodes.delete(name) : form.nodes.add(name); draw(); };
    return el("div", {},
      mine.length ? el("div", { class: "chips small" }, "Where your jobs run:", mine.map((j) =>
        el("button", { type: "button", class: "chip", title: `${j.name} (job ${j.job_id})`, onclick: () => (j.nodes || "").split(",").forEach((n) => n && form.nodes.add(n)) || draw() },
          `${j.name} on ${j.nodes || "?"}`))) : null,
      el("div", { class: "field-row" },
        el("label", {}, "Find a node", el("input", { type: "search", value: nodeFilter, placeholder: "name or partition",
          oninput: (e) => { nodeFilter = e.target.value; drawList(); } })),
        el("label", {}, "Or type names", el("input", { type: "text", placeholder: "node1, node2", style: "width:14em",
          onchange: (e) => { e.target.value.split(/[\s,]+/).filter(Boolean).forEach((n) => form.nodes.add(n)); e.target.value = ""; draw(); } }))),
      form.nodes.size ? el("div", { class: "chips" }, "Probing:", [...form.nodes].sort().map((n) =>
        el("button", { type: "button", class: "chip", title: "Remove", onclick: () => toggle(n) }, `${n} ×`))) : el("p", { class: "muted small" }, "No node chosen: the scheduler picks one."),
      el("div", { class: "node-list", id: "node-list" }, listRows(rows, toggle)));
  }
  const listRows = (rows, toggle) => rows.slice(0, 400).map((n) => el("label", { class: "node-row" + (/idle|mix|alloc/.test(n.state) ? "" : " unavailable") },
    el("input", { type: "checkbox", checked: form.nodes.has(n.name), onchange: () => toggle(n.name) }),
    el("span", { class: "grow" }, n.name), el("span", { class: "muted small" }, `${n.partitions.join(",")} · ${n.state} · ${n.cpus} cpu`)));
  function drawList() {
    const list = root.querySelector("#node-list");
    if (!list || !ctx.nodes || !ctx.nodes.slurm) return;
    const wanted = nodeFilter.toLowerCase();
    const toggle = (name) => { form.nodes.has(name) ? form.nodes.delete(name) : form.nodes.add(name); draw(); };
    fill(list, listRows(ctx.nodes.nodes.filter((n) => !wanted || n.name.toLowerCase().includes(wanted) || n.partitions.join(",").includes(wanted)), toggle));
  }

  async function run() {
    const split = (s) => s.split(/\n|:(?=\/|~)/).map((p) => p.trim()).filter(Boolean);
    if (!form.latency && !form.survey && !form.quota) throw new Error("Choose something to measure.");
    const spec = { where: form.where, latency: form.latency, quota: form.quota, repeats: form.repeats, threads: form.threads,
                   paths: split(form.paths), survey_roots: form.survey ? split(form.survey_roots) : [] };
    if (form.survey && !spec.survey_roots.length) spec.survey_roots = [ctx.info.home];
    if (form.where !== "login") {
      Object.assign(spec, { nodes: [...form.nodes], partition: form.partition, account: form.account });
      if (form.time_limit) spec.time_limit = form.time_limit;
      if (form.mem) spec.mem = form.mem;
    }
    const made = await api("/api/runs", { method: "POST", body: spec });
    toast(form.where === "login" ? "Started." : "Started. Compute probes wait in the queue like any job.", { tone: "ok" });
    ctx.open("results", made.id);
  }

  function draw() {
    const compute = form.where !== "login";
    fill(body,
      el("fieldset", {}, el("legend", {}, "Where"),
        el("div", { class: "choices" },
          radio("where", "login", "Login node", "the node you ssh to; where the tunnel's ssh forwards land"),
          radio("where", "compute", "Compute node", "a SLURM job: where the REST server runs"),
          radio("where", "both", "Both", "compare them: a file system that is fast on the login node and slow on compute points at the compute side")),
        compute ? el("div", {}, el("h3", {}, "Which compute nodes"), nodePicker(),
          el("div", { class: "field-row" }, text("partition", "Partition", "default"), text("account", "Account", "default"),
            text("time_limit", "Time limit", "00:20:00", "7em"), text("mem", "Memory", "1G", "5em"))) : null),
      el("fieldset", {}, el("legend", {}, "What to measure"),
        el("div", { class: "choices" },
          check("latency", "Call latency per place", "stat, open, create, fsync and a sqlite commit at each place"),
          check("survey", "How much is in my folders", "entries per directory and which listings are slow"),
          check("quota", "Quota and inodes", "the cluster's quota commands, and inode use from statvfs")),
        form.latency ? el("div", {},
          lines("paths", "Places to time (one per line; NAME=PATH to label one; empty: your home, scratch, work, /tmp and /dev/shm where they exist)",
            ctx.info.places.map((p) => `${p.label}=${p.path}`).join("\n"), 4),
          el("div", { class: "field-row" }, number("repeats", "Repeats", 3, 100), number("threads", "Concurrent threads", 1, 16))) : null,
        form.survey ? lines("survey_roots", "Folders to count (one per line; empty: your home directory). Large trees take up to a minute each.", ctx.info.home, 3) : null),
      el("div", { class: "chips" },
        el("button", { type: "button", class: "primary", onclick: (e) => {
          e.target.disabled = true;
          run().catch((err) => toast(err.message, { tone: "error" })).finally(() => { e.target.disabled = false; });
        } }, "Run diagnostics"),
        el("span", { class: "muted small" }, compute ? "A short SLURM job per target; the login probe runs at once." : "Runs on the login node now; takes up to a few minutes.")));
  }
  draw();
  return { nodesLoaded: draw };
}
