import { el } from "../core.js";
import { parseDuration, formatDuration, parseMemory, formatMemory } from "../units.js";

// ------------------------------------------------------------------ <hpc-resources>

const FIELDS = ["partition", "time", "nodes", "ntasks", "cpus", "memory", "memory_per", "gpus", "gpu_type", "account", "qos"];

export class HpcResources extends HTMLElement {
  // What a batch job asks of the cluster, as typed fields: a time as 6h or 1d 2h, memory as 8G (per node or per CPU),
  // GPUs by count and type, a partition and account from what the cluster has. Properties:
  //   cluster  {partitions: [{name, default, time_limit, time_limit_minutes, cpus, memory_mb, gpus: [{type, count}]}],
  //             accounts, default_account, gpu_types, slurm_problem}  (GET /api/cluster); optional: without it every
  //             field is free text
  //   value    {partition, time, nodes, ntasks, cpus, memory, memory_per, gpus, gpu_type, account, qos}: strings or
  //            numbers; set it, read it; "change" fires (with the value in detail) after each edit
  //   modules  [names]; suggest(query) -> Promise<[names]> fills the module box's suggestions (optional)
  // Each field says what it understood ("= 1-02:00:00") and what the chosen partition would not take.
  constructor() {
    super();
    this._cluster = null;
    this._modules = [];
    this.suggest = null;
    this._inputs = {};
  }

  connectedCallback() {
    if (this._built) return;
    this._built = true;
    const field = (name, label, attrs = {}, hint = true) => {
      const input = el(attrs.tag || "input", { "aria-label": label, "data-field": name, spellcheck: "false", ...attrs, tag: undefined,
        oninput: () => this._edited(name) });
      this._inputs[name] = input;
      const note = el("small", { class: "muted", "data-note": name });
      return el("label", { class: "field" }, el("span", {}, label), input, hint ? note : null);
    };
    this._lists = { partition: el("datalist", { id: this._id("partitions") }), account: el("datalist", { id: this._id("accounts") }),
      gpu_type: el("datalist", { id: this._id("gputypes") }), modules: el("datalist", { id: this._id("modules") }) };
    this._memoryPer = el("select", { "aria-label": "Memory is per", "data-field": "memory_per", onchange: () => this._edited("memory_per") },
      el("option", { value: "node" }, "per node"), el("option", { value: "cpu" }, "per CPU"));
    this._moduleBox = el("input", { "aria-label": "Modules", "data-field": "modules", spellcheck: "false", list: this._lists.modules.id,
      placeholder: "GCC/13.3.0 CUDA/12.6.0", oninput: () => this._modulesEdited() });
    this._status = el("p", { class: "resources-problems small", "aria-live": "polite" });
    this._problem = el("p", { class: "error-text small", hidden: true });
    this.replaceChildren(
      this._problem,
      el("div", { class: "resources-grid" },
        field("partition", "Partition", { list: this._lists.partition.id, placeholder: "the default" }),
        field("time", "Time limit", { placeholder: "6h, 1d 2h, 90m" }),
        field("nodes", "Nodes", { type: "number", min: "1" }, false),
        field("ntasks", "Tasks per node", { type: "number", min: "1" }, false),
        field("cpus", "CPUs per task", { type: "number", min: "1", placeholder: "the default" }, false),
        el("label", { class: "field" }, el("span", {}, "Memory"), el("span", { class: "inline" },
          (this._inputs.memory = el("input", { "aria-label": "Memory", "data-field": "memory", spellcheck: "false", placeholder: "8G",
            oninput: () => this._edited("memory") })), this._memoryPer), el("small", { class: "muted", "data-note": "memory" })),
        field("gpus", "GPUs", { type: "number", min: "0" }, false),
        field("gpu_type", "GPU type", { list: this._lists.gpu_type.id, placeholder: "any" }, false),
        field("account", "Account", { list: this._lists.account.id, placeholder: "the default" }, false),
        field("qos", "QOS", { placeholder: "the default" }, false)),
      el("label", { class: "field" }, el("span", {}, "Modules"), this._moduleBox, el("small", { class: "muted" }, "loaded first, space-separated")),
      this._status, ...Object.values(this._lists));
  }

  _id(name) { return `hpc-resources-${name}-${HpcResources.count = (HpcResources.count || 0) + 1}`; }

  get cluster() { return this._cluster; }
  set cluster(value) {
    this._cluster = value;
    if (!this._built) return;
    const fill = (list, items) => list.replaceChildren(...items.map((v) => el("option", { value: v })));
    fill(this._lists.partition, ((value && value.partitions) || []).map((p) => p.name));
    fill(this._lists.account, (value && value.accounts) || []);
    fill(this._lists.gpu_type, (value && value.gpu_types) || []);
    this._problem.hidden = !(value && value.slurm_problem);
    this._problem.textContent = value && value.slurm_problem ? `Slurm didn't answer here (${value.slurm_problem}); the fields are free text.` : "";
    this._inputs.partition.placeholder = (((value && value.partitions) || []).find((p) => p.default) || {}).name || "the default";
    if (value && value.default_account && !this._inputs.account.value) this._inputs.account.placeholder = value.default_account;
    this._notes();
  }

  get value() {
    const out = {};
    for (const name of FIELDS) {
      const input = name === "memory_per" ? this._memoryPer : this._inputs[name];
      if (input) out[name] = input.value.trim();
    }
    return out;
  }
  set value(v) {
    if (!this._built) this.connectedCallback();
    v = v || {};
    for (const name of FIELDS) {
      const input = name === "memory_per" ? this._memoryPer : this._inputs[name];
      if (input) input.value = v[name] === undefined || v[name] === null ? (name === "memory_per" ? "node" : "") : String(v[name]);
    }
    this._notes();
  }

  get modules() { return this._moduleBox ? this._moduleBox.value.split(/\s+/).filter(Boolean) : this._modules; }
  set modules(list) {
    this._modules = list || [];
    if (this._moduleBox) this._moduleBox.value = this._modules.join(" ");
  }

  _edited() {
    this._notes();
    this.dispatchEvent(new CustomEvent("change", { detail: this.value, bubbles: true }));
  }

  async _modulesEdited() {
    this.dispatchEvent(new CustomEvent("change", { detail: this.value, bubbles: true }));
    const last = this._moduleBox.value.split(/\s+/).pop();
    if (!this.suggest || last.length < 2) return;
    try {
      const names = await this.suggest(last);
      const head = this._moduleBox.value.slice(0, this._moduleBox.value.length - last.length);
      this._lists.modules.replaceChildren(...names.map((n) => el("option", { value: head + n })));
    } catch { /* suggestions are a convenience */ }
  }

  /** What each field understood, and what the partition wouldn't take (the server's warnings say the same, from Slurm). */
  _notes() {
    if (!this._built) return;
    const v = this.value;
    const note = (name, text) => { const n = this.querySelector(`[data-note="${name}"]`); if (n) n.textContent = text; };
    const minutes = parseDuration(v.time);
    note("time", !v.time ? "" : minutes === undefined ? "not a time: try 6h, 1d 2h or 6:00:00" : `= ${formatDuration(minutes)}`);
    const mb = parseMemory(v.memory);
    note("memory", !v.memory ? "" : mb === undefined ? "not a size: try 8G or 500M" : `= ${formatMemory(mb)} ${v.memory_per === "cpu" ? "for each CPU" : "on each node"}`);
    const parts = (this._cluster && this._cluster.partitions) || [];
    const part = parts.find((p) => p.name === v.partition) || parts.find((p) => p.default);
    const warnings = [];
    if (part) {
      const limit = part.time_limit_minutes;
      if (Number.isFinite(minutes) && limit && minutes > limit) warnings.push(`${part.name} allows ${formatDuration(limit)} at most`);
      if (mb && part.memory_mb && mb * (v.memory_per === "cpu" ? Math.max(+v.cpus || 1, 1) * (+v.ntasks || 1) : 1) > part.memory_mb) {
        warnings.push(`${part.name}'s nodes have ${formatMemory(part.memory_mb)}`);
      }
      if (+v.gpus && !part.gpus.some((g) => (!v.gpu_type || g.type === v.gpu_type) && g.count >= +v.gpus)) {
        warnings.push(`${part.name} has no node with ${v.gpus} ${v.gpu_type || ""} GPU${+v.gpus === 1 ? "" : "s"}`.replace("  ", " "));
      }
    }
    this._status.textContent = warnings.join("; ");
    this._status.hidden = !warnings.length;
  }
}

if (!customElements.get("hpc-resources")) customElements.define("hpc-resources", HpcResources);
