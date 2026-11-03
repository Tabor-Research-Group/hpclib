// What every part of the page shares: the token, the API call, and how numbers and times are written.
//
//   import { api, download, ms, tone, when, count, bytes, fill } from "./client.js";
//
// The token comes from the address once (the console's Open link carries it) and then lives in this tab's
// sessionStorage; it travels only in the X-Fsdiag-Token header.

import { configure } from "hpclib-ui/index.js";

export const token = (() => {
  const q = new URLSearchParams(location.search);
  let t = q.get("token");
  try {
    if (t) sessionStorage.setItem("fsdiag-token", t);
    else t = sessionStorage.getItem("fsdiag-token");
    if (q.get("token") && sessionStorage.getItem("fsdiag-token")) history.replaceState(null, "", location.pathname + location.hash);
  } catch { /* storage may be off: the token stays in the address then */ }
  return t || "";
})();

export async function api(path, { method = "GET", body, raw = false } = {}) {
  const init = { method, headers: { "X-Fsdiag-Token": token } };
  if (body !== undefined) { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
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

/** Save a text answer (a report) the way a link would; the token is a header, so it can't be a plain link. */
export async function download(path, name) {
  const res = await api(path, { raw: true });
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

export const ms = (x) => (x === null || x === undefined ? "–" : x < 1 ? `${x.toFixed(2)} ms` : x < 10 ? `${x.toFixed(1)} ms` : `${Math.round(x).toLocaleString()} ms`);
export const count = (n) => (n === null || n === undefined ? "–" : Number(n).toLocaleString());
export const when = (t) => (t ? new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
export function bytes(n) {
  if (n === null || n === undefined) return "";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}

// the thresholds fsfindings.py's rules use: over 20 ms is slow and over 100 ms a problem (fsync: 50 and 250)
export function tone(op, x) {
  if (x === null || x === undefined) return "";
  const [warn, bad] = op === "fsync" ? [50, 250] : op === "sqlite_commit" ? [50, 250] : [20, 100];
  return x >= bad ? "bad" : x >= warn ? "warn" : "good";
}
/** 0 to 100: where x ms sits on a log scale from 0.01 ms to 10 s, for the bars. */
export const logPercent = (x) => (x === null || x === undefined ? 0 : Math.max(2, Math.min(100, ((Math.log10(Math.max(x, 0.01)) + 2) / 6) * 100)));

export const fill = (node, ...kids) => node.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
