// Pieces the views share: findings, the run's targets, and the one fetch of a run (so Results and Directories
// don't each ask for it).

import { el } from "hpclib-ui/index.js";
import { api } from "../client.js";

const ICON = { problem: "Problem", warning: "Warning", info: "Note", ok: "OK" };

export function findingList(found) {
  if (!found || !found.length) return el("p", { class: "muted" }, "Nothing to report yet.");
  return el("div", {}, found.map((f) => el("div", { class: `finding ${f.severity}` },
    el("div", { class: "title" }, `${ICON[f.severity] || f.severity}: ${f.title}`),
    f.detail ? el("div", { class: "detail" }, f.detail) : null,
    f.advice ? el("div", { class: "advice" }, f.advice) : null)));
}

const cache = { id: null, at: 0, value: null };
/** The run, its results and findings; `fresh` skips the half-second cache. */
export async function fetchRun(id, fresh = false) {
  if (!fresh && cache.id === id && Date.now() - cache.at < 500) return cache.value;
  const value = await api(`/api/run?id=${encodeURIComponent(id)}&requests=1`);
  Object.assign(cache, { id, at: Date.now(), value });
  return value;
}

export const FINAL = new Set(["done", "failed", "cancelled"]);
export const runState = (run) => {
  const s = run.targets.map((t) => t.state);
  if (s.some((x) => !FINAL.has(x))) return s.includes("running") ? "running" : "pending";
  if (s.every((x) => x === "done")) return "done";
  return s.includes("done") ? "partial" : "failed";
};
export const targetName = (key) => (key === "login" ? "login node" : key === "any" ? "compute node" : key.replace(/^node-/, ""));
export const stateChip = (state) => el("span", { class: `chip ${state === "done" ? "good" : state === "failed" ? "bad" : state === "partial" ? "warn" : ""}` }, state);
