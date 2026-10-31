// ------------------------------------------------------------------ <hpc-toast>
//
// A short message that fades after a few seconds, in one live region so screen readers hear it. Errors stay a
// little longer and are announced as alerts. A click dismisses.
//
//   import { toast } from "hpclib-ui/index.js";
//   toast("Saved.");                                  toast(err.message, { tone: "error" });

export class HpcToasts extends HTMLElement {
  // attributes are set here and not in the constructor: document.createElement refuses an element that has some
  connectedCallback() {
    this.setAttribute("role", "status");
    this.setAttribute("aria-live", "polite");
  }

  add(text, { tone = "info", ms = null } = {}) {
    const item = document.createElement("div");
    item.className = `toast ${tone}`;
    item.textContent = text;
    if (tone === "error") item.setAttribute("role", "alert");
    const remove = () => item.remove();
    item.addEventListener("click", remove);
    this.append(item);
    while (this.children.length > 4) this.firstChild.remove();
    setTimeout(remove, ms ?? (tone === "error" ? 7000 : 3500));
    return item;
  }
}

if (!customElements.get("hpc-toasts")) customElements.define("hpc-toasts", HpcToasts);

let region = null;
/** Show a message: tone is info, ok or error. */
export function toast(text, options = {}) {
  if (!region || !region.isConnected) {
    region = document.createElement("hpc-toasts");
    document.body.append(region);
  }
  return region.add(String(text), options);
}
