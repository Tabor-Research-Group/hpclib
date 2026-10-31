// Classic templates: the form over a script template that Submit job… was before it learned to build the script from
// the cluster's own answers (submit.js). Kept for what a template can say and the form can't: your own sbatch
// scripts in $HPCTUNNELS_DATA_DIR/script_templates. Opened from the guided form's "Classic templates…" link; registers
// the opener "submit-classic" (asked for by id).
//
//   POST /api/render   {template, input, values}            the script and the values it settled on
//   POST /api/submit   {script, name, dir, overwrite, ...}  writes it, runs sbatch, answers the job id
//
// Editing the preview by hand stops the form from changing it until "Rebuild from the form".

import { el, toast } from "hpclib-ui/index.js";
import { api, basename, dirname, join, ask, fill } from "../client.js";
import { ws, barButton } from "../shell.js";

let templatesCache = null;

async function templates(refresh = false) {
  if (!templatesCache || refresh) templatesCache = await api("/api/templates");
  return templatesCache;
}

async function openSubmit(input) {
  const box = el("div", { class: "submit" }, el("p", { class: "muted" }, "Loading the templates…"));
  const tab = ws.tabs.add({ label: `Template: ${basename(input)}`, title: input, path: input, persist: false, view: el("div", {}, box) });
  try { await draw(tab, box, await templates(true)); }
  catch (err) { box.replaceChildren(el("p", { class: "error-text" }, err.message)); }
  return tab;
}

async function draw(tab, box, list, chosen = null) {
  const input = tab.path;
  const fits = (t) => t.for.includes("*") || t.for.some((e) => input.endsWith(e));
  const offered = list.templates.filter(fits);
  const others = list.templates.filter((t) => !fits(t));
  const pick = chosen || (offered.find((t) => t.name === list.last) || offered.find((t) => t.name === "python-uv") ||
    offered[0] || list.templates[0] || {}).name;
  const template = list.templates.find((t) => t.name === pick);
  if (!template) { box.replaceChildren(el("p", {}, "No script templates.")); return; }
  const remembered = list.remembered[template.name] || {};
  const select = el("select", { "aria-label": "Template", onchange: () => draw(tab, box, list, select.value) },
    el("optgroup", { label: "For this file" }, offered.map((t) => el("option", { value: t.name, selected: t.name === pick },
      `${t.title}${t.source === "yours" ? " (yours)" : ""}`))),
    others.length ? el("optgroup", { label: "Others" }, others.map((t) => el("option", { value: t.name, selected: t.name === pick }, t.title))) : null);
  const inputs = {};
  let timer = null;
  const schedule = () => { clearTimeout(timer); timer = setTimeout(render, 250); };
  const fields = template.fields.map((f) => {
    const value = remembered[f.name] !== undefined ? remembered[f.name] : null;
    inputs[f.name] = el("input", { "aria-label": f.label, "data-field": f.name, spellcheck: "false",
      placeholder: f.hint || "", title: f.hint || "", oninput: () => { inputs[f.name].dataset.untouched = ""; schedule(); } });
    if (value !== null) inputs[f.name].value = value;
    inputs[f.name].dataset.untouched = value === null ? "1" : "";
    return el("label", { class: "field" }, f.label, inputs[f.name]);
  });
  const name = el("input", { class: "name", "aria-label": "Job script name", spellcheck: "false" });
  const overwrite = el("input", { type: "checkbox" });
  const result = el("div", { hidden: true });
  const handNote = el("span", { class: "muted small", hidden: true }, "Edited by hand: the form no longer changes it. ");
  const rebuild = el("button", { type: "button", class: "small-button", hidden: true,
    onclick: () => { handEdited = false; handNote.hidden = rebuild.hidden = true; render(); } }, "Rebuild from the form");
  let handEdited = false, settingPreview = false, rendered = null;
  const preview = el("hpc-code", { language: "shell" });
  const previewBox = el("div", { class: "preview" }, preview);
  preview.addEventListener("change", () => {
    if (settingPreview) return;
    handEdited = true;
    handNote.hidden = rebuild.hidden = false;
  });
  tab.onshow = () => preview.refresh();
  tab.onresize = () => preview.refresh();
  const values = () => {
    const out = {};
    for (const [k, i] of Object.entries(inputs)) if (!i.dataset.untouched || i.value) out[k] = i.value;
    return out;
  };
  async function render() {
    try {
      rendered = await api("/api/render", { method: "POST", body: { template: template.name, input, values: values() } });
      for (const f of template.fields) {
        const i = inputs[f.name];
        if (i.dataset.untouched && !i.value) i.placeholder = rendered.values[f.name] ? `${rendered.values[f.name]}` : (f.hint || "");
      }
      if (!name.dataset.touched) name.value = rendered.name;
      if (!handEdited) { settingPreview = true; preview.value = rendered.script; settingPreview = false; }
      result.hidden = true;
    } catch (err) {
      result.hidden = false;
      result.className = "result error";
      result.textContent = err.message;
    }
  }
  name.addEventListener("input", () => { name.dataset.touched = "1"; });
  const submit = el("button", { type: "button", class: "primary", onclick: async () => {
    submit.disabled = true;
    result.hidden = false;
    result.className = "result";
    result.textContent = "Submitting…";
    try {
      const vals = Object.assign({}, rendered ? rendered.values : {}, values());
      const out = await api("/api/submit", { method: "POST", body: { script: preview.value, name: name.value.trim(),
        dir: dirname(input), overwrite: overwrite.checked, template: template.name, values: vals } });
      const jobName = (rendered && rendered.values.job_name) || "";
      const guess = jobName && /%x-%j\.out/.test(preview.value) ? join(dirname(input), `${jobName}-${out.job_id}.out`) : null;
      result.className = "result ok";
      fill(result, `Submitted job ${out.job_id}: ${out.path}. `,
        guess ? el("button", { type: "button", class: "small-button", onclick: () => ws.open(guess, {}, { opener: "text" }) },
                   `Open ${basename(guess)}`) : null);
      templatesCache = null;
      document.dispatchEvent(new CustomEvent("ws:job-submitted", { detail: { id: out.job_id } }));
      ws.changed(dirname(input));
    } catch (err) {
      result.className = "result error";
      result.textContent = err.message;
    } finally { submit.disabled = false; }
  } }, "Submit");
  const edit = barButton(template.source === "yours" ? "Edit template" : "Edit (your copy)", async () => {
    try {
      const out = await api("/api/templates/edit", { method: "POST", body: { name: template.name } });
      toast(out.created ? `Your copy: ${out.path}. Changes apply the next time you open Submit job.` : `Yours: ${out.path}`, { ms: 7000 });
      templatesCache = null;
      ws.open(out.path, {}, { opener: "text" });
    } catch (err) { toast(err.message, { tone: "error" }); }
  });
  const copy = barButton("New template from this…", async () => {
    const n = await ask("A name for the new template (letters, digits, _ . -):", `${template.name}-mine`, "Create");
    if (!n) return;
    try {
      const out = await api("/api/templates/edit", { method: "POST", body: { name: n, from: template.name } });
      templatesCache = null;
      ws.open(out.path, {}, { opener: "text" });
    } catch (err) { toast(err.message, { tone: "error" }); }
  });
  fill(box,
    el("div", { class: "row" }, el("strong", {}, "Template"), select, edit, copy,
      el("span", { class: "muted small" }, template.source === "yours" ? `yours: ${template.path}` : `the Workspace's; yours go in ${list.dir}`)),
    template.description ? el("p", { class: "muted", style: "margin:0" }, template.description) : null,
    el("div", { class: "fields" }, fields),
    el("div", { class: "row" }, el("strong", {}, "Job script"), name, el("span", { class: "muted small" }, `in ${dirname(input)}`),
      el("label", { class: "check small" }, overwrite, "Replace one that exists")),
    el("div", { class: "row" }, handNote, rebuild),
    previewBox,
    el("div", { class: "row" }, submit, el("span", { class: "muted small" }, "Writes the job script there, then runs sbatch in that directory.")),
    result);
  await preview.ready;           // it starts once it is on the page
  await render();
}

ws.registerOpener({ id: "submit-classic", match: () => -1, open: (path) => openSubmit(path), restore: false });
