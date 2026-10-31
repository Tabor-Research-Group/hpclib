
// ------------------------------------------------------------------ <hpc-state>

// every state word the API uses, by how it should look
const TONES = {
  up: ["up", "connected", "running", "succeeded", "logged in", "completed", "ready"],
  starting: ["starting", "queued", "password_sent", "push_sent", "waiting", "behind", "pending", "configuring",
             "completing", "suspended", "stale", "busy"],
  down: ["down", "none", "stopped", "expired", "not set up", "cancelled", "not prepared", "tool missing"],
  error: ["error", "failed", "timeout", "out_of_memory", "node_fail", "boot_fail", "deadline"],
};
const TONE_OF = Object.fromEntries(Object.entries(TONES).flatMap(([tone, words]) => words.map((w) => [w, tone])));

export class HpcState extends HTMLElement {
  // state: the API's word (up, queued, failed, ...); label: the text to show (default: the state); tone: override
  static observedAttributes = ["state", "label", "tone"];

  connectedCallback() { this._render(); }
  attributeChangedCallback() { this._render(); }

  _render() {
    const state = this.getAttribute("state") || "";
    this.textContent = this.getAttribute("label") ?? state;
    this.className = `state ${this.getAttribute("tone") || TONE_OF[state] || "down"}`;
  }
}

if (!customElements.get("hpc-state")) customElements.define("hpc-state", HpcState);
