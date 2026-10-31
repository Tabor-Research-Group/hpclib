// ------------------------------------------------------------------ <hpc-split>
//
// Two panes with a draggable divider. Light DOM: the element's two children are the panes, and it adds the divider
// between them. One pane has a size you set (and the divider changes); the other takes what is left.
//
//   <hpc-split direction="row" fixed="first" size="340" min="200" min-other="300" persist="ws.side">
//     <aside>…</aside> <section>…</section>
//   </hpc-split>
//
//   direction   row (side by side, the default) or column (stacked)
//   fixed       which pane has the size: first (the default) or last (a dock at the bottom)
//   size        its starting size in pixels; persist: a key under which this browser remembers the size
//   min, max    limits for that pane; min-other: the least the other pane keeps
//
// The divider is a separator you can use with the keyboard: arrows move it by 16 px (Shift: 64), Home and End go
// to the limits, and a double click goes back to `size`. A "split-resize" event follows each change, so editors
// and terminals inside can refit.

import { remember } from "../core.js";

export class HpcSplit extends HTMLElement {
  static observedAttributes = ["size", "direction", "fixed"];

  constructor() {
    super();
    this._divider = document.createElement("div");
    this._divider.className = "hpc-split-divider";
    this._divider.setAttribute("role", "separator");
    this._divider.tabIndex = 0;
    this._divider.addEventListener("pointerdown", (e) => this._drag(e));
    this._divider.addEventListener("keydown", (e) => this._key(e));
    this._divider.addEventListener("dblclick", () => this.resize(this._default()));
    this._size = null;
  }

  connectedCallback() {
    if (!this._divider.isConnected) {
      const panes = [...this.children];
      if (panes.length >= 2) panes[0].after(this._divider);
    }
    this._size = this._clamp(remember.get(this._key_(), this._default()));
    this._apply();
  }

  attributeChangedCallback() { if (this.isConnected) this._apply(); }

  get _vertical() { return this.getAttribute("direction") === "column"; }       // panes stacked: divider is horizontal
  get _last() { return this.getAttribute("fixed") === "last"; }
  _key_() { return this.hasAttribute("persist") ? `hpc-split:${this.getAttribute("persist")}` : null; }
  _default() { return Number(this.getAttribute("size")) || 300; }
  _num(name, fallback) { const v = Number(this.getAttribute(name)); return v > 0 ? v : fallback; }

  _clamp(px) {
    const room = this._vertical ? this.clientHeight : this.clientWidth;
    const min = this._num("min", 120);
    const max = Math.min(this._num("max", Infinity), room ? room - this._num("min-other", 160) : Infinity);
    return Math.round(Math.max(min, Math.min(Number(px) || min, Math.max(min, max))));
  }

  get size() { return this._size; }

  /** Set the fixed pane's size (pixels), remember it and tell the page. */
  resize(px) {
    this._size = this._clamp(px);
    this._apply();
    const key = this._key_();
    if (key) remember.set(key, this._size);
    this.dispatchEvent(new CustomEvent("split-resize", { detail: { size: this._size }, bubbles: true }));
  }

  _apply() {
    const vertical = this._vertical;
    this.style.setProperty("--split-size", `${this._size ?? this._default()}px`);
    this.dataset.direction = vertical ? "column" : "row";
    this.dataset.fixed = this._last ? "last" : "first";
    const d = this._divider;
    d.setAttribute("aria-orientation", vertical ? "horizontal" : "vertical");
    d.setAttribute("aria-valuenow", String(this._size ?? this._default()));
    d.setAttribute("aria-valuemin", String(this._num("min", 120)));
    d.setAttribute("aria-label", this.getAttribute("label") || "Resize");
  }

  _drag(e) {
    if (e.button !== 0) return;
    e.preventDefault();
    const d = this._divider;
    d.setPointerCapture(e.pointerId);
    const box = this.getBoundingClientRect();
    const move = (ev) => {
      const at = this._vertical ? ev.clientY - box.top : ev.clientX - box.left;
      const total = this._vertical ? box.height : box.width;
      this.resize(this._last ? total - at : at);
    };
    const up = () => { d.removeEventListener("pointermove", move); d.removeEventListener("pointerup", up); };
    d.addEventListener("pointermove", move);
    d.addEventListener("pointerup", up);
  }

  _key(e) {
    const step = e.shiftKey ? 64 : 16;
    // arrows move the divider; the fixed pane grows when the divider moves away from it
    const growsWith = this._vertical ? { ArrowDown: 1, ArrowUp: -1 } : { ArrowRight: 1, ArrowLeft: -1 };
    const sign = growsWith[e.key];
    if (sign) {
      e.preventDefault();
      this.resize(this._size + (this._last ? -sign : sign) * step);
    } else if (e.key === "Home" || e.key === "End") {
      e.preventDefault();
      this.resize(e.key === "Home" ? this._num("min", 120) : 1e6);
    }
  }
}

if (!customElements.get("hpc-split")) customElements.define("hpc-split", HpcSplit);
