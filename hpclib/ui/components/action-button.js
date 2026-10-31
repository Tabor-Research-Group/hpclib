import { el } from "../core.js";

// ------------------------------------------------------------------ <hpc-action-button>

export class HpcActionButton extends HTMLElement {
  // label, primary, disabled: the button's. .action = async () => ...: runs on click, with the button off until it
  // finishes; an error shows beside the button (× clears it), so it stays next to what failed. result: also show
  // the string the action returns ("Saved."). Fires "done" with the action's value.
  static observedAttributes = ["label", "primary", "disabled"];

  constructor() {
    super();
    this.button = el("button", { type: "button" });
    this.out = el("span", { class: "action-out" });
    this.button.addEventListener("click", () => this.run());
    this._running = false;
  }

  connectedCallback() {
    if (!this.button.isConnected) this.append(this.button, this.out);
    this._render();
  }
  attributeChangedCallback() { this._render(); }

  get disabled() { return this.hasAttribute("disabled"); }
  set disabled(v) { this.toggleAttribute("disabled", !!v); }

  _render() {
    this.button.textContent = this.getAttribute("label") || "";
    this.button.className = this.hasAttribute("primary") ? "primary" : "";
    this.button.disabled = this._running || this.disabled;
  }

  async run() {
    if (this._running || !this.action) return;
    this._running = true;
    this._render();
    this.clear();
    try {
      const value = await this.action();
      if (this.hasAttribute("result") && value) this.show(String(value), "saved");
      this.dispatchEvent(new CustomEvent("done", { detail: value }));
    } catch (err) {
      this.show(err.message || String(err), "error");
    } finally {
      this._running = false;
      this._render();
    }
  }

  show(text, kind) {
    const x = el("button", { type: "button", class: "x", title: "Dismiss", "aria-label": "Dismiss" }, "×");
    x.addEventListener("click", () => this.clear());
    this.out.replaceChildren(el("span", { class: kind }, text), x);
  }
  clear() { this.out.replaceChildren(); }
}

if (!customElements.get("hpc-action-button")) customElements.define("hpc-action-button", HpcActionButton);
