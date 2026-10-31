import { el } from "../core.js";

// ------------------------------------------------------------------ <hpc-env-editor>

export class HpcEnvEditor extends HTMLElement {
  // Environment variables as NAME=value lines (# comments allowed), checked as you type against .rules: the
  // server's own (GET /admin/config's rules.environment: name_pattern, reserved, reserved_prefixes, max_value);
  // without them only the NAME=value form is checked, and the server still decides on save.
  // .value: {NAME: value} (throws, naming the line, if one is wrong); label: names it in those messages.
  constructor() {
    super();
    this.textarea = el("textarea", { rows: 3, spellcheck: "false", placeholder: "NAME=value, one per line" });
    this.messages = el("ul", { class: "env-errors" });
    this.textarea.addEventListener("input", () => this._check());
    this.rules = null;
  }

  connectedCallback() {
    if (!this.textarea.isConnected) this.append(this.textarea, this.messages);
    if (this.hasAttribute("rows")) this.textarea.rows = Number(this.getAttribute("rows"));
    this._check();
  }

  set value(vars) {
    this.textarea.value = Object.entries(vars || {}).map(([k, v]) => `${k}=${v}`).join("\n");
    this._check();
  }
  get value() {
    const { vars, errors } = this.parse();
    if (errors.length) throw new Error(`${this.getAttribute("label") || "Environment"}: ${errors[0]}`);
    return vars;
  }
  get errors() { return this.parse().errors; }

  parse() {
    const r = this.rules || {};
    const pattern = new RegExp(`^(?:${r.name_pattern || "[A-Za-z_][A-Za-z0-9_]*"})$`);
    const vars = {};
    const errors = [];
    this.textarea.value.split("\n").forEach((raw, i) => {
      const line = raw.trim();
      if (!line || line.startsWith("#")) return;
      const at = `line ${i + 1}`;
      const eq = line.indexOf("=");
      if (eq < 1) return errors.push(`${at}: "${line}" is not NAME=value`);
      const name = line.slice(0, eq).trim();
      const value = line.slice(eq + 1).trim();
      if (!pattern.test(name)) errors.push(`${at}: ${name} is not a variable name`);
      else if ((r.reserved || []).includes(name) || (r.reserved_prefixes || []).some((p) => name.startsWith(p))) {
        errors.push(`${at}: ${name} can't be set here (it would change PATH, the sandbox, hpclib or SLURM)`);
      } else if (r.max_value && value.length > r.max_value) errors.push(`${at}: the value is over ${r.max_value} characters`);
      else if (name in vars) errors.push(`${at}: ${name} is set twice`);
      vars[name] = value;
    });
    return { vars, errors };
  }

  _check() {
    const { errors } = this.parse();
    this.messages.replaceChildren(...errors.map((e) => el("li", {}, e)));
    this.textarea.toggleAttribute("aria-invalid", errors.length > 0);
  }
}

if (!customElements.get("hpc-env-editor")) customElements.define("hpc-env-editor", HpcEnvEditor);
