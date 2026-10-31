// The pieces every hpclib page shares: el() for building DOM, action() for a button that runs an async function,
// css() for the stylesheets custom elements adopt, and configure({ api }), which gives elements that fetch (hpc-log)
// the page's API function. Nothing here knows which page it is on: the console passes its own api, the Workspace
// its own.

let fetcher = async () => { throw new Error("hpclib-ui: call configure({ api }) first"); };

export function configure({ api }) {
  fetcher = api;
}

/** The API function the page gave configure(). */
export const api = (...args) => fetcher(...args);

/** el("tag", {attr: value, onclick: fn}, ...children): a DOM element. */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

/** An <hpc-action-button> running `fn`. */
export function action(label, fn, attrs = {}) {
  const b = el("hpc-action-button", { label, ...attrs });
  b.action = fn;
  return b;
}

export const css = (text) => {
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(text);
  return sheet;
};

/** Whether the page is dark now: the menu's switch (data-theme on <html>), else the system's. */
export const isDark = () => {
  const chosen = document.documentElement.dataset.theme;
  return chosen ? chosen === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
};

/** Calls fn() whenever the page's theme changes (the switch, or the system's setting). */
export function onThemeChange(fn) {
  new MutationObserver(fn).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", fn);
}

/** Remembers a small value in this browser (a split's size, a tab's choice); never throws, since storage can be off. */
export const remember = {
  get(key, fallback = null) {
    try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage off: not remembered */ }
  },
};

// Close controls share their shape and typography; each component sets its position.
export const CLOSE_CSS = css(`
  button.close {
    position: absolute;
    width: var(--close-size, 24px);
    height: var(--close-size, 24px);
    padding: 0;
    border: none;
    border-radius: var(--radius-control, 6px);
    background: none;
    color: var(--muted);
    font: var(--close-font-size, 18px)/var(--close-size, 24px) var(--font-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif);
    cursor: pointer;
  }
  button.close:hover { background: var(--panel); color: var(--fg); }
  button.close[hidden] { display: none; }
`);
