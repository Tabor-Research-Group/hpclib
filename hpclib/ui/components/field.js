
// ------------------------------------------------------------------ <hpc-field>

export class HpcField extends HTMLElement {
  // label, hint: text around its one child control, wrapped in a real <label> so clicking the text focuses it
  static observedAttributes = ["label", "hint"];

  connectedCallback() {
    if (!this._label) {
      const control = this.firstElementChild;
      this._label = document.createElement("label");
      this._label.className = "field";
      this._text = document.createElement("span");
      this._hint = document.createElement("small");
      this._hint.className = "muted";
      this._label.append(this._text);
      if (control) this._label.append(control);
      this._label.append(this._hint);
      this.replaceChildren(this._label);
    }
    this._render();
  }

  attributeChangedCallback() { if (this._label) this._render(); }

  _render() {
    this._text.textContent = this.getAttribute("label") || "";
    this._hint.textContent = this.getAttribute("hint") || "";
    this._hint.hidden = !this._hint.textContent;
  }
}

if (!customElements.get("hpc-field")) customElements.define("hpc-field", HpcField);
