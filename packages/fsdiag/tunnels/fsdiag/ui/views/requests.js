// The Slow requests tab: what the REST server's slow requests were waiting for. It reads the server's audit log (each
// request's wall time and CPU time) and the stack dumps in the session logs, so it says which routes are slow, how
// much of their time was waiting rather than computing, and which call each stuck request was inside.
// A route whose wall time is far above its CPU time is waiting on something outside the process: here, the file system.

import { el } from "hpclib-ui/index.js";
import { api, count, fill, ms, when } from "../client.js";
import { findingList } from "./shared.js";

export function mountRequests(root, ctx) {
  const opts = { hours: 24, slow_ms: 1000 };
  const body = el("div");
  const controls = el("div", { class: "field-row" },
    el("label", {}, "Look back", el("select", { onchange: (e) => { opts.hours = Number(e.target.value); load(); } },
      [[1, "1 hour"], [6, "6 hours"], [24, "24 hours"], [72, "3 days"], [168, "7 days"]].map(([v, t]) => el("option", { value: v, selected: v === opts.hours }, t)))),
    el("label", {}, "Slow means over", el("select", { onchange: (e) => { opts.slow_ms = Number(e.target.value); load(); } },
      [[250, "250 ms"], [1000, "1 second"], [5000, "5 seconds"]].map(([v, t]) => el("option", { value: v, selected: v === opts.slow_ms }, t)))),
    el("button", { type: "button", onclick: () => load() }, "Refresh"));
  root.append(el("h2", {}, "What slow requests were waiting for"), el("p", { class: "lede" },
    "From the REST server's audit log and its stack dumps. Restart the tunnel after updating hpclib if this is empty: older servers did not log timings."), controls, body);

  async function load() {
    fill(body, el("p", { class: "muted" }, "Reading the logs…"));
    try {
      const { analysis: a, findings } = await api(`/api/requests?hours=${opts.hours}&slow_ms=${opts.slow_ms}`);
      draw(a, findings);
    } catch (err) { fill(body, el("p", { class: "error-text" }, err.message)); }
  }

  function draw(a, findings) {
    const r = a.requests, src = a.sources || {};
    if (!r.count && !(a.stacks && a.stacks.count)) {
      fill(body, el("p", { class: "note-box" }, "No requests found in this period. ", src.audit_log ? `Read ${src.audit_log}.` : "There is no audit log yet: start the tunnel's REST server first."));
      return;
    }
    const peak = Math.max(1, ...a.timeline.map((t) => t.p95_ms));
    fill(body,
      el("div", { class: "cards" },
        card(count(r.count), "requests"), card(`${count(r.slow)}${r.slow_share === null ? "" : ` (${r.slow_share}%)`}`, `slow (over ${ms(a.slow_threshold_ms)})`),
        card(r.wait_share === null ? "–" : `${r.wait_share}%`, "of request time spent waiting, not computing"),
        card(a.slow_wait_share === null ? "–" : `${a.slow_wait_share}%`, "same, for the slow ones"), card(count(a.stacks.count), "stack dumps")),
      el("h3", {}, "Findings"), findingList(findings),
      a.timeline.length ? [el("h3", {}, "When (95th percentile per minute)"), el("div", { class: "timeline" }, a.timeline.map((t) =>
        el("i", { class: t.p95_ms >= 5000 ? "bad" : t.p95_ms >= a.slow_threshold_ms ? "warn" : "", style: `height:${Math.max(2, (t.p95_ms / peak) * 100)}%`,
          title: `${when(t.minute)}: ${t.count} requests, ${t.slow} slow, p95 ${ms(t.p95_ms)}, slowest ${ms(t.max_ms)}` })))] : null,
      a.routes.length ? [el("h3", {}, "Routes by total time"), el("table", {}, el("thead", {}, el("tr", {}, ["Route", "Calls", "Slow", "Median", "p95", "Slowest", "Waiting"].map((h, i) => el("th", { class: i ? "right" : "" }, h)))),
        el("tbody", {}, a.routes.map((x) => el("tr", {}, el("td", {}, el("code", {}, `${x.verb} ${x.route}`)), el("td", { class: "right" }, count(x.count)), el("td", { class: "right" }, count(x.slow)),
          el("td", { class: "right" }, ms(x.p50_ms)), el("td", { class: "right" }, ms(x.p95_ms)), el("td", { class: "right" }, ms(x.max_ms)),
          el("td", { class: `right ${x.wait_share >= 90 ? "warn" : ""}` }, x.wait_share === null ? "–" : `${x.wait_share}%`)))))] : null,
      stacks(a.stacks),
      a.slowest.length ? [el("h3", {}, "Slowest requests"), el("table", {}, el("tbody", {}, a.slowest.map((x) => el("tr", {}, el("td", { class: "muted small" }, when(x.time)),
        el("td", {}, el("code", {}, `${x.verb} ${x.path}`)), el("td", { class: "right" }, x.status), el("td", { class: "right bad" }, ms(x.duration_ms)),
        el("td", { class: "right muted small" }, x.cpu_ms === null || x.cpu_ms === undefined ? "" : `${ms(x.cpu_ms)} cpu`)))))] : null,
      el("p", { class: "muted small" }, `Read ${count(src.audit_entries_with_timing)} audit entries, ${count(src.slow_request_lines)} slow-request lines and ${count(src.stack_dumps)} stack dumps from ${src.session_logs ? src.session_logs.length : 0} session logs.`));
  }
  const card = (big, label) => el("div", { class: "card" }, el("div", { class: "big" }, big), el("div", { class: "muted small" }, label));

  function stacks(s) {
    if (!s || !s.count) return el("p", { class: "muted small" }, "No stack dumps in this period: they are written when a request runs longer than HPCLIB_REST_STACK_MS (5 s by default).");
    return [el("h3", {}, "What stuck requests were inside"),
      el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, "Waiting on"), el("th", { class: "right" }, "Dumps"), el("th", { class: "right" }, "Share"), el("th", {}, "Meaning"))),
        el("tbody", {}, s.categories.map((c) => el("tr", {}, el("td", {}, c.category), el("td", { class: "right" }, count(c.count)), el("td", { class: "right" }, `${c.share}%`), el("td", { class: "small" }, c.advice))))),
      s.frames.length ? el("p", { class: "muted small" }, "Most common last calls: ", s.frames.slice(0, 6).map((f) => `${f.where} (${f.count})`).join(" · ")) : null,
      s.examples.length ? el("details", {}, el("summary", {}, "Example stacks"), s.examples.map((e) => el("div", {},
        el("div", { class: "small" }, `${e.category}: ${e.request}, ${e.seconds} s in, decided by ${e.decided_by}`), el("div", { class: "stack small" }, e.frames.join("\n"))))) : null];
  }
  return { shown: load };
}
