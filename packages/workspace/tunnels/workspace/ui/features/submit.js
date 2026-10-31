// Submit job…: what to run, the environment it runs in, what it asks of the cluster, and a button. The form is a JSON
// job spec; the server turns it into the sbatch script (shown here, read-only until you choose to edit it by hand),
// so nobody writes #SBATCH lines. The environment card says whether the project's uv or pixi environment is ready,
// and Prepare makes it. Registers the opener "submit" (asked for by id) and the action "Submit job…" for .py, .sh and
// .bash files, which an editor tab also shows on its bar.
//
//   GET  /api/cluster, /api/modules?q=            the partitions, accounts and GPU types; module names
//   GET  /api/env?path=                           the environment card; POST /api/env/{prepare,tool,dirs}, GET /api/env/operation
//   POST /api/jobs/render {spec}                  the script and what is wrong with the spec
//   POST /api/jobs/check, /api/jobs/submit        sbatch --test-only; write the script and sbatch it
//   GET  /api/profiles?dir=, POST /api/profiles, POST /api/profiles/delete    last used, the project's, and saved profiles
//   GET  /api/runs?dir=                           the runs a new job can wait for
//
// A .slurm or .sbatch file opens in the same form ("Open as job form…"): a script the form made comes back as its own
// spec; one written by hand is read (its #SBATCH lines into the fields, the rest kept as written) and the form shows what
// it would change, with "Adopt the form's version" (the file is rewritten, the original kept as NAME.bak) or "Keep mine"
// (the file is submitted as it lies). "Interactive session" opens a terminal in the dock running srun --pty with the
// form's resources.
//
//   GET /api/jobs/import?path=, POST /api/jobs/adopt {path, spec}, POST /api/jobs/interactive {spec}
//
// "Many tasks" makes the job an array or a sweep (every combination of some variables is a task); "Start after" waits
// for other jobs; a profile is the cluster and environment part of a spec, remembered by the server.
//
// Submit waits while the environment is not prepared, stale, busy or failed, unless the job is told to sync it itself.
// The script's text is hand-editable ("Edit by hand…"): then the form no longer changes it, and "Back to the form"
// makes it again. The template form this replaced is one link away ("Classic templates…").

import { el, remember, toast } from "hpclib-ui/index.js";
import { api, q, basename, dirname, confirmAsk, ask, fill } from "../client.js";
import { ws, barButton } from "../shell.js";

const follow = (ctx) => ws.dockOpen("follow", ctx);
const openClassic = (path) => ws.open(path, {}, { opener: "submit-classic" });
const SUBMITTABLE = /\.(py|sh|bash)$/i;
const SLURM_FILE = /\.(slurm|sbatch)$/i;                  // a job script: opened in the form, not run by it
const DIR_LABELS = { UV_CACHE_DIR: "uv's package cache", UV_PYTHON_INSTALL_DIR: "Pythons uv downloads", PIXI_CACHE_DIR: "pixi's package cache" };
const MODES = [["auto", "Use this environment"], ["sync", "Let the job sync it first"], ["none", "Don't activate one"]];
let clusterAnswer = null;

const cluster = async () => clusterAnswer || (clusterAnswer = await api("/api/cluster"));
/** `promise`, or an error saying what didn't answer if it takes longer than `seconds` (a login node can be slow, and a
 *  form that waits for ever is worse than one that opens without that part). */
const within = (promise, seconds, what) => Promise.race([promise,
  new Promise((_, reject) => setTimeout(() => reject(new Error(`${what} didn't answer within ${seconds} s`)), seconds * 1000))]);

async function openSubmit(input) {
  const box = el("div", { class: "submit" }, el("p", { class: "muted" }, "Asking the cluster… (Slurm and your environment tools; each gives up after a short wait)"));
  const tab = ws.tabs.add({ label: `${SLURM_FILE.test(input) ? "Job" : "Submit"}: ${basename(input)}`, title: input, path: input, persist: false, view: el("div", {}, box) });
  try { await build(tab, box); }
  catch (err) { box.replaceChildren(el("p", { class: "error-text" }, err.message)); }
  return tab;
}

async function build(tab, box) {
  const input = tab.path;
  const imported = SLURM_FILE.test(input) ? await api(`/api/jobs/import?${q({ path: input })}`) : null;
  const was = imported ? imported.spec : null;
  // The form opens at once; the two slow questions (Slurm, and the environment tools) fill it in when they answer, each
  // giving up after a while, and the form says how long each took so a slow login node shows where the time goes.
  const began = performance.now(), took = {};
  const timed = (key, promise) => {
    const done = () => { took[key] = (performance.now() - began) / 1000; showTimes(); };
    return promise.then((value) => { done(); return value; }, (err) => { done(); throw err; });
  };
  const askCluster = timed("Slurm", within(cluster(), 30, "Slurm")).catch((err) => ({ partitions: [], gpu_types: [], accounts: [], slurm_problem: err.message }));
  const askEnv = timed("the environment check", within(api(`/api/env?${q({ path: input })}`), 60, "the environment check")).catch((err) => ({
    start: input, found: false, status: "unknown", reason: `couldn't read the environment: ${err.message}`, tools: {}, dirs: {} }));
  const card = { start: input, found: false, status: "checking", reason: "checking the environment…", tools: {}, dirs: {} };
  let envPending = true;
  const stem = (was && was.name) || basename(input).replace(/\.[^.]*$/, "");
  const settings = { mode: "auto", shell_init: "bashrc" };
  let handEdited = false, settingPreview = false, last = null, timer = null, envCard = card;
  const times = el("div", { class: "muted small" });
  function showTimes() {
    const parts = Object.entries(took).map(([k, s]) => `${k} answered in ${s.toFixed(1)} s`);
    const pending = [!("Slurm" in took) ? "Slurm" : null, !("the environment check" in took) ? "the environment check" : null].filter(Boolean);
    times.textContent = [...parts, ...pending.map((k) => `waiting for ${k}…`)].join(" · ") + (envCard.took ? ` (tools ${envCard.took.tools} s${envCard.took.detect !== undefined ? `, project ${envCard.took.detect} s` : ""})` : "");
  }

  // what to run
  const name = el("input", { "aria-label": "Job name", value: stem, spellcheck: "false", oninput: schedule });
  const args = el("input", { "aria-label": "Arguments", placeholder: "arguments for the script", spellcheck: "false", oninput: schedule });
  const workdir = el("input", { "aria-label": "Working directory", value: was ? was.workdir : dirname(input), spellcheck: "false", oninput: schedule });
  const scriptName = el("input", { class: "name", "aria-label": "Job script file", value: imported ? basename(input) : `${stem}.slurm`, spellcheck: "false" });

  // the environment card
  const envBody = el("div", {});
  const envPanel = el("hpc-panel", { heading: "Environment" }, envBody);
  const opLog = el("pre", { class: "log", hidden: true });

  // resources
  const resources = el("hpc-resources");

  // profiles: last used, the project's, and your own
  let profiles = { last: null, project: null, profiles: [] };
  const profileSelect = el("select", { "aria-label": "Profile", onchange: () => chooseProfile() });
  const saveProfile = el("button", { type: "button", class: "small-button", title: "Keep these resources, modules and environment settings under a name", onclick: () => keepProfile() }, "Save as profile…");
  const dropProfile = el("button", { type: "button", class: "small-button", hidden: true, onclick: () => deleteProfile() }, "Delete profile");

  // many tasks: an array range and limit, and a sweep of variables
  const arrayRange = el("input", { "aria-label": "Array range", placeholder: "e.g. 1-10 or 0-99:2", size: 14, spellcheck: "false", oninput: () => { tasksChanged(); schedule(); } });
  const arrayLimit = el("input", { "aria-label": "Tasks at once", placeholder: "all", size: 5, inputmode: "numeric", oninput: schedule });
  const sweepRows = el("div", { class: "sweep-rows" });
  const taskNote = el("span", { class: "muted small" });
  const tasks = el("details", { class: "fan" }, el("summary", {}, "Many tasks (array or sweep)"),
    el("div", { class: "row" }, el("label", { class: "field-inline" }, "Array range ", arrayRange), el("label", { class: "field-inline" }, "at most ", arrayLimit, " at once")),
    el("div", { class: "muted small" }, "A sweep runs one task for every combination of its variables. Each task sees them as $NAME, and {NAME} in the arguments becomes the value."),
    sweepRows, el("div", { class: "row" }, el("button", { type: "button", class: "small-button", onclick: () => addSweep() }, "Add a variable"), taskNote));

  // start after other jobs
  const depKind = el("select", { "aria-label": "Start after", onchange: schedule },
    [["afterok", "after they finish successfully"], ["afterany", "after they end, however"], ["afternotok", "after they fail"], ["after", "after they start"]]
      .map(([v, label]) => el("option", { value: v }, label)));
  const depIds = el("input", { "aria-label": "Job numbers", placeholder: "job numbers, e.g. 12345 12346", size: 28, spellcheck: "false", oninput: schedule });
  const depRuns = el("div", { class: "dep-runs small" });
  const after = el("details", { class: "fan", ontoggle: () => after.open && loadDependencies() }, el("summary", {}, "Start after other jobs"),
    el("div", { class: "row" }, "Start ", depKind), depRuns, el("div", { class: "row" }, depIds));

  // the script
  const preview = el("hpc-code", { language: "shell", readonly: "" });
  const handNote = el("span", { class: "muted small", hidden: true }, "Edited by hand: the form no longer changes it. ");
  const problems = el("div", { class: "problems small", "aria-live": "polite" });
  const result = el("div", { class: "result", hidden: true });
  const check = el("button", { type: "button", onclick: () => run("check") }, "Check");
  const submit = el("button", { type: "button", class: "primary", onclick: () => run("submit") }, "Submit");
  const submitFollow = el("button", { type: "button", class: "primary", onclick: () => run("submit", true) }, "Submit and follow");
  const interactive = el("button", { type: "button", title: "Ask for these resources and open a shell on the compute node (srun --pty), in a terminal below", onclick: () => interactiveSession() }, "Interactive session");
  const buttons = [check, submit, submitFollow];
  const edit = el("button", { type: "button", class: "small-button", onclick: () => {
    handEdited = true;
    preview.removeAttribute("readonly");
    handNote.hidden = back.hidden = false;
    edit.hidden = true;
    preview.focus();
  } }, "Edit by hand…");
  const back = el("button", { type: "button", class: "small-button", hidden: true, onclick: () => {
    handEdited = asIs = false;
    preview.setAttribute("readonly", "");
    handNote.hidden = back.hidden = true;
    handNote.textContent = "Edited by hand: the form no longer changes it. ";
    edit.hidden = false;
    if (imported && !adopted) { importBox.hidden = !imported.differs; if (imported.differs) drawImportBox(); }
    render();
  } }, "Back to the form");
  tab.onshow = () => preview.refresh();
  tab.onresize = () => preview.refresh();

  const spec = () => ({ name: name.value.trim() || stem, workdir: workdir.value.trim(),
    ...(was ? { kind: "body", command: "", body: was.body, extra_sbatch: was.extra_sbatch || [] } : { kind: "script", command: input }),
    arguments: args.value, resources: resources.value, modules: resources.modules,
    environment: { mode: settings.mode, shell_init: settings.shell_init }, output: was ? was.output : "%x-%j.out", ...fanOut() });
  // a script kept as it lies is submitted from where it is, not written again (asIs); a form's version replaces the file
  let asIs = false, adopted = false;
  const fileDir = dirname(input);
  const body = () => (handEdited ? { script: preview.value, dir: workdir.value.trim(), name: scriptName.value.trim(), ...(imported ? { script_dir: fileDir } : {}), ...(asIs ? { as_is: true } : {}) }
    : { spec: spec(), name: scriptName.value.trim(), ...(imported ? { overwrite: true, script_dir: fileDir } : {}) });

  function schedule() { clearTimeout(timer); timer = setTimeout(render, 300); }

  // ---- many tasks, and what to wait for
  function sweepSpec() {
    const sweep = {};
    for (const row of sweepRows.children) {
      const [key, values] = row.querySelectorAll("input");
      if (key.value.trim()) sweep[key.value.trim()] = values.value;
    }
    return sweep;
  }
  function fanOut() {
    const out = {}, sweep = sweepSpec(), range = arrayRange.value.trim(), limit = arrayLimit.value.trim();
    if (Object.keys(sweep).length) out.sweep = sweep;
    if (range || limit || out.sweep) out.array = { range, limit };
    const jobs = [...depRuns.querySelectorAll("input:checked")].map((b) => b.value).concat(depIds.value.split(/[\s,]+/).filter(Boolean));
    if (jobs.length) out.dependency = { type: depKind.value, jobs };
    return out;
  }
  function addSweep(name = "", values = "") {
    const row = el("div", { class: "row sweep-row" },
      el("input", { "aria-label": "Variable name", placeholder: "NAME", size: 12, value: name, spellcheck: "false", oninput: () => { tasksChanged(); schedule(); } }),
      "=", el("input", { "aria-label": "Values", placeholder: "values, separated by spaces: 0.1 0.01 0.001", size: 34, value: values, spellcheck: "false", oninput: () => { tasksChanged(); schedule(); } }),
      el("button", { type: "button", class: "small-button", "aria-label": "Remove the variable", onclick: () => { row.remove(); tasksChanged(); schedule(); } }, "Remove"));
    sweepRows.append(row);
    tasksChanged();
  }
  function tasksChanged() {
    const sweep = sweepSpec(), names = Object.keys(sweep);
    if (names.length) {
      const total = names.reduce((n, k) => n * (sweep[k].split(/[\s,]+/).filter(Boolean).length || 0), 1);
      taskNote.textContent = `${total} task${total === 1 ? "" : "s"}, numbered from 0${arrayRange.value.trim() ? " (the range above is ignored)" : ""}`;
    } else taskNote.textContent = "";
    tasks.querySelector("summary").textContent = names.length ? `Many tasks: a sweep of ${names.join(" × ")}` : arrayRange.value.trim() ? `Many tasks: array ${arrayRange.value.trim()}` : "Many tasks (array or sweep)";
  }
  async function loadDependencies() {
    depRuns.textContent = "…";
    try {
      const out = await api(`/api/runs?${q({ dir: workdir.value.trim() || dirname(input) })}`);
      const open = out.runs.filter((r) => !r.ended).slice(0, 15);
      fill(depRuns, open.length ? open.map((r) => el("label", { class: "dep-run" },
        el("input", { type: "checkbox", value: r.job_id, onchange: schedule }), ` ${r.name} `, el("span", { class: "muted" }, `#${r.job_id} ${(r.state || "").toLowerCase()}`)))
        : el("span", { class: "muted" }, "None of the runs from here is waiting or running; type job numbers below to wait for any job."));
    } catch (err) { depRuns.textContent = err.message; }
  }

  // ---- profiles
  function drawProfiles(keep) {
    const options = [el("option", { value: "" }, "Profile…")];
    if (profiles.last) options.push(el("option", { value: "last" }, "Last used"));
    if (profiles.project) options.push(el("option", { value: "project" }, `This project (${profiles.project.problem ? "can't read .hpclib/job.json" : ".hpclib/job.json"})`));
    for (const p of profiles.profiles) options.push(el("option", { value: `p:${p.name}` }, p.name));
    fill(profileSelect, options);
    profileSelect.value = keep || "";
    dropProfile.hidden = !profileSelect.value.startsWith("p:");
  }
  function profileFor(key) {
    if (key === "last") return profiles.last;
    if (key === "project") return profiles.project && profiles.project.spec;
    const mine = profiles.profiles.find((p) => `p:${p.name}` === key);
    return mine && mine.spec;
  }
  function applyProfile(p) {
    resources.value = p.resources;
    resources.modules = p.modules || [];
    settings.mode = (p.environment || {}).mode || "auto";
    settings.shell_init = (p.environment || {}).shell_init || "bashrc";
    drawEnv();
    schedule();
  }
  function chooseProfile() {
    dropProfile.hidden = !profileSelect.value.startsWith("p:");
    if (profileSelect.value === "project" && profiles.project && profiles.project.problem) {
      toast(`${profiles.project.path}: ${profiles.project.problem}`, { tone: "error" });
      profileSelect.value = "";
      return;
    }
    const p = profileFor(profileSelect.value);
    if (p) applyProfile(p);
  }
  async function loadProfiles() {
    try { profiles = await api(`/api/profiles?${q({ dir: workdir.value.trim() || dirname(input) })}`); }
    catch (err) { profiles = { last: null, project: null, profiles: [] }; }
    drawProfiles();
  }
  async function keepProfile() {
    const profileName = await ask("Name for this profile", "");
    if (!profileName || !profileName.trim()) return;
    try {
      const out = await api("/api/profiles", { method: "POST", body: { name: profileName.trim(), spec: spec() } });
      await loadProfiles();
      drawProfiles(`p:${out.name}`);
      toast(`Saved the profile ${out.name}`);
    } catch (err) { toast(err.message, { tone: "error" }); }
  }
  async function deleteProfile() {
    const key = profileSelect.value;
    if (!key.startsWith("p:") || !await confirmAsk(`Delete the profile ${key.slice(2)}?`, "Delete")) return;
    try { await api("/api/profiles/delete", { method: "POST", body: { name: key.slice(2) } }); await loadProfiles(); }
    catch (err) { toast(err.message, { tone: "error" }); }
  }

  async function render() {
    try {
      last = await api("/api/jobs/render", { method: "POST", body: { spec: spec() } });
    } catch (err) { last = { errors: [err.message], warnings: [], script: null }; }
    const blocked = !handEdited && (envPending || (last.environment && last.environment.blocks));
    fill(problems,
      (last.errors || []).map((e) => el("div", { class: "error-text" }, e)),
      (last.warnings || []).map((w) => el("div", { class: "warning" }, w)),
      envPending ? el("div", { class: "muted" }, "Still checking the environment; Submit waits for it.")
        : blocked ? el("div", { class: "warning" }, `The environment is ${last.environment.status} (${last.environment.reason}): prepare it above, or let the job sync it.`) : null);
    if (!handEdited) {
      settingPreview = true;
      preview.value = last.script || "# the form has something to fix first (see above)";
      settingPreview = false;
    }
    check.disabled = !last.script && !handEdited;
    submit.disabled = submitFollow.disabled = !handEdited && (!last.script || blocked);
    submit.title = submitFollow.title = envPending ? "Still checking the environment" : blocked ? "Prepare the environment first" : "";
    if (!imported) remember.set("ws.submit.resources", { resources: resources.value, modules: resources.modules });
    showDiff();
  }

  // ---- a script that was opened, not made: what the form would change in it, and the two ways out
  const diffBox = el("div", { class: "diff-box" });
  const diffView = el("hpc-diff", { language: "shell" });
  const diffNote = el("span", { class: "muted small" }, "The form has something to fix first.");
  const importBox = el("div", { class: "import-box", hidden: !imported || !imported.differs });
  function showDiff() {
    if (!imported || importBox.hidden || adopted || asIs) return;
    const ok = last && last.script;
    if (ok) diffView.show(imported.text, last.script);
    diffBox.replaceChildren(ok ? diffView : diffNote);
  }
  async function adopt() {
    try {
      const out = await api("/api/jobs/adopt", { method: "POST", body: { path: input, spec: spec() } });
      adopted = true;
      fill(importBox, el("div", {}, `The form's version is now in ${basename(input)}; the original is ${basename(out.backup)} beside it. `,
        "From here on the form and the file go together."));
      ws.changed(dirname(input));
    } catch (err) { toast(err.message, { tone: "error" }); }
  }
  function keepMine() {
    handEdited = asIs = true;
    settingPreview = true;
    preview.value = imported.text;
    settingPreview = false;
    preview.removeAttribute("readonly");
    handNote.hidden = false;
    handNote.textContent = "Kept as you wrote it: Submit sends this file as it lies (change the text and it is written as a new version). ";
    edit.hidden = true;
    back.hidden = false;
    fill(importBox, el("div", {}, "Keeping your script as written. ", el("span", { class: "muted small" }, "The form's fields don't change it; Back to the form shows its version again.")));
    submit.disabled = submitFollow.disabled = false;
    check.disabled = false;
  }
  function drawImportBox() {
    fill(importBox,
      el("div", {}, el("strong", {}, imported.source === "embedded" ? "This script was made by the form, and has been changed by hand since. " : "This script was written by hand. "),
        imported.source === "embedded" ? "The form shows its settings; the diff is what the form's version would change in the file."
          : "The form read its #SBATCH lines into the fields below and kept everything else as written; the diff is what the form's version would change in the file."),
      (imported.import_warnings || []).map((w) => el("div", { class: "warning" }, w)),
      el("details", { open: true }, el("summary", {}, "What the form's version would change"), diffBox),
      el("div", { class: "row" }, el("button", { type: "button", class: "primary small-button", onclick: adopt, title: `Writes the form's version to ${basename(input)} and keeps yours as ${basename(input)}.bak` }, "Adopt the form's version"),
        el("button", { type: "button", class: "small-button", onclick: keepMine, title: "Submit the file as you wrote it" }, "Keep mine")));
    showDiff();
  }
  if (imported) {
    drawImportBox();
    preview.addEventListener("change", () => { if (!settingPreview && handEdited) asIs = false; });
  }

  async function interactiveSession() {
    try {
      const out = await api("/api/jobs/interactive", { method: "POST", body: { spec: spec() } });
      for (const w of out.warnings || []) toast(w);
      ws.dockOpen("terminal", { cwd: out.workdir, label: `Interactive: ${name.value.trim() || stem}`, command: out.command });
    } catch (err) { toast(err.message, { tone: "error" }); }
  }

  async function run(what, andFollow = false) {
    for (const b of buttons) b.disabled = true;
    result.hidden = false;
    result.className = "result";
    result.textContent = what === "check" ? "Asking Slurm…" : "Submitting…";
    try {
      if (what === "check") {
        const out = await api("/api/jobs/check", { method: "POST", body: body() });
        result.className = `result ${out.ok ? "ok" : "error"}`;
        result.textContent = out.ok ? `Slurm would take it: ${out.message}.` : `Slurm would refuse it: ${out.message}`;
        return;
      }
      let out;
      try { out = await api("/api/jobs/submit", { method: "POST", body: body() }); }
      catch (err) {
        if (err.status !== 409 || !/already exists/.test(err.message)) throw err;
        if (!await confirmAsk(`${err.message}. Replace it?`, "Replace")) { result.hidden = true; return; }
        out = await api("/api/jobs/submit", { method: "POST", body: { ...body(), overwrite: true } });
      }
      result.className = "result ok";
      fill(result, `Submitted job ${out.job_id}: ${out.path}. `,
        el("button", { type: "button", class: "small-button", onclick: () => follow({ job: out.job_id, output: out.output_path, label: name.value }) }, "Follow output"),
        " ", el("button", { type: "button", class: "small-button", onclick: () => ws.open(out.path, {}, { opener: "text" }) }, "Open the script"));
      document.dispatchEvent(new CustomEvent("ws:job-submitted", { detail: { id: out.job_id } }));
      ws.changed(dirname(out.path));
      if (andFollow) follow({ job: out.job_id, output: out.output_path, label: name.value });
    } catch (err) {
      result.className = "result error";
      result.textContent = err.message;
    } finally {
      render();
    }
  }

  // ---- the environment card
  function drawEnv() {
    const c = envCard;
    const status = el("hpc-state", { state: c.status, label: c.status });
    const lines = [];
    if (c.found) {
      const i = c.info || {};
      lines.push(el("div", {}, status, " ", el("span", {}, c.reason), " ",
        el("span", { class: "muted small" }, `${c.manager} project ${c.project}`)));
      const bits = [i.python && i.python.version ? `Python ${i.python.version}` : null, i.packages ? `${i.packages} packages` : null,
        i.synced ? `prepared ${new Date(i.synced * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}` : null].filter(Boolean);
      if (bits.length) lines.push(el("div", { class: "muted small" }, bits.join(" · ")));
    } else {
      lines.push(el("div", {}, status, " ", el("span", {}, c.reason), " ",
        el("span", { class: "muted small" }, "The job runs with your shell's Python and the modules below.")));
    }
    const buttonsRow = el("div", { class: "row" });
    const manager = c.manager;
    const tool = manager && c.tools[manager] && c.tools[manager].path;
    if (c.found && c.operation) {
      buttonsRow.append(el("span", { class: "muted small" }, `${c.operation.kind} is running…`));
      watch(c.operation.id);
    } else if (c.status === "tool missing") {
      buttonsRow.append(el("button", { type: "button", class: "primary", onclick: () => operate("/api/env/tool", { tool: manager }, `Installing ${manager}…`) }, `Install ${manager}`));
    } else if (c.found && ["not prepared", "stale", "failed"].includes(c.status)) {
      buttonsRow.append(el("button", { type: "button", class: "primary", onclick: () => operate("/api/env/prepare", { project: c.project }, "Preparing…") },
        c.status === "not prepared" ? "Prepare" : "Prepare again"));
    } else if (c.found && c.status === "ready" && tool) {
      buttonsRow.append(el("button", { type: "button", class: "small-button", onclick: () => operate("/api/env/prepare", { project: c.project, update: true }, "Updating…") },
        "Update packages"));
    }
    if (c.found) {
      const select = el("select", { "aria-label": "How the job gets its environment", onchange: () => { settings.mode = select.value; schedule(); } },
        MODES.map(([v, label]) => el("option", { value: v, selected: v === settings.mode }, label)));
      buttonsRow.append(select);
    }
    const dirs = el("details", { open: c.dirsOpen || null, ontoggle: (e) => { c.dirsOpen = e.target.open; if (e.target.open && !c.dirs_loaded) loadDirs(); } },
      el("summary", {}, "uv and pixi on this cluster"),
      c.dirs_loaded || !c.dirsOpen ? null : el("div", { class: "muted small" }, c.dirs_problem ? `Couldn't read the directories: ${c.dirs_problem}` : "Reading the directories…"),
      el("div", { class: "small muted" }, ["uv", "pixi"].map((t) => `${t}: ${c.tools[t] && c.tools[t].path ? `${c.tools[t].path} (${c.tools[t].version || "?"})` : "not installed"}`).join("   ")),
      el("div", { class: "row" }, ["uv", "pixi"].filter((t) => !(c.tools[t] && c.tools[t].path)).map((t) =>
        el("button", { type: "button", class: "small-button", onclick: () => operate("/api/env/tool", { tool: t }, `Installing ${t}…`) }, `Install ${t}`))),
      Object.entries(c.dirs).map(([v, d]) => {
        const field = el("input", { "aria-label": DIR_LABELS[v] || v, value: d.value || "", spellcheck: "false" });
        return el("div", { class: "row dir-row" }, el("span", { class: "dir-label" }, DIR_LABELS[v] || v), field,
          el("span", { class: "muted small" }, d.source),
          el("button", { type: "button", class: "small-button", onclick: () => setDir(v, field.value.trim()) }, "Save"));
      }),
      scratchButton(c));
    fill(envBody, ...lines, buttonsRow, opLog, dirs);
  }

  /** uv's and pixi's directories come from a slow shell, so they are read when the section is first opened. */
  async function loadDirs() {
    try {
      const out = await within(api("/api/env/settings"), 100, "reading the directories");
      envCard.dirs = out.dirs || {};
      envCard.dirs_loaded = !out.problem;
      envCard.dirs_problem = out.problem || null;
    } catch (err) { envCard.dirs_problem = err.message; }
    drawEnv();
  }

  function scratchButton(c) {
    const place = (ws.info.places || []).find((p) => p.label === "Scratch" || p.label === "Work");
    if (!place) return null;
    return el("div", { class: "row" }, el("button", { type: "button", class: "small-button", onclick: async () => {
      if (!await confirmAsk(`Keep uv's and pixi's caches and downloaded Pythons under ${place.path}? (They are large, and home directories are small.)`, "Use it")) return;
      await setDir("UV_CACHE_DIR", `${place.path}/.cache/uv`);
      await setDir("UV_PYTHON_INSTALL_DIR", `${place.path}/.local/share/uv/python`);
      await setDir("PIXI_CACHE_DIR", `${place.path}/.cache/rattler`);
    } }, `Use ${place.label.toLowerCase()} (${place.path})…`));
  }

  async function setDir(variable, dir) {
    try {
      const out = await api("/api/env/dirs", { method: "POST", body: { var: variable, dir: dir || "default" } });
      envCard.dirs = out.dirs;
      envCard.dirs_loaded = true;
      drawEnv();
    } catch (err) { toast(err.message, { tone: "error" }); }
  }

  async function operate(path, payload, title) {
    opLog.hidden = false;
    opLog.textContent = title;
    try {
      const op = await api(path, { method: "POST", body: payload });
      watch(op.id);
    } catch (err) { opLog.textContent = err.message; opLog.classList.add("error"); }
  }

  async function watch(id) {
    opLog.hidden = false;
    opLog.classList.remove("error");
    for (;;) {
      let op;
      try { op = await api(`/api/env/operation?${q({ id })}`); }
      catch (err) { opLog.textContent = err.message; return; }
      opLog.textContent = op.log || `${op.kind}…`;
      opLog.scrollTop = opLog.scrollHeight;
      if (op.state !== "running") {
        opLog.classList.toggle("error", op.state !== "succeeded");
        break;
      }
      await new Promise((r) => setTimeout(r, 1000));
      if (!tab.view.isConnected) return;
    }
    envCard = await api(`/api/env?${q({ path: input })}`);
    drawEnv();
    opLog.hidden = false;
    render();
  }

  fill(box,
    el("div", { class: "row" }, el("strong", {}, imported ? "Job script" : "Run"), el("code", {}, input),
      imported ? null : el("button", { type: "button", class: "small-button push-right", onclick: () => openClassic(input) }, "Classic templates…")),
    importBox,
    el("div", { class: "fields" },
      el("label", { class: "field" }, "Job name", name), imported ? null : el("label", { class: "field" }, "Arguments", args),
      el("label", { class: "field" }, "Working directory", workdir)),
    envPanel,
    el("div", { class: "row" }, el("strong", {}, "Resources"), profileSelect, saveProfile, dropProfile),
    resources,
    tasks,
    after,
    el("div", { class: "row" }, el("strong", {}, "Job script"), scriptName, handNote, edit, back),
    el("div", { class: "preview" }, preview),
    problems,
    times,
    el("div", { class: "row actions-bar" }, check, submit, submitFollow, interactive,
      el("span", { class: "muted small" }, "Check asks Slurm whether it would take the job, and when it would start. Interactive session gives you a shell on a compute node with these resources.")),
    result);
  showTimes();
  askCluster.then((info) => {
    resources.cluster = info;
    if (info && info.slurm_problem) {
      box.prepend(el("p", { class: "note-box" }, `The form can't check partitions or limits: ${info.slurm_problem}. ` +
        "You can still fill it in; Check asks Slurm itself."));
    }
  });
  askEnv.then((got) => { envCard = got; envPending = false; drawEnv(); showTimes(); render(); });
  const saved = remember.get("ws.submit.resources", null);
  resources.value = (saved && saved.resources) || { time: "6:00:00", memory: "8G", nodes: 1, ntasks: 1, memory_per: "node" };
  resources.modules = (saved && saved.modules) || [];
  await loadProfiles();                       // the project's profile is offered first, then the last used; the browser's copy is the fallback
  if (was) {                                  // an opened script: its own settings, not a profile's
    resources.value = was.resources || {};
    resources.modules = was.modules || [];
    settings.mode = (was.environment || {}).mode || "none";
    settings.shell_init = (was.environment || {}).shell_init || "none";
    if (was.array) { arrayRange.value = was.array.range || ""; arrayLimit.value = was.array.limit || ""; tasks.open = !!was.array.range; tasksChanged(); }
    if (was.dependency) { depKind.value = was.dependency.type; depIds.value = was.dependency.jobs.join(" "); after.open = true; }
    drawProfiles();
  } else {
    const start = profiles.project && profiles.project.spec ? "project" : profiles.last ? "last" : "";
    if (start) { applyProfile(profileFor(start)); drawProfiles(start); }
  }
  resources.suggest = async (s) => (await api(`/api/modules?${q({ q: s })}`)).modules;
  resources.addEventListener("change", schedule);
  drawEnv();
  await preview.ready;
  await render();
}

ws.registerOpener({ id: "submit", match: () => -1, open: (path) => openSubmit(path), restore: false });
ws.registerAction({ id: "job-form", label: "Open as job form…", group: "run", bar: true,
  when: (e) => !e.dir && SLURM_FILE.test(e.path), run: (e) => ws.open(e.path, e, { opener: "submit" }) });
ws.registerAction({ id: "submit", label: "Submit job…", group: "run", bar: true,
  when: (e) => !e.dir && SUBMITTABLE.test(e.path), run: (e) => ws.open(e.path, e, { opener: "submit" }) });
