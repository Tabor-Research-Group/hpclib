// ------------------------------------------------------------------ <hpc-menu>
//
// A menu that opens at a button or at the pointer, on the browser's own popover: it sits above everything and needs
// no z-index. It is a manual popover that closes itself (Escape, a press elsewhere, the window losing focus), because
// the automatic kind closes on the mouse-up of the very right-click that opened it where contextmenu fires on
// mouse-down (Linux, macOS). Items are plain data and the menu is built when it opens.
//
//   import { showMenu } from "hpclib-ui/index.js";
//   showMenu([{ label: "Open", run: () => open(path) },
//             { label: "Delete…", run: remove, tone: "danger" },
//             { separator: true },
//             { label: "Download", disabled: true }], event.currentTarget);     // an element, or a pointer event
//
// Arrow keys, Home and End move through the items, Enter or Space runs one, a letter jumps to the next item that
// starts with it, and focus goes back to where it was when the menu closes.

export class HpcMenu extends HTMLElement {
  constructor() {
    super();
    this.addEventListener("keydown", (e) => this._key(e));
    this.addEventListener("toggle", (e) => { if (e.newState === "closed") this._closed(); });
  }

  // attributes are set here and not in the constructor: document.createElement refuses an element that has some
  connectedCallback() {
    this.setAttribute("popover", "manual");
    this.setAttribute("role", "menu");
  }

  /** Open with `items` at `at`: an element (below it) or a pointer event (at the pointer). */
  show(items, at) {
    this._from = document.activeElement;
    this.replaceChildren(...items.map((item) => {
      if (item.separator) {
        const hr = document.createElement("div");
        hr.setAttribute("role", "separator");
        hr.className = "menu-separator";
        return hr;
      }
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "menuitem");
      b.className = `menu-item${item.tone ? ` ${item.tone}` : ""}`;
      b.textContent = item.label;
      b.disabled = !!item.disabled;
      b.tabIndex = -1;
      b.addEventListener("click", () => {
        this.hidePopover();
        if (item.run) item.run();
      });
      return b;
    }));
    if (!this.isConnected) document.body.append(this);
    this.showPopover();
    this._outside = (e) => { if (!this.contains(e.target)) this.hidePopover(); };
    document.addEventListener("pointerdown", this._outside, true);
    addEventListener("blur", this._away);
    addEventListener("resize", this._away);
    const box = at && at.getBoundingClientRect ? at.getBoundingClientRect()
      : { left: at.clientX, right: at.clientX, bottom: at.clientY, top: at.clientY };
    const w = this.offsetWidth, h = this.offsetHeight;
    const left = Math.max(4, Math.min(box.left, innerWidth - w - 4));
    const below = box.bottom + h + 4 <= innerHeight;
    this.style.left = `${left}px`;
    this.style.top = `${Math.max(4, below ? box.bottom : box.top - h)}px`;
    this._items()[0]?.focus();
  }

  _items() { return [...this.querySelectorAll('[role="menuitem"]:not(:disabled)')]; }

  _away = () => { if (this.matches(":popover-open")) this.hidePopover(); };

  _closed() {
    document.removeEventListener("pointerdown", this._outside, true);
    removeEventListener("blur", this._away);
    removeEventListener("resize", this._away);
    const from = this._from;
    this._from = null;
    if (from && from.isConnected && from.focus) from.focus();
  }

  _key(e) {
    const items = this._items();
    if (!items.length) { if (e.key === "Escape") this.hidePopover(); return; }
    const at = items.indexOf(document.activeElement);
    const to = { ArrowDown: (at + 1) % items.length, ArrowUp: (at - 1 + items.length) % items.length, Home: 0,
                 End: items.length - 1 }[e.key];
    if (to !== undefined) {
      e.preventDefault();
      items[to].focus();
    } else if (e.key.length === 1 && /\S/.test(e.key)) {
      const next = [...items.slice(at + 1), ...items.slice(0, at + 1)]
        .find((b) => b.textContent.toLowerCase().startsWith(e.key.toLowerCase()));
      if (next) next.focus();
    } else if (e.key === "Tab" || e.key === "Escape") {
      this.hidePopover();
    }
  }
}

if (!customElements.get("hpc-menu")) customElements.define("hpc-menu", HpcMenu);

let shared = null;
/** Open a menu (one at a time, shared). */
export function showMenu(items, at) {
  if (!shared || !shared.isConnected) shared = document.createElement("hpc-menu");
  shared.show(items.filter(Boolean), at);
}
