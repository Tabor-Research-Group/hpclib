// Packaged widgets: small dashboards on the clusters page (slot "home"), each cluster's Overview
// ("cluster.overview") or a tab of their own ("cluster.tab"). See docs/extending.md, "Console widgets".
//
// A widget is one HTML file from a package, shown in an iframe with sandbox="allow-scripts" and no
// allow-same-origin: its origin is its own, so it can't read this page, its storage (the session key) or call the
// console's API, and the Content-Security-Policy put at its top allows no network requests. It talks to this page
// only by postMessage, through the helper put in beside the CSP (SHIM):
//
//   widget → page   {hpclib: 1, type: "hello"}                       (the helper, when the frame loads)
//                   {hpclib: 1, type: "subscribe", topic}            a topic its manifest `reads` (or "theme")
//                   {hpclib: 1, type: "navigate", hash}              e.g. "#/c/grace/jobs"; never a #key= address
//                   {hpclib: 1, type: "resize", height}              its content's height, in pixels
//   page → widget   {hpclib: 1, type: "init", widget, context}       context: {cluster} on a cluster's page
//                   {hpclib: 1, type: "data", topic, value}          now, and whenever the topic is refreshed
//                   {hpclib: 1, type: "error", topic, message}
//
// The page fetches each topic once for every widget (the jobs topic runs squeue over each logged-in cluster's
// login, so widgets mustn't poll on their own), only while a widget on screen wants it.
import { el } from "./components.js";

let ctx = null;          // { api, preferences: () => preferences, errorBox }
let installed = [];      // GET /api/widgets
export function init(given) { ctx = given; }
export async function load() {
  try { installed = (await ctx.api("widgets")).widgets || []; } catch { installed = []; }
  return installed;
}
export const all = () => installed;

const enc = encodeURIComponent;
const CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; " +
            "font-src data:; media-src data: blob:; form-action 'none'; base-uri 'none'";
const SHIM = `(() => {
  const subs = {};
  let resolve;
  const ready = new Promise((r) => { resolve = r; });
  const send = (m) => parent.postMessage(Object.assign({ hpclib: 1 }, m), "*");
  const api = { widget: null, context: {},
    subscribe(topic, fn) { (subs[topic] = subs[topic] || []).push(fn); send({ type: "subscribe", topic }); return api; },
    navigate(hash) { send({ type: "navigate", hash: String(hash) }); },
    resize(height) { send({ type: "resize", height: Number(height) }); } };
  addEventListener("message", (e) => {
    const m = e.data;
    if (e.source !== parent || !m || m.hpclib !== 1) return;
    if (m.type === "init") { api.widget = m.widget; api.context = m.context || {}; resolve(api); }
    if (m.type === "data") for (const fn of subs[m.topic] || []) { try { fn(m.value); } catch (err) { console.error(err); } }
    if (m.type === "error") dispatchEvent(new CustomEvent("hpclib-error", { detail: m }));
  });
  window.hpclibWidget = () => ready;
  const size = () => send({ type: "resize", height: Math.ceil(document.documentElement.getBoundingClientRect().height) });
  addEventListener("load", () => { size(); new ResizeObserver(size).observe(document.documentElement); });
  send({ type: "hello" });
})();`;
const BASE_CSS = (scheme) => `html { color-scheme: ${scheme}; } body { margin: 0; font: 14px/1.45 -apple-system,
  BlinkMacSystemFont, "Segoe UI", sans-serif; background: transparent; color: CanvasText; }`;
// the page's theme now (the menu's switch, else the system's): a frame matching it stays transparent
const scheme = () => (document.documentElement.dataset.theme ||
  (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));

// The widget's page: its HTML after the CSP, the helper and base styles, so nothing of its runs before them
export function srcdoc(html, colorScheme = "light") {
  const body = String(html).replace(/^\uFEFF?\s*<!doctype[^>]*>/i, "");
  return `<!doctype html><meta http-equiv="Content-Security-Policy" content="${CSP}"><meta charset="utf-8">` +
         `<script>${SHIM}</script><style>${BASE_CSS(colorScheme)}</style>${body}`;
}

// ---------------------------------------------------------------- the topics

const pick = (o, keys) => Object.fromEntries(keys.filter((k) => o && k in o).map((k) => [k, o[k]]));
const loggedIn = (c) => (c.login || {}).state === "connected";

async function clustersTopic() {
  const { clusters } = await ctx.api("clusters");
  return clusters.map((c) => ({ ...pick(c, ["name", "host", "mcp_name", "set_up", "work_dirs"]),
    login: (c.login || {}).state || "none", tunnel: (c.tunnel || {}).state || "down",
    hpclib_version: (c.tunnel || {}).hpclib_version || null }));
}
async function jobsTopic() {
  const { clusters } = await ctx.api("clusters");
  const byCluster = {};
  await Promise.all(clusters.filter(loggedIn).map(async (c) => {
    try { byCluster[c.name] = await ctx.api(`clusters/${enc(c.name)}/jobs`); }
    catch (err) { byCluster[c.name] = { error: err.message }; }
  }));
  return { at: Date.now() / 1000, byCluster };
}
async function storageTopic() {
  const { clusters } = await ctx.api("clusters");
  const byCluster = {};
  await Promise.all(clusters.map(async (c) => {
    try { byCluster[c.name] = await ctx.api(`clusters/${enc(c.name)}/storage`); } catch { /* an older console */ }
  }));
  return { byCluster };
}
async function appsTopic() {
  const { apps } = await ctx.api("apps");
  const list = apps.filter((a) => a.id !== "agents");
  const byCluster = {};
  await Promise.all(list.map(async (a) => {
    try {
      const { sessions } = await ctx.api(`apps/${a.id}`);
      // the state only: never its URL, token or password
      for (const s of sessions) {
        (byCluster[s.cluster] = byCluster[s.cluster] || {})[a.id] = {
          ...pick(s, ["state", "status", "version", "port", "started_here", "shared"]),
          install: (s.install || {}).state || null };
      }
    } catch { /* that app's sessions aren't readable now */ }
  }));
  return { apps: list.map((a) => pick(a, ["id", "title", "kind", "package"])), byCluster };
}
let varNames = null;
async function themeTopic() {
  if (!varNames) {
    try {
      const text = await (await fetch("default-styles.css", { cache: "no-store" })).text();
      varNames = [...new Set([...text.matchAll(/(--[a-z0-9-]+)\s*:/g)].map((m) => m[1]))];
    } catch { varNames = []; }
  }
  const style = getComputedStyle(document.documentElement);
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  return { dark, vars: Object.fromEntries(varNames.map((n) => [n, style.getPropertyValue(n).trim()])) };
}

const TOPICS = {
  clusters: { fetch: clustersTopic, every: 30000 },
  jobs: { fetch: jobsTopic, every: 60000 },
  storage: { fetch: storageTopic, every: 120000 },
  apps: { fetch: appsTopic, every: 30000 },
  theme: { fetch: themeTopic, every: Infinity },
};
const cache = {};        // topic -> {value, at, error}
const pending = {};      // topic -> promise
const frames = new Set();   // live frame records: {iframe, widget, context, topics: Set}

async function refresh(topic) {
  if (pending[topic]) return pending[topic];
  pending[topic] = (async () => {
    try { cache[topic] = { value: await TOPICS[topic].fetch(), at: Date.now() }; }
    catch (err) { cache[topic] = { error: err.message, at: Date.now() }; }
    finally { delete pending[topic]; }
    push(topic);
  })();
  return pending[topic];
}

function post(f, message) {
  try { f.iframe.contentWindow?.postMessage({ hpclib: 1, ...message }, "*"); } catch { /* gone */ }
}

function push(topic) {
  const c = cache[topic];
  for (const f of live()) {
    if (!f.topics.has(topic)) continue;
    post(f, c.error ? { type: "error", topic, message: c.error } : { type: "data", topic, value: c.value });
  }
}

function live() {
  for (const f of [...frames]) if (!f.iframe.isConnected) frames.delete(f);
  return [...frames];
}

// once a few seconds: refresh what a widget on screen wants and is due
setInterval(() => {
  const wanted = new Set(live().flatMap((f) => [...f.topics]));
  for (const topic of wanted) {
    const c = cache[topic];
    if (!c || Date.now() - c.at >= TOPICS[topic].every) refresh(topic);
  }
}, 5000);
// a theme change: each frame is drawn again in it (a frame in another scheme than the page's isn't transparent)
const redraw = () => {
  for (const f of live()) {
    if (f.html == null) continue;
    f.redrawing = true;
    f.topics.clear();
    f.iframe.style.colorScheme = scheme();
    f.iframe.srcdoc = srcdoc(f.html, scheme());
  }
};
document.addEventListener("hpc-theme", () => { delete cache.theme; redraw(); });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { delete cache.theme; redraw(); });
document.getElementById("interface-css")?.addEventListener("load", () => { if (cache.theme) refresh("theme"); });

const NAV_RE = /^#\/[A-Za-z0-9_\-./%?=&@:~]*$/;

window.addEventListener("message", (e) => {
  const m = e.data;
  if (!m || m.hpclib !== 1) return;
  const f = live().find((x) => x.iframe.contentWindow === e.source);
  if (!f) return;
  if (m.type === "hello") {
    post(f, { type: "init", context: f.context,
              widget: pick(f.widget, ["key", "id", "title", "slot", "reads", "package", "version"]) });
  } else if (m.type === "subscribe") {
    const topic = String(m.topic);
    if (!(topic in TOPICS) || (topic !== "theme" && !f.widget.reads.includes(topic))) {
      post(f, { type: "error", topic, message: `${topic}: not a topic this widget reads (its manifest's reads)` });
      return;
    }
    f.topics.add(topic);
    const c = cache[topic];
    if (c && Date.now() - c.at < TOPICS[topic].every) {
      post(f, c.error ? { type: "error", topic, message: c.error } : { type: "data", topic, value: c.value });
    } else refresh(topic);
  } else if (m.type === "navigate") {
    const hash = String(m.hash || "");
    if (NAV_RE.test(hash) && !/key=/i.test(hash)) location.hash = hash;
  } else if (m.type === "resize") {
    const h = Math.round(Number(m.height));
    if (h > 0) f.iframe.style.height = `${Math.min(Math.max(h, 40), 1600)}px`;
  }
});

// ---------------------------------------------------------------- frames, for the pages

const keyOf = (w) => w.key;

/** The visible widgets for a slot, in the order chosen on Interface Settings. */
export function forSlot(slot) {
  const prefs = (ctx.preferences() || {}).widgets || { hidden: [], order: [] };
  const order = prefs.order || [];
  const rank = (w) => { const i = order.indexOf(keyOf(w)); return i < 0 ? order.length : i; };
  return installed.filter((w) => w.slot === slot && !(prefs.hidden || []).includes(keyOf(w)))
    .sort((a, b) => rank(a) - rank(b) || keyOf(a).localeCompare(keyOf(b)));
}

/** One widget in its frame (loaded when it is drawn). */
export function frame(w, context = {}, { bare = false } = {}) {
  const iframe = el("iframe", { sandbox: "allow-scripts", title: w.title, class: "widget-frame",
                                referrerpolicy: "no-referrer" });
  iframe.style.height = `${w.height}px`;
  const record = { iframe, widget: w, context, topics: new Set() };
  frames.add(record);
  const box = el("section", { class: bare ? "widget bare" : "card widget", "data-widget": keyOf(w) },
    bare ? null : el("div", { class: "widget-head" }, el("h3", {}, w.title),
      el("span", { class: "muted small" }, w.package)),
    iframe);
  const [pkg, id] = keyOf(w).split("/");
  ctx.api(`widgets/${enc(pkg)}/${enc(id)}`).then(({ html }) => {
    if (typeof html !== "string") throw new Error(`${w.title}: the console sent no page for it`);
    record.html = html;
    iframe.style.colorScheme = scheme();
    iframe.srcdoc = srcdoc(html, scheme());
    // a widget that leaves its page (navigating itself elsewhere) is taken off the console's page
    let loads = 0;
    iframe.addEventListener("load", () => {
      if (record.redrawing) { record.redrawing = false; return; }
      if (++loads > 1) {
        frames.delete(record);
        iframe.replaceWith(el("p", { class: "error-box small" }, `${w.title} left its page and was stopped.`));
      }
    });
  }).catch((err) => iframe.replaceWith(ctx.errorBox(err)));
  return box;
}

/** A slot's widgets in a strip, or null when it has none. */
export function strip(slot, context = {}) {
  const list = forSlot(slot);
  return list.length ? el("div", { class: `widget-strip slot-${slot.replace(".", "-")}` },
    list.map((w) => frame(w, context))) : null;
}
