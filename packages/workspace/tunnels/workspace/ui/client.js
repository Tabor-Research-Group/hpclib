// What every part of the Workspace page shares: the token, the API call, paths and formatting, and the two kinds of
// question (confirm, prompt). It knows nothing about layout; the shell (app.js) and the features import it.
//
//   import { api, rawUrl, join, dirname, basename, size, when, ask, confirmAsk } from "./client.js";
//
// The token comes from the address once (the console's Open link carries it) and then lives in this tab's
// sessionStorage; it travels only in the X-Workspace-Token header. A file for an <img>, <iframe> or <video> gets a
// URL the server signs (rawUrl): it expires and covers one directory, so a page the viewer shows can't use it for
// anything else.

import { configure, confirmDialog, promptDialog } from "hpclib-ui/index.js";

export const token = (() => {
  const q = new URLSearchParams(location.search);
  let t = q.get("token");
  try {
    if (t) sessionStorage.setItem("workspace-token", t);
    else t = sessionStorage.getItem("workspace-token");
    if (q.get("token") && sessionStorage.getItem("workspace-token")) {
      history.replaceState(null, "", location.pathname + location.hash);
    }
  } catch { /* storage may be off: the token stays in the address then */ }
  return t || "";
})();

export async function api(path, { method = "GET", body, raw = false, headers = {} } = {}) {
  const init = { method, headers: { "X-Workspace-Token": token, ...headers } };
  if (body !== undefined) {
    if (body instanceof Blob || body instanceof ArrayBuffer || typeof body === "string") init.body = body;
    else { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
  }
  const res = await fetch(path, init);
  if (raw && res.ok) return res;
  let data = {};
  try { data = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) {
    const err = new Error(data.error || `${res.status} ${res.statusText}`);
    err.status = res.status;
    throw err;
  }
  return data;
}
configure({ api });

export const q = (params) => new URLSearchParams(params).toString();
export const rawUrl = async (path) => (await api("/api/raw-url", { method: "POST", body: { path } })).url;

export const join = (dir, name) => (dir === "/" ? "" : dir) + "/" + name;
export const dirname = (p) => p.replace(/\/[^/]*$/, "") || "/";
export const basename = (p) => p.replace(/^.*\//, "");

export function size(n) {
  if (n === null || n === undefined) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}

export function when(t) {
  const d = new Date(t * 1000);
  const now = new Date();
  return d.toDateString() === now.toDateString() ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { year: d.getFullYear() === now.getFullYear() ? undefined : "numeric", month: "short", day: "numeric" });
}

/** A text answer (null when cancelled). */
export const ask = (text, value = "", ok = "OK") => promptDialog("", text, value, ok);
/** true when the person agreed. */
export const confirmAsk = (text, ok = "OK") => confirmDialog("", text, ok);

/** Download a file the way the browser would from a link. */
export async function download(path) {
  const a = document.createElement("a");
  a.href = await rawUrl(path);
  a.download = basename(path);
  document.body.append(a);
  a.click();
  a.remove();
}

/** node.replaceChildren(...kids), leaving out the null or false a conditional child gives (it would print "null"). */
export const fill = (node, ...kids) => node.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
