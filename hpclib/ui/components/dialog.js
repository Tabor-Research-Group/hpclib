import { el, css, CLOSE_CSS } from "../core.js";

// ------------------------------------------------------------------ <hpc-dialog>

const DIALOG_CSS = css(`
  dialog {
    position: relative;
    width: min(var(--dialog-max-width, 440px), calc(100vw - var(--dialog-viewport-gap, 32px)));
    box-sizing: border-box;
    border: 1px solid var(--line);
    border-radius: var(--radius-dialog, 10px);
    padding: var(--dialog-padding, 16px 18px);
    background: var(--bg);
    color: var(--fg);
  }
  dialog::backdrop { background: var(--dialog-backdrop, rgb(0 0 0 / 0.35)); }
  h3 { font-size: var(--font-size-subheading, 15px); margin: 0 calc(var(--close-size, 24px) + var(--space-xs, 4px)) 10px 0; }
  h3:empty { display: none; }
  button.close { top: 10px; right: 10px; }
`);

export class HpcDialog extends HTMLElement {
  // heading: its title; transient: remove the element once closed. Escape and × close it.
  static observedAttributes = ["heading"];

  constructor() {
    super();
    const root = this.attachShadow({ mode: "open" });
    root.adoptedStyleSheets = [CLOSE_CSS, DIALOG_CSS];
    root.innerHTML = `<dialog part="dialog"><button class="close" part="close" type="button" aria-label="Close" title="Close">×</button>
      <h3 part="heading"></h3><slot></slot></dialog>`;
    this._dialog = root.querySelector("dialog");
    this._heading = root.querySelector("h3");
    root.querySelector(".close").addEventListener("click", () => this.close());
    this._dialog.addEventListener("close", () => {
      this.dispatchEvent(new CustomEvent("close"));
      if (this.hasAttribute("transient")) this.remove();
    });
  }

  connectedCallback() { this._heading.textContent = this.getAttribute("heading") || ""; }
  attributeChangedCallback() { this._heading.textContent = this.getAttribute("heading") || ""; }

  get open() { return this._dialog.open; }

  showModal() {
    if (!this.isConnected) document.body.append(this);
    if (!this._dialog.open) this._dialog.showModal();
    const first = this.querySelector("[autofocus], input:not([type=hidden]), textarea, select, button");
    if (first) first.focus();
  }

  close() {
    if (this._dialog.open) this._dialog.close();
  }
}

/** A yes/no question in an <hpc-dialog>; resolves true for `ok`, false for Cancel, Escape or ×. */
export function confirmDialog(heading, text, ok = "OK") {
  return new Promise((resolve) => {
    const yes = el("button", { class: "primary", type: "button" }, ok);
    const no = el("button", { type: "button" }, "Cancel");
    const dialog = el("hpc-dialog", { heading, transient: true }, el("p", {}, text), el("div", { class: "actions" }, yes, no));
    let answer = false;
    yes.addEventListener("click", () => { answer = true; dialog.close(); });
    no.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => resolve(answer));
    dialog.showModal();
    no.focus();
  });
}

/** A question with a text answer in an <hpc-dialog>; resolves with the text, or null for Cancel, Escape or ×. */
export function promptDialog(heading, text, value = "", ok = "OK") {
  return new Promise((resolve) => {
    const input = el("input", { spellcheck: "false", autocomplete: "off", "aria-label": text, class: "prompt-input" });
    input.value = value;
    const yes = el("button", { class: "primary", type: "submit" }, ok);
    const no = el("button", { type: "button" }, "Cancel");
    const form = el("form", {}, el("p", {}, text), input, el("div", { class: "actions" }, yes, no));
    const dialog = el("hpc-dialog", { heading, transient: true }, form);
    let answer = null;
    form.addEventListener("submit", (e) => { e.preventDefault(); answer = input.value; dialog.close(); });
    no.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => resolve(answer));
    dialog.showModal();
    input.focus();
    input.select();
  });
}

if (!customElements.get("hpc-dialog")) customElements.define("hpc-dialog", HpcDialog);
