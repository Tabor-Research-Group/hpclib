// ------------------------------------------------------------------ <hpc-terminal>
//
// A terminal (xterm.js, pinned in libs.json) talking to a server over a WebSocket in the Workspace's terminal
// protocol: binary frames carry the shell's bytes both ways; text frames are JSON: {"type": "resize", cols, rows}
// to the server, and from it {"type": "session", id} first (the shell's id: it keeps running when the page leaves) and
// {"type": "exit", code}. The page decides how the WebSocket is opened, so the element holds no credentials:
//
//   const t = el("hpc-terminal");
//   t.connect = async ({ cols, rows, session }) => new WebSocket(await urlWithTicket(cols, rows, session));
//   parent.append(t);  t.start({ session });          // session: attach to a shell that is already running
//   t.type("ls\n")                                         // type into the shell
//   t.addEventListener("terminal-session", (e) => ...)    // {id}: the shell this terminal is showing
//   t.addEventListener("terminal-state", (e) => ...)      // {state}: connected | reconnecting | ended
//   t.addEventListener("terminal-end", (e) => ...)        // the shell exited ({code}), is gone, or can't be reached
//
// When the connection drops (a tunnel that dropped, a laptop that slept) and the server named a session, the element
// connects again, a little later each time, and the server replays what the shell printed meanwhile. connect() may
// throw an error with `gone = true` to say the shell no longer exists: the terminal then ends instead of retrying.
//
// Light DOM, since xterm's styles are page styles. It fits itself to its box whenever that changes size, and
// again when it becomes visible (a hidden terminal has no size).

import { loadLib } from "../libs.js";

const RETRIES = 8;                // reconnection attempts, one after another (1, 2, 4, 8, 15 … seconds), before giving up

export class HpcTerminal extends HTMLElement {
  constructor() {
    super();
    this.ended = false;
    this.connect = null;
    this.session = null;
    this._pending = [];
  }

  /** Open the terminal and connect; resolves when connected (or after saying why it couldn't). */
  async start({ session = null } = {}) {
    let lib;
    try { lib = await loadLib("xterm"); }
    catch (err) { return this._say(`The terminal (xterm.js) didn't load: ${err.message}. Check this browser's ` +
      "internet access, or set the Workspace's CDN to a mirror you can reach."); }
    const { Terminal, FitAddon, WebLinksAddon } = lib;
    const xt = new Terminal({ cursorBlink: true, scrollback: 5000, fontSize: 13, allowProposedApi: false,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", theme: { background: "#111111" } });
    const fit = FitAddon && FitAddon.FitAddon ? new FitAddon.FitAddon() : null;
    if (fit) xt.loadAddon(fit);
    if (WebLinksAddon && WebLinksAddon.WebLinksAddon) xt.loadAddon(new WebLinksAddon.WebLinksAddon());
    xt.open(this);
    this.xt = xt;
    this.session = session;
    this._fit = () => { if (fit && this.offsetWidth && this.offsetHeight) { try { fit.fit(); } catch { /* hidden */ } } };
    this._fit();
    new ResizeObserver(() => this._fit()).observe(this);
    const enc = new TextEncoder();
    xt.onData((d) => this._send(enc.encode(d)));
    xt.onBinary((d) => this._send(Uint8Array.from(d, (c) => c.charCodeAt(0))));
    xt.onResize(({ cols, rows }) => { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify({ type: "resize", cols, rows })); });
    await this._open(0);
    xt.focus();
  }

  /** Type text into the shell as the person would (queued when the shell isn't connected yet). */
  type(text) { this._send(new TextEncoder().encode(text)); }

  _send(bytes) {
    const ws = this.ws;
    if (ws && ws.readyState === 1) ws.send(bytes);
    else if (ws && ws.readyState === 0) this._pending.push(bytes);        // typed before the shell is there: sent once it is
  }

  _state(state) {
    this.setAttribute("state", state);
    this.dispatchEvent(new CustomEvent("terminal-state", { detail: { state }, bubbles: true }));
  }

  /** Connect (or connect again, the `attempt`th time): the server replays the shell's recent output on an attach. */
  async _open(attempt) {
    if (this.ended) return;
    let ws;
    try { ws = await this.connect({ cols: this.xt.cols, rows: this.xt.rows, session: this.session }); }
    catch (err) {
      if (err && err.gone) return this._finish("[that terminal has ended]");
      if (this.session && attempt < RETRIES) return this._retry(attempt);
      return this._finish(`[can't open the terminal: ${err.message}]`);
    }
    this.ws = ws;
    this._pending = [];
    ws.binaryType = "arraybuffer";
    let opened = false;
    ws.onopen = () => { opened = true; for (const b of this._pending.splice(0)) ws.send(b); };
    ws.onmessage = (m) => {
      if (typeof m.data === "string") {
        const msg = JSON.parse(m.data);
        if (msg.type === "session") {
          if (attempt || this.session) this.xt.reset();                    // the replay brings the screen back
          this.session = msg.id;
          this._state("connected");
          this.dispatchEvent(new CustomEvent("terminal-session", { detail: { id: msg.id }, bubbles: true }));
        } else if (msg.type === "exit") this._finish(`[the shell ended (exit ${msg.code})]`, msg.code);
      } else this.xt.write(new Uint8Array(m.data));
    };
    ws.onclose = () => {
      if (this.ended || ws !== this.ws) return;
      if (this.session) this._retry(opened ? 0 : attempt);
      else this._finish("[disconnected]");
    };
  }

  _retry(attempt) {
    if (attempt >= RETRIES) return this._finish("[disconnected: the connection didn't come back]");
    this._state("reconnecting");
    clearTimeout(this._timer);
    this._timer = setTimeout(() => this._open(attempt + 1), Math.min(1000 * 2 ** attempt, 15000));
  }

  _finish(text, code = null) {
    if (this.xt) this.xt.write(`\r\n${text}\r\n`); else this._say(text);
    this.ended = true;
    this._state("ended");
    this.dispatchEvent(new CustomEvent("terminal-end", { detail: { code }, bubbles: true }));
  }

  _say(text) {
    const p = document.createElement("p");
    p.className = "terminal-note";
    p.textContent = text;
    this.replaceChildren(p);
    this.ended = true;
    this.dispatchEvent(new CustomEvent("terminal-end", { detail: { code: null }, bubbles: true }));
  }

  focus() { if (this.xt) this.xt.focus(); }
  fit() { if (this._fit) this._fit(); }

  /** Close the connection and the terminal. */
  dispose() {
    this.ended = true;
    clearTimeout(this._timer);
    if (this.ws) this.ws.close();
    if (this.xt) this.xt.dispose();
  }
}

if (!customElements.get("hpc-terminal")) customElements.define("hpc-terminal", HpcTerminal);
