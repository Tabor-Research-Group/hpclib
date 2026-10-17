// Activity's read-only job tracker and request log. The existing REST API owns
// submissions and scheduler status; this page never submits or modifies a job.
import { el, action } from "./components.js";

const WAITING = new Set(["PENDING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED", "RESV_DEL_HOLD"]);
const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL",
  "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED", "SPECIAL_EXIT"]);
const finished = (job) => job.terminal === true || TERMINAL.has(job.state);
const failed = (job) => finished(job) && job.state !== "COMPLETED";
const matchesState = (job, value) => value === "all" || (value === "waiting" ? WAITING.has(job.state)
  : value === "finished" ? finished(job) : value === "failed" ? failed(job) : job.state === value);
const timestamp = (value) => value ? new Date(value * 1000).toLocaleString() : "Unknown";
const stateBadge = (job) => el("hpc-state", { label: job.state || "UNKNOWN",
  tone: failed(job) ? "error" : job.state === "COMPLETED" || job.state === "RUNNING" ? "up"
    : WAITING.has(job.state) ? "starting" : "down" });
const select = (label, values) => el("select", { "aria-label": label }, values.map(([value, text]) => el("option", { value }, text)));
const field = (label, control) => el("label", { class: "activity-filter" }, el("span", {}, label), control);
const message = (text) => el("p", { class: "muted" }, text);

export async function activityPage({ api, filesHash, setFollowing }) {
  const { clusters } = await api("clusters");
  const pick = el("hpc-cluster-picker", { live: true, "aria-label": "Activity cluster" });
  pick.clusters = clusters;
  if (!pick.options.length) return [el("h2", {}, "Activity"), message("Start a cluster tunnel with an owner token to check jobs and requests.")];

  const page = el("section", { class: "activity-page" });
  const token = el("input", { "aria-label": "Agent token", placeholder: "All agents", size: 16 });
  const search = el("input", { "aria-label": "Find jobs", placeholder: "Job ID, label or template", size: 24 });
  const phase = select("Job state", [["all", "All states"], ["waiting", "Waiting"], ["RUNNING", "Running"],
    ["finished", "Finished"], ["COMPLETED", "Completed"], ["failed", "Failed or stopped"], ["UNKNOWN", "Unknown"]]);
  const period = select("Submitted within", [["all", "All loaded history"], ["1", "Last 24 hours"], ["7", "Last 7 days"], ["30", "Last 30 days"]]);
  const limit = select("History size", [["100", "100 jobs"], ["250", "250 jobs"], ["500", "500 jobs"], ["1000", "1000 jobs"]]);
  const errorsOnly = el("input", { type: "checkbox", "aria-label": "Request errors only" });
  const follow = el("input", { type: "checkbox", "aria-label": "Follow activity" });
  const notices = el("div", { role: "status", "aria-live": "polite" });
  const checked = el("span", { class: "muted small" });
  const summary = el("div", { class: "activity-summary", "aria-label": "Job counts" });
  const jobBody = el("tbody");
  const jobCount = el("p", { class: "muted small", role: "status" });
  const detail = el("div");
  const auditBody = el("tbody");
  const auditCount = el("p", { class: "muted small", role: "status" });
  const jobPane = el("section", { class: "activity-pane", "aria-label": "Submitted jobs" },
    message("Jobs submitted through the agent API, including their finished outcomes. Jobs managed only through job-queue are tracked separately."),
    el("div", { class: "toolbar" }, field("Find", search), field("State", phase), field("Submitted", period), field("Load latest", limit)),
    summary, jobCount,
    el("div", { class: "activity-table" }, el("table", { class: "activity-jobs" },
      el("thead", {}, el("tr", {}, ["Job", "Agent", "Submitted", "State", ""].map((title) => el("th", {}, title)))), jobBody)), detail);
  const auditPane = el("section", { class: "activity-pane", hidden: true, "aria-label": "Request log" },
    el("div", { class: "toolbar" }, el("label", {}, errorsOnly, " Errors only")), auditCount,
    el("div", { class: "activity-table" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Time", "Agent", "Request", "Status", "Detail"].map((title) => el("th", {}, title)))), auditBody)));
  let jobs = [], entries = [], latest = null, selected = null, lastDetail = null;
  let generation = 0, detailGeneration = 0, loading = false, jobError = "", auditError = "";
  let taskPhase = "all", outputBox = el("div");
  const detailError = el("p", { class: "error-box", role: "status" });

  const drawNotices = () => notices.replaceChildren(...[jobError && `Jobs: ${jobError}`, auditError && `Requests: ${auditError}`]
    .filter(Boolean).map((text) => el("p", { class: "error-box" }, text)));
  const visibleJobs = () => {
    const words = search.value.toLowerCase().trim();
    const since = period.value === "all" ? 0 : Date.now() / 1000 - Number(period.value) * 86400;
    return jobs.filter((j) => (!token.value.trim() || j.token === token.value.trim()) && (!since || j.submitted >= since)
      && (!words || [j.job_id, j.label, j.template].filter(Boolean).join(" ").toLowerCase().includes(words)));
  };
  const drawJobs = () => {
    const filtered = visibleJobs();
    summary.replaceChildren(...[["Submitted", filtered.length, "all"], ["Waiting", filtered.filter((j) => WAITING.has(j.state)).length, "waiting"],
      ["Running", filtered.filter((j) => j.state === "RUNNING").length, "RUNNING"],
      ["Completed", filtered.filter((j) => j.state === "COMPLETED").length, "COMPLETED"],
      ["Failed / stopped", filtered.filter(failed).length, "failed"]].map(([label, count, value]) =>
      el("button", { type: "button", class: "activity-count", onclick: () => { phase.value = value; drawJobs(); } },
        el("strong", {}, count), el("span", {}, label))));
    const shown = filtered.filter((j) => matchesState(j, phase.value));
    jobCount.textContent = `${shown.length} shown · ${jobs.length} loaded, up to ${limit.value} newest jobs. Filters apply to this loaded history.${jobError ? " Data may be stale." : ""}`;
    jobBody.replaceChildren(...(shown.length ? shown.map((j) => el("tr", { "data-job-id": j.job_id },
      el("td", {}, el("strong", {}, j.job_id), j.label ? el("div", {}, j.label) : null,
        el("div", { class: "muted small" }, `${j.template}${j.array_size ? ` · ${j.array_size} tasks` : ""}`)),
      el("td", {}, j.token || ""), el("td", { class: "small" }, timestamp(j.submitted)),
      el("td", {}, stateBadge(j)), el("td", {}, action("Details", () => openJob(j)))))
      : [el("tr", {}, el("td", { colspan: 5, class: "muted" }, jobError ? "Job history unavailable; retry Check now." : "No jobs match these filters."))]));
  };
  const drawAudit = () => {
    const shown = entries.filter((e) => (!token.value.trim() || e.token === token.value.trim())
      && (!errorsOnly.checked || (e.status || 0) >= 400));
    auditCount.textContent = `${shown.length} requests${auditError ? " · data may be stale" : ""}`;
    auditBody.replaceChildren(...shown.slice().reverse().map((e) => el("tr", {},
      el("td", { class: "small" }, timestamp(e.time)), el("td", {}, e.token || ""),
      el("td", {}, `${e.verb} ${e.path}`), el("td", {}, e.status >= 400 ? el("hpc-state", { tone: "error", label: e.status }) : e.status),
      el("td", {}, e.detail ? JSON.stringify(e.detail) : ""))));
  };
  const outputActions = (clusterName, path, box) => el("div", { class: "actions" },
    action("Preview output", async () => {
      const out = await api(`${cluster(clusterName)}/rest/files/tail?` + new URLSearchParams({ path, lines: "200", max_bytes: "65536" }));
      box.replaceChildren(el("hpc-panel", { heading: "Output preview", dismissible: true, class: "log-panel" },
        message(out.binary ? "Binary output; open it in Files." : `${out.truncated ? "Truncated preview. " : ""}Last 200 lines, up to 64 KiB.`),
        out.binary ? null : el("pre", { class: "log" }, out.lines.join("\n"))));
    }), el("a", { class: "button", href: filesHash(clusterName, path) }, "Open in Files"));
  const closeDetail = () => { selected = null; lastDetail = null; ++detailGeneration; detail.replaceChildren(); };
  const drawDetail = (job, clusterName) => {
    const log = outputBox;
    const panel = el("hpc-panel", { heading: `Job ${job.job_id}${job.label ? ` · ${job.label}` : ""}`, dismissible: true });
    panel.addEventListener("dismiss", (event) => { if (event.target === panel) closeDetail(); });
    const stats = [["State", job.state], ["Agent", job.token], ["Template", job.template], ["Submitted", timestamp(job.submitted)],
      ["Elapsed", job.elapsed], ["Exit code", job.exit_code], ["Started", job.start], ["Finished", job.end],
      ["Reason", job.reason], ["Nodes", job.nodes], ["Working directory", job.workdir]];
    panel.append(el("dl", { class: "activity-details" }, stats.filter(([, value]) => value !== undefined && value !== "")
      .map(([label, value]) => [el("dt", {}, label), el("dd", {}, value)])));
    if (job.output) panel.append(message(`Output: ${job.output}`));
    if (job.output && !job.array_size) panel.append(outputActions(clusterName, job.output, log));
    if (job.array_size) {
      const counts = job.task_counts || {};
      panel.append(message(`${job.array_size} tasks · concurrency ${job.throttle || "unspecified"}${Object.keys(counts).length ? " · " + Object.entries(counts).map(([state, n]) => `${state}: ${n}`).join(", ") : ""}`));
      const taskFilter = select("Task state", [["all", "All tasks"], ["waiting", "Waiting"], ["RUNNING", "Running"],
        ["finished", "Finished"], ["failed", "Failed or stopped"]]);
      const tasks = el("tbody"), count = el("p", { class: "muted small" });
      taskFilter.value = taskPhase;
      const drawTasks = () => {
        const shown = (job.tasks || []).filter((t) => matchesState(t, taskFilter.value));
        count.textContent = `${Math.min(shown.length, 500)} of ${shown.length} matching tasks shown`;
        tasks.replaceChildren(...shown.slice(0, 500).map((t) => el("tr", {}, el("td", {}, t.index),
          el("td", {}, stateBadge(t)), el("td", {}, t.exit_code || ""),
          el("td", {}, t.output ? outputActions(clusterName, t.output, log) : null))));
      };
      taskFilter.addEventListener("change", () => { taskPhase = taskFilter.value; drawTasks(); }); drawTasks();
      panel.append(field("Task state", taskFilter), count,
        el("div", { class: "activity-table" }, el("table", {}, el("thead", {}, el("tr", {},
          ["Task", "State", "Exit code", "Output"].map((title) => el("th", {}, title)))), tasks)));
    }
    panel.append(log, el("details", {}, el("summary", {}, "Parameters and resources"),
      el("pre", {}, JSON.stringify({ parameters: job.params, resources: job.resources }, null, 2))));
    detail.replaceChildren(detailError, panel);
  };
  const checkDetail = async () => {
    if (!selected) return;
    const target = selected, name = pick.value, mine = ++detailGeneration;
    try {
      const out = await api(`${cluster(name)}/rest/jobs/status?` + new URLSearchParams({ id: target, tasks: "1" }));
      if (mine !== detailGeneration || target !== selected || name !== pick.value) return;
      const changed = JSON.stringify(out) !== JSON.stringify(lastDetail);
      detailError.textContent = "";
      lastDetail = out;
      if (changed) drawDetail(out, name);
    } catch (err) {
      if (mine === detailGeneration) {
        if (!lastDetail) detail.replaceChildren(el("p", { class: "error-box" }, err.message));
        else detailError.textContent = `Job detail refresh failed: ${err.message}`;
      }
    }
  };
  const openJob = async (job) => {
    selected = job.job_id; lastDetail = null; ++detailGeneration;
    taskPhase = "all"; outputBox = el("div"); detailError.textContent = "";
    detail.replaceChildren(message(`Checking job ${job.job_id}…`));
    await checkDetail();
    detail.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  const cluster = (name) => `clusters/${encodeURIComponent(name)}`;
  const load = async (reset = false) => {
    const name = pick.value, mine = ++generation;
    loading = true; checked.textContent = "Checking…";
    const jobsQuery = new URLSearchParams({ limit: limit.value });
    const auditQuery = new URLSearchParams({ limit: "500" });
    if (token.value.trim()) auditQuery.set("token", token.value.trim());
    if (!reset && latest != null) auditQuery.set("since", latest);
    const results = await Promise.allSettled([api(`${cluster(name)}/rest/jobs?${jobsQuery}`),
      api(`${cluster(name)}/rest/admin/audit?${auditQuery}`)]);
    if (mine !== generation || name !== pick.value) return;
    const [jobResult, auditResult] = results;
    if (jobResult.status === "fulfilled") {
      jobs = jobResult.value.jobs.slice().sort((a, b) => (b.submitted || 0) - (a.submitted || 0)); jobError = "";
    } else jobError = jobResult.reason.message;
    if (auditResult.status === "fulfilled") {
      entries = (reset ? auditResult.value.entries : entries.concat(auditResult.value.entries)).slice(-2000);
      latest = auditResult.value.latest; auditError = "";
    } else auditError = auditResult.reason.message;
    checked.textContent = `Checked ${new Date().toLocaleTimeString()}`;
    drawJobs(); drawAudit(); drawNotices();
    if (selected) await checkDetail();
    if (mine === generation) loading = false;
  };
  const setView = (next) => {
    jobPane.hidden = next !== "jobs"; auditPane.hidden = next !== "requests";
    jobsButton.setAttribute("aria-pressed", next === "jobs"); requestsButton.setAttribute("aria-pressed", next === "requests");
  };
  const jobsButton = el("button", { type: "button", "aria-pressed": "true", onclick: () => setView("jobs") }, "Jobs");
  const requestsButton = el("button", { type: "button", "aria-pressed": "false", onclick: () => setView("requests") }, "Requests");
  token.addEventListener("input", () => { drawJobs(); drawAudit(); });
  token.addEventListener("change", () => load(true).catch((err) => { auditError = err.message; drawNotices(); }));
  for (const control of [search, phase, period]) control.addEventListener(control === search ? "input" : "change", drawJobs);
  errorsOnly.addEventListener("change", drawAudit);
  limit.addEventListener("change", () => load().catch((err) => { jobError = err.message; drawNotices(); }));
  pick.addEventListener("change", () => {
    ++generation; jobs = []; entries = []; latest = null; jobError = ""; auditError = ""; closeDetail(); drawJobs(); drawAudit();
    load(true).catch((err) => { jobError = err.message; drawNotices(); });
  });
  follow.addEventListener("change", () => setFollowing(follow.checked ? () => {
    if (!page.isConnected) { setFollowing(null); return; }
    if (!loading) load().catch((err) => { jobError = err.message; loading = false; drawNotices(); });
  } : null));
  page.append(el("h2", {}, "Activity"), el("div", { class: "toolbar" }, pick, field("Agent", token),
    action("Check now", () => load()), el("label", {}, follow, " Follow every 5 seconds"), checked),
  el("div", { class: "toolbar", role: "group", "aria-label": "Activity view" }, jobsButton, requestsButton),
  notices, jobPane, auditPane);
  await load(true);
  return page;
}
