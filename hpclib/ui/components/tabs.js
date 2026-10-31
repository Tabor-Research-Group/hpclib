// ------------------------------------------------------------------ <hpc-tabs>
//
// A row of tabs with the keyboard behaviour of the ARIA tabs pattern: one tab is in the tab order, arrows move
// between tabs (and select them), Home and End jump, Delete closes. Light DOM, so the page's own CSS variables and
// the rules in base.css apply, and the panels the tabs control can be anywhere in the page.
//
//   const tabs = el("hpc-tabs", { label: "Open files" });
//   tabs.tabs = [{ id: "a", label: "train.py", title: "/home/me/train.py", dirty: true, closable: true,
//                  controls: "panel-a", badge: "2" }];
//   tabs.active = "a";
//   tabs.addEventListener("tab-select", (e) => show(e.detail.id));
//   tabs.addEventListener("tab-close", (e) => close(e.detail.id));     // the page decides; nothing closes by itself
//
// A tab: id, label, title (tooltip), dirty (a dot), closable, controls (the id of its panel), badge (small text).

export class HpcTabs extends HTMLElement {
  static observedAttributes = ["label"];

  constructor() {
    super();
    this._tabs = [];
    this._active = null;
    this._list = document.createElement("div");
    this._list.className = "hpc-tablist";
    this._list.setAttribute("role", "tablist");
    this._list.addEventListener("keydown", (e) => this._key(e));
    this._list.addEventListener("click", (e) => this._click(e));
    this._list.addEventListener("auxclick", (e) => this._aux(e));
  }

  connectedCallback() {
    if (!this._list.isConnected) this.append(this._list);
    this._list.setAttribute("aria-label", this.getAttribute("label") || "");
    this._render();
  }

  attributeChangedCallback() { this._list.setAttribute("aria-label", this.getAttribute("label") || ""); }

  get tabs() { return this._tabs; }
  set tabs(list) {
    this._tabs = list.slice();
    if (!this._tabs.some((t) => t.id === this._active)) this._active = this._tabs.length ? this._tabs[this._tabs.length - 1].id : null;
    this._render();
  }

  get active() { return this._active; }
  set active(id) {
    this._active = id;
    this._render();
  }

  /** Select a tab and tell the page (the page's handler usually shows the tab's panel). */
  select(id, { focus = false } = {}) {
    if (!this._tabs.some((t) => t.id === id)) return;
    const changed = id !== this._active;
    this._active = id;
    this._render();
    if (focus) this._button(id)?.focus();
    if (changed) this.dispatchEvent(new CustomEvent("tab-select", { detail: { id }, bubbles: true }));
  }

  _button(id) { return [...this._list.querySelectorAll('[role="tab"]')].find((b) => b.dataset.id === id); }

  _render() {
    const focused = this.contains(document.activeElement) ? document.activeElement.dataset.id : null;
    this._list.replaceChildren(...this._tabs.map((t) => {
      const on = t.id === this._active;
      const tab = document.createElement("div");
      tab.className = `hpc-tab${on ? " active" : ""}${t.dirty ? " dirty" : ""}`;
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      tab.dataset.id = t.id;
      if (t.title) tab.title = t.title;
      if (t.controls) tab.setAttribute("aria-controls", t.controls);
      const label = document.createElement("span");
      label.className = "label";
      label.textContent = t.label;
      tab.append(label);
      if (t.dirty) {
        const dot = document.createElement("span");
        dot.className = "dirty-dot";
        dot.setAttribute("role", "img");
        dot.setAttribute("aria-label", "unsaved changes");
        dot.textContent = "●";
        tab.append(dot);
      }
      if (t.badge) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = t.badge;
        tab.append(badge);
      }
      if (t.closable !== false) {
        const close = document.createElement("button");
        close.type = "button";
        close.className = "tab-close";
        close.tabIndex = -1;                       // Delete closes from the keyboard; the button is for the pointer
        close.setAttribute("aria-label", `Close ${t.label}`);
        close.textContent = "×";
        tab.append(close);
      }
      return tab;
    }));
    if (focused) this._button(focused)?.focus();
    const on = this._button(this._active);
    if (on && on.scrollIntoView) on.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  _click(e) {
    const tab = e.target.closest('[role="tab"]');
    if (!tab) return;
    if (e.target.closest("button.tab-close")) {
      this.dispatchEvent(new CustomEvent("tab-close", { detail: { id: tab.dataset.id }, bubbles: true }));
      return;
    }
    this.select(tab.dataset.id);
  }

  _aux(e) {
    const tab = e.target.closest('[role="tab"]');
    if (tab && e.button === 1) this.dispatchEvent(new CustomEvent("tab-close", { detail: { id: tab.dataset.id }, bubbles: true }));
  }

  _key(e) {
    const tab = e.target.closest('[role="tab"]');
    if (!tab || !this._tabs.length) return;
    const ids = this._tabs.map((t) => t.id);
    const at = ids.indexOf(tab.dataset.id);
    const to = { ArrowRight: (at + 1) % ids.length, ArrowLeft: (at - 1 + ids.length) % ids.length, Home: 0,
                 End: ids.length - 1 }[e.key];
    if (to !== undefined) {
      e.preventDefault();
      this.select(ids[to], { focus: true });
    } else if ((e.key === "Delete" || e.key === "Backspace") && this._tabs[at].closable !== false) {
      e.preventDefault();
      this.dispatchEvent(new CustomEvent("tab-close", { detail: { id: tab.dataset.id }, bubbles: true }));
    }
  }
}

if (!customElements.get("hpc-tabs")) customElements.define("hpc-tabs", HpcTabs);
