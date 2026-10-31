import { el, action, api } from "../core.js";

// ------------------------------------------------------------------ <hpc-log>

export class HpcLog extends HTMLElement {
  // src: an API path; field: the list of lines in its answer ("lines"); every: seconds between refreshes (0: once);
  // empty: what to show when there is nothing yet. Emits "update" with the whole answer after each fetch.
  static observedAttributes = ["src", "every"];

  constructor() {
    super();
    this._pre = document.createElement("pre");
    this._pre.className = "log";
    this._pre.textContent = "…";
    this._copy = action("Copy last block", async () => {
      const src = this.getAttribute("src");
      const text = api(`${src}${src.includes("?") ? "&" : "?"}latest=1`).then(data => {
        if (typeof data.copy_text !== "string") throw new Error("Update the console backend to copy the last log block.");
        if (!data.copy_text) throw new Error("There is no log output to copy yet.");
        return data.copy_text;
      });
      try {
        if (typeof ClipboardItem === "function" && navigator.clipboard.write) {
          // Start the clipboard write during the click, before fetching, to preserve Safari's user gesture.
          const content = text.then(value => new Blob([value], { type: "text/plain" }));
          content.catch(() => {});  // A browser may reject the write before consuming this promise.
          await navigator.clipboard.write([new ClipboardItem({
            "text/plain": content,
          })]);
        } else {
          await navigator.clipboard.writeText(await text);
        }
      } catch (error) {
        await text;  // Prefer the backend's useful error if fetching the block failed.
        throw error;
      }
      return "Copied.";
    }, { result: true });
    this._controls = el("div", { class: "actions log-actions" }, this._copy);
    this._stick = true;
    this._pre.addEventListener("scroll", () => {
      const p = this._pre;
      this._stick = p.scrollHeight - p.scrollTop - p.clientHeight < 24;   // follow only while at the bottom
    });
  }

  connectedCallback() {
    if (!this._pre.isConnected) this.append(this._controls, this._pre);
    this.refresh();
    this._schedule();
  }

  disconnectedCallback() { clearTimeout(this._timer); }

  attributeChangedCallback(name, old, value) {
    if (!this.isConnected || old === value) return;
    if (name === "src") this.refresh();
    this._schedule();
  }

  _schedule() {
    clearTimeout(this._timer);
    const every = Number(this.getAttribute("every") || 0);
    if (every > 0) {
      this._timer = setTimeout(async () => {
        await this.refresh();
        this._schedule();
      }, every * 1000);
    }
  }

  async refresh() {
    const src = this.getAttribute("src");
    if (!src || !this.isConnected) return;
    let data;
    try {
      data = await api(src);
    } catch (err) {
      this._pre.textContent = err.message || String(err);
      this._pre.classList.add("error");
      return;
    }
    this._pre.classList.remove("error");
    const lines = data[this.getAttribute("field") || "lines"] || [];
    this._pre.textContent = lines.join("\n") || this.getAttribute("empty") || "(no output yet)";
    if (this._stick) this._pre.scrollTop = this._pre.scrollHeight;
    this.dispatchEvent(new CustomEvent("update", { detail: data }));
  }

  /** Stop refreshing (e.g. once an operation has finished). */
  stop() { this.removeAttribute("every"); }
}

if (!customElements.get("hpc-log")) customElements.define("hpc-log", HpcLog);
