// Disk usage: a directory's folders by size (du) and by file count (du --inodes: files, folders and links, which is
// what a file quota counts), for any directory; rows open the folder in the file manager, or its own usage. Registers
// the opener "usage" (asked for by id) and the action "Disk usage" on folders.
//
//   GET /api/usage?path=      {path, kb, files, children[{name, path, kb, files}], here, partial, limit, seconds, count_method}

import { el } from "hpclib-ui/index.js";
import { api, q, basename, size, fill } from "../client.js";
import { ws } from "../shell.js";

async function openUsage(dir) {
  const box = el("div", { class: "usage" });
  const tab = ws.tabs.add({ label: `Usage: ${basename(dir) || "/"}`, title: dir, path: dir, view: el("div", {}, box) });
  let key = "kb", data = null;
  const value = (v, k) => (v === null || v === undefined ? "—" : k === "kb" ? size(v * 1024) : v.toLocaleString());
  const draw = () => {
    if (!data) return;
    const kids = data.children.slice().sort((a, b) => (b[key] || 0) - (a[key] || 0));
    const top = kids.length ? kids[0][key] || 0 : 0;
    const total = key === "kb" ? data.kb : data.files;
    const pct = (v) => (total && v ? `${Math.round(100 * v / total)}%` : "");
    const sortButton = (k, label) => el("button", { type: "button", class: k === key ? "primary" : null,
      "aria-pressed": String(k === key), onclick: () => { key = k; draw(); } }, label);
    fill(box,
      el("div", { class: "bar-row" },
        el("code", {}, data.path),
        el("strong", {}, value(data.kb, "kb")), el("span", { class: "muted" }, `${value(data.files, "files")} files`),
        sortButton("kb", "By size"), sortButton("files", "By files"),
        el("button", { type: "button", onclick: () => scan() }, "Scan again"),
        data.partial ? el("span", { class: "warn" }, `partial: the scan hit its ${data.limit} s limit`) : null,
        el("span", { class: "muted small push-right" }, `${data.seconds} s${data.count_method === "walk" ? " · counted by walking (du has no --inodes)" : ""}`)),
      el("table", { class: "usage-table" },
        el("thead", {}, el("tr", {}, el("th", {}, "Folder"), el("th", {}), el("th", { class: "num" }, "Size"),
          el("th", { class: "num" }, "Files"), el("th", { class: "num" }, ""), el("th", {}))),
        el("tbody", {}, kids.map((c) => el("tr", {},
          el("td", {}, el("a", { href: "#", title: `Open ${c.path}`, onclick: (e) => { e.preventDefault(); ws.reveal(c.path); } }, c.name + "/")),
          el("td", { class: "bar-cell" }, el("div", { class: "bar", style: `width:${top ? Math.max(1, 100 * (c[key] || 0) / top) : 0}%` })),
          el("td", { class: `num${key === "kb" ? "" : " muted"}` }, value(c.kb, "kb")),
          el("td", { class: `num${key === "files" ? "" : " muted"}` }, value(c.files, "files")),
          el("td", { class: "num muted" }, pct(c[key])),
          el("td", {}, el("button", { type: "button", class: "small-button", onclick: () => ws.open(c.path, { dir: true }, { opener: "usage" }) }, "Usage")))),
          data.here && data.here.files ? el("tr", { class: "muted" },
            el("td", {}, "(files directly here)"), el("td", {}),
            el("td", { class: "num" }, value(data.here.kb, "kb")), el("td", { class: "num" }, value(data.here.files, "files")),
            el("td", { class: "num" }, pct(data.here[key])), el("td", {})) : null)),
      kids.length ? null : el("p", { class: "muted" }, "No folders here."));
  };
  async function scan() {
    fill(box, el("p", { class: "muted" }, `Scanning ${dir} (du, at low priority; up to two minutes)…`));
    try {
      data = await api(`/api/usage?${q({ path: dir })}`);
      draw();
    } catch (err) { fill(box, el("p", { class: "error-text" }, err.message)); }
  }
  await scan();
  return tab;
}

ws.registerOpener({ id: "usage", match: () => -1, open: (path) => openUsage(path), restore: true });
ws.registerAction({ id: "usage", label: "Disk usage", group: "view", when: (e) => e.dir,
  run: (e) => ws.open(e.path, e, { opener: "usage" }) });
