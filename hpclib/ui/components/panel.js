import { css, CLOSE_CSS } from "../core.js";

// ------------------------------------------------------------------ <hpc-panel>

const PANEL_CSS = css(`
  :host { display: block; min-width: 0; margin: var(--space-xs, 4px) 0 var(--space-lg, 12px); }
  :host([hidden]) { display: none; }
  section {
    position: relative;
    border: 1px solid var(--line);
    border-radius: var(--radius-panel, 8px);
    padding: var(--panel-padding, 10px 14px 12px);
    box-sizing: border-box;
    min-width: 0;
    max-width: 100%;
    background: var(--bg);
  }
  header { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; min-height: var(--close-size, 24px); padding-right: calc(var(--close-size, 24px) + var(--space-xs, 4px)); }
  header:not(.has-content) { display: none; }
  .heading { font-weight: var(--font-weight-semibold, 600); }
  .heading, .status { min-width: 0; overflow-wrap: anywhere; }
  .status { color: var(--muted); }
  .status.error { color: var(--bad); }
  .status.ok { color: var(--good); }
  .body { min-width: 0; max-width: 100%; margin-top: var(--space-md, 8px); }
  header:not(.has-content) + .body { margin-top: 0; }
  button.close { top: var(--space-sm, 6px); right: var(--space-md, 8px); }
  ::slotted([slot="actions"]) { margin-left: 2px; }
`);

export class HpcPanel extends HTMLElement {
  static observedAttributes = ["heading", "status", "tone", "dismissible"];

  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    root.adoptedStyleSheets = [CLOSE_CSS, PANEL_CSS];
    root.innerHTML = `<section part="panel">
        <header part="header"><span class="heading" part="heading"></span><span class="status" part="status"></span>
          <slot name="actions"></slot></header>
        <div class="body" part="body"><slot></slot></div>
        <button class="close" part="close" type="button" aria-label="Dismiss" title="Dismiss" hidden>×</button>
      </section>`;
    this._parts = {
      header: root.querySelector("header"),
      heading: root.querySelector(".heading"),
      status: root.querySelector(".status"),
      close: root.querySelector(".close"),
    };
    this._parts.close.addEventListener("click", () => this.dismiss());
    root.querySelector('slot[name="actions"]').addEventListener("slotchange", () => this._render());
  }

  connectedCallback() { this._render(); }
  attributeChangedCallback() { this._render(); }

  _render() {
    const p = this._parts;
    p.heading.textContent = this.getAttribute("heading") || "";
    p.status.textContent = this.getAttribute("status") || "";
    p.status.className = `status ${this.getAttribute("tone") || ""}`;
    p.close.hidden = !this.hasAttribute("dismissible");
    const hasActions = !!this.querySelector(':scope > [slot="actions"]');
    p.header.classList.toggle("has-content", !!(p.heading.textContent || p.status.textContent || hasActions));
  }

  /** Hides the panel, unless a "dismiss" listener calls preventDefault(). */
  dismiss() {
    const event = new CustomEvent("dismiss", { cancelable: true, bubbles: true });
    if (this.dispatchEvent(event)) this.hidden = true;
  }
}

if (!customElements.get("hpc-panel")) customElements.define("hpc-panel", HpcPanel);
