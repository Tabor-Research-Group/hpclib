// The Directories tab: what the chosen run's survey found (how many entries each top-level folder holds, the largest
// directories, the listings that were slow), inode and space use per place, and the cluster's own quota output.
// Listing a directory costs time in proportion to its size on most shared file systems: a directory of 100,000
// entries is slow for everything that lists it, however fast the storage is.

import { el } from "hpclib-ui/index.js";
import { bytes, count, fill, ms, tone } from "../client.js";
import { fetchRun, findingList, targetName } from "./shared.js";

export function mountDirectories(root, ctx) {
  const body = el("div");
  root.append(el("h2", {}, "Where your files are"), el("p", { class: "lede" },
    "The survey counts entries per directory without opening the files. Large directories and large file counts slow every call that lists or searches them, " +
    "and a nearly full inode or space quota makes writes fail or crawl."), body);

  async function draw() {
    if (!ctx.runId) { fill(body, el("p", { class: "muted" }, "Run a survey on the Probe tab first.")); return; }
    let d;
    try { d = await fetchRun(ctx.runId); } catch (err) { fill(body, el("p", { class: "error-text" }, err.message)); return; }
    const results = d.results;
    const survey = d.findings.filter((f) => /director|entr|inode|quota|space|file count|listing/i.test(f.title));
    fill(body, survey.length ? [el("h3", {}, "Findings"), findingList(survey)] : null,
      results.map((r) => el("div", {}, el("h3", {}, `Measured from the ${targetName(r.target)}`),
        (r.surveys || []).length ? r.surveys.map(survey_) : el("p", { class: "muted small" }, "No survey was run here."),
        capacity(r), quota(r))));
    if (!results.length) fill(body, el("p", { class: "muted" }, "This run has no results yet."));
  }
  ctx.onRun(() => { if (ctx.tab("directories")) draw(); });

  const survey_ = (s) => el("div", { class: "panel-block" },
    el("div", { class: "chips" }, el("strong", {}, s.root), el("span", { class: "chip" }, `${count(s.entries)} entries`), el("span", { class: "chip" }, `${count(s.directories)} directories`),
      el("span", { class: "chip" }, `${s.seconds} s`), s.truncated ? el("span", { class: "chip warn", title: "it stopped at its entry or time limit: counts are lower bounds" }, "partial") : null,
      s.errors ? el("span", { class: "chip", title: "directories it could not read" }, `${s.errors} unreadable`) : null),
    s.error ? el("p", { class: "error-text" }, s.error) : null,
    s.children && s.children.length ? el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, "Folder"), el("th", { class: "right" }, "Entries"), el("th", { class: "right" }, "Directories"), el("th", {}, ""))),
      el("tbody", {}, s.children.slice(0, 15).map((c) => el("tr", {}, el("td", {}, c.name), el("td", { class: "right" }, count(c.entries)), el("td", { class: "right" }, count(c.directories)),
        el("td", { class: "muted small" }, c.complete ? "" : "partial"))))) : null,
    s.largest_directories && s.largest_directories.length ? [el("h3", {}, "Largest directories"), el("table", {}, el("tbody", {}, s.largest_directories.map((x) =>
      el("tr", {}, el("td", {}, el("code", {}, x.path)), el("td", { class: "right" }, count(x.entries)), el("td", { class: `right ${tone("list", x.list_ms)}` }, ms(x.list_ms))))))] : null,
    s.slow_listings && s.slow_listings.length ? [el("h3", {}, "Slowest listings"), el("table", {}, el("tbody", {}, s.slow_listings.map((x) =>
      el("tr", {}, el("td", {}, el("code", {}, x.path)), el("td", { class: "right" }, count(x.entries)), el("td", { class: `right ${tone("list", x.list_ms)}` }, ms(x.list_ms))))))] : null,
    s.directory_sizes ? el("p", { class: "muted small" }, "Directories by size: ", s.directory_sizes.filter((b) => b.directories).map((b) => `${b.label}: ${count(b.directories)}`).join(" · ")) : null);

  const capacity = (r) => {
    const rows = (r.paths || []).filter((p) => p.statvfs);
    return rows.length ? el("div", { class: "panel-block" }, el("strong", {}, "Space and inodes"), el("table", {},
      el("thead", {}, el("tr", {}, el("th", {}, "Place"), el("th", { class: "right" }, "Inodes used"), el("th", { class: "right" }, "Space free"))),
      el("tbody", {}, rows.map((p) => el("tr", {}, el("td", {}, `${p.label} `, el("span", { class: "muted small" }, p.mount ? p.mount.mountpoint : "")),
        el("td", { class: `right ${p.statvfs.inodes_used_percent >= 95 ? "bad" : p.statvfs.inodes_used_percent >= 85 ? "warn" : ""}` }, p.statvfs.inodes_total ? `${p.statvfs.inodes_used_percent}%` : "n/a"),
        el("td", { class: "right" }, bytes(p.statvfs.bytes_free)))))),
      el("p", { class: "muted small" }, "These are the whole file system's figures; your own quota is below.")) : null;
  };
  const quota = (r) => (r.quota && r.quota.length ? el("div", { class: "panel-block" }, el("strong", {}, "Quota"), r.quota.map((q) =>
    el("div", {}, el("div", { class: "muted small" }, `$ ${q.command}  (exit ${q.exit_code}, ${q.seconds} s)`), el("pre", { class: "quota" }, (q.lines || []).join("\n"))))) : null);

  return { shown: draw };
}
