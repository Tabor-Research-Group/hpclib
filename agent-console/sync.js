// Data transfer -> Sync: saved pairs, warning settings, and explicit local ignore setup.
// Uses existing page elements. Transfers and heartbeat workers are not enabled in this stage.
import { el, action, confirmDialog, toast } from "./components.js";

// Shared entry on the cluster and all-cluster Data transfer pages.
export function syncEntry(cluster) {
  const href = "#/transfer/sync" + (cluster ? `?cluster=${encodeURIComponent(cluster)}` : "");
  return el("hpc-panel", { heading: "Directory sync" },
    el("p", {}, "Pair a local directory with directories on your clusters and manage saved syncs."),
    el("div", { class: "actions" },
      el("a", { class: "button primary", href }, "Manage directory syncs")));
}

export async function syncPage({ api }) {
  const page = el("div", { class: "sync-page" });
  let editing = null;
  let defaults;
  const fail = (e) => page.prepend(el("p", { class: "error-box", role: "alert" }, e.message));
  const call = async (path, method, body) => api(path, { method, body });

  async function setup(record, area) {
    const id = encodeURIComponent(record.spec.id);
    const preview = await api(`sync/${id}/setup`);
    area.replaceChildren(
      el("h4", {}, preview.needs_setup ? "First-time ignore setup" : "Saved ignore settings"),
      el("p", {}, preview.metadata ? `Current choice: ${preview.metadata.git.default_excludes}` : "No sync metadata was found."),
      el("p", { class: "muted" }, preview.message),
      el("p", {}, "Default exclusions:"), el("pre", {}, preview.defaults.join("\n")),
      el("details", {}, el("summary", {}, "Proposed .gitignore change"), el("pre", {}, preview.merge_diff || "No change")),
      action(preview.needs_setup ? "Choose ignore settings…" : "Change ignore settings…", async () => {
        let mode;
        if (preview.gitignore_exists) {
          if (await confirmDialog("Merge default ignore rules?", "Apply the displayed default block to .gitignore, preserving its existing rules?")) {
            mode = "gitignore";
          } else {
            mode = await confirmDialog("Use defaults for this sync only?", "Leave .gitignore unchanged and use the displayed defaults only for hpclib sync?")
              ? "sync-only" : "off";
          }
        } else {
          mode = await confirmDialog("Use default sync exclusions?", "Use the displayed list for this sync only? .gitignore will stay absent.") ? "sync-only" : "off";
        }
        if (!await confirmDialog("Save ignore choice?", `Choice: ${mode}. This writes ${preview.metadata_file}` +
            (mode === "gitignore" ? " and the reviewed .gitignore change." : "; .gitignore stays unchanged.") +
            " No commit or transfer will be started.")) return;
        await call(`sync/${id}`, "POST", { revision: preview.revision,
          setup: { mode, review: preview.review, confirm: true } });
        toast("Ignore choice saved");
        await draw();
      }));
  }

  function flow(spec) {
    return spec.direction || (spec.mode === "rclone" ? spec.rclone?.direction || "push" : "both");
  }

  function arrow(direction) {
    const labels = { both: "Bidirectional sync", push: "Local to remote", pull: "Remote to local" };
    return el("span", { class: "sync-direction-arrow", role: "img", "aria-label": labels[direction],
                        title: labels[direction] }, { both: "↔", push: "→", pull: "←" }[direction]);
  }

  function editor(clusters) {
    const spec = editing?.spec;
    const effective = editing?.settings?.effective_git || spec?.git || defaults.git;
    const sources = editing?.settings?.setting_sources || {};
    const form = el("form", { class: "sync-editor" });
    const field = (label, control, hint) => el("hpc-field", { label, ...(hint ? { hint } : {}) }, control);
    const input = (name, value, extra = {}) => el("input", { name, value, ...extra });
    const name = input("name", spec?.name || "", { required: true, maxlength: 120, "aria-label": "Name" });
    const direction = el("select", { name: "direction", "aria-label": "Sync direction" },
      [["both", "Bidirectional"], ["push", "Local to remote"], ["pull", "Remote to local"]].map(([value, label]) =>
        el("option", { value, selected: value === flow(spec || {}), disabled: value === "both" && spec?.mode === "rclone" }, label)));
    const branch = input("branch", spec?.git.branch || defaults.git.branch, { required: true });
    const interval = input("interval", spec?.interval ?? defaults.interval, { type: "number", min: 0, max: 86400, required: true });
    const one = input("warn_file_mb", effective.warn_file_mb, { type: "number", min: "0.000001", step: "any", required: true });
    const total = input("warn_snapshot_mb", effective.warn_snapshot_mb, { type: "number", min: "0.000001", step: "any", required: true });
    const count = input("warn_file_count", effective.warn_file_count, { type: "number", min: 1, step: 1, required: true });
    const targetRows = el("div", { class: "sync-pairs" });
    const targets = [];
    let localValue = spec?.local || "";
    let nextRow = 0;
    const updateArrows = () => {
      for (const target of targets) target.arrow.replaceChildren(arrow(direction.value));
    };
    direction.addEventListener("change", updateArrows);
    const addTarget = (target = {}) => {
      const rowId = nextRow++;
      const local = input("local", localValue, { id: `sync-local-${rowId}`, required: true,
        placeholder: "/local/directory", "aria-label": "Local directory" });
      const cluster = el("select", { "aria-label": "Cluster", required: true },
        el("option", { value: "" }, "Choose cluster"),
        clusters.map((c) => el("option", { value: c.name, selected: c.name === target.cluster }, c.name)));
      const remote = input("remote", target.remote || "", { id: `sync-remote-${rowId}`, required: true,
        placeholder: "/remote/directory", "aria-label": "Remote directory" });
      const connector = el("div", { class: "sync-connector" }, arrow(direction.value));
      const entry = { local, cluster, remote, arrow: connector };
      local.addEventListener("input", () => {
        localValue = local.value;
        for (const other of targets) if (other !== entry) other.local.value = localValue;
      });
      const row = el("div", { class: "sync-pair-row" },
        el("div", { class: "sync-endpoint" },
          el("div", { class: "sync-endpoint-title" }, el("label", { for: local.id }, "Local directory")), local),
        connector,
        el("div", { class: "sync-endpoint" },
          el("div", { class: "sync-endpoint-title" }, el("label", { for: remote.id }, "Remote directory"), cluster), remote),
        el("button", { type: "button", class: "sync-remove-target", onclick: () => {
          targets.splice(targets.indexOf(entry), 1); row.remove();
        } }, "Remove"));
      targets.push(entry);
      targetRows.append(row);
    };
    (spec?.targets || [{ cluster: new URLSearchParams(location.hash.split("?")[1] || "").get("cluster") || "" }]).forEach(addTarget);
    form.append(
      el("div", { class: "sync-basics" }, field("Name", name), field("Direction", direction)),
      targetRows,
      el("details", { class: "sync-advanced" }, el("summary", {}, "Advanced settings"),
        el("div", { class: "sync-settings-grid" }, field("Sync branch", branch),
          field("Poll interval (seconds)", interval, "0 is manual; scheduling is not available yet.")),
        el("h4", {}, "Snapshot warnings"),
        el("div", { class: "sync-warning-grid" }, field("One file (MB)", one, sources.warn_file_mb),
          field("New/changed content (MB)", total, sources.warn_snapshot_mb),
          field("New/changed files", count, sources.warn_file_count))),
      editing?.settings_error ? el("p", { class: "error-box" }, editing.settings_error) : null,
      el("div", { class: "sync-editor-actions" },
        el("button", { type: "button", onclick: () => addTarget() }, "Add target"),
        el("div", { class: "actions" }, el("button", { type: "submit", class: "primary" }, "Save sync"),
          el("button", { type: "button", onclick: () => { editing = null; draw().catch(fail); } }, "Cancel"))));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const submit = form.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        const body = { v: 1, name: name.value, local: localValue, mode: spec?.mode || "git", direction: direction.value,
          interval: Number(interval.value), policy: spec?.policy || { on_change: "notify", merge: "undecided" },
          targets: targets.map((t) => ({ cluster: t.cluster.value, remote: t.remote.value })),
          git: { ...(spec?.git || {}), branch: branch.value, ignored: spec?.git.ignored || "exclude", warn_file_mb: Number(one.value),
            warn_snapshot_mb: Number(total.value), warn_file_count: Number(count.value) } };
        if (spec?.rclone) body.rclone = { ...spec.rclone, ...(spec.mode === "rclone" ? { direction: direction.value } : {}) };
        if (spec) Object.assign(body, { id: spec.id, revision: spec.revision, paused: spec.paused });
        await call(spec ? `sync/${encodeURIComponent(spec.id)}` : "sync", "POST", body);
        editing = null;
        toast("Sync saved");
        await draw();
      } catch (err) { fail(err); } finally { submit.disabled = false; }
    });
    return el("hpc-panel", { heading: spec ? `Edit ${spec.name}` : "Add directory sync", class: "sync-editor-card" }, form);
  }

  async function draw() {
    const [{ syncs, defaults: givenDefaults }, { clusters }] = await Promise.all([api("sync"), api("clusters")]);
    defaults = givenDefaults;
    page.replaceChildren(el("h2", {}, "Directory sync"),
      el("p", { class: "muted" }, "Choose paired directories and review ignore rules. File transfers are not available yet."));
    if (!editing) page.append(action("Add sync", async () => { editing = {}; await draw(); }));
    if (editing) page.append(editor(clusters));
    for (const record of syncs) {
      const { spec, status } = record;
      if (editing?.spec?.id === spec.id) continue;
      const id = encodeURIComponent(spec.id);
      const area = el("div", { class: "sync-setup" });
      const targetStatus = new Map((status.targets || []).map((t) => [t.target, t]));
      page.append(el("hpc-panel", { heading: spec.name, class: "sync-card" },
        el("div", { class: "sync-card-status" },
          el("hpc-state", { state: "starting", label: spec.paused ? "Paused" : status.state }),
          el("span", { class: "muted" }, status.message)),
        el("div", { class: "sync-pair-table" }, el("table", { class: "compact" },
          el("thead", {}, el("tr", {}, ["Local directory", "", "Remote directory", "State", "Last sync"].map((t) => el("th", {}, t)))),
          el("tbody", {}, spec.targets.map((t) => {
            const cached = targetStatus.get(t.id) || {};
            return el("tr", {}, el("td", { class: "sync-path" }, spec.local), el("td", {}, arrow(flow(spec))),
              el("td", { class: "sync-path" }, el("div", { class: "muted small" }, t.cluster), t.remote),
              el("td", {}, cached.state || "Unverified"),
              el("td", {}, cached.last_successful_sync ? new Date(cached.last_successful_sync * 1000).toLocaleString() : "Never"));
          })))),
        el("div", { class: "actions" },
          action("Edit", async () => {
            editing = { ...record };
            if (spec.mode === "git") {
              try { editing.settings = await api(`sync/${id}/setup`); }
              catch (err) { editing.settings_error = `Repository settings could not be loaded: ${err.message}`; }
            }
            await draw();
          }),
          action("Ignore setup…", async () => setup(record, area)),
          action(spec.paused ? "Resume" : "Pause", async () => {
            await call(`sync/${id}/${spec.paused ? "resume" : "pause"}`, "POST", { revision: spec.revision }); await draw();
          }),
          el("button", { type: "button", disabled: true, title: "File transfers are not available yet" }, "Run"),
          action("Unregister", async () => {
            if (await confirmDialog("Unregister this sync?", "Working directories, sync history, and repository metadata will be preserved.")) {
              await call(`sync/${id}`, "DELETE", { confirm: true, revision: spec.revision }); await draw();
            }
          })), area));
    }
    if (!syncs.length) page.append(el("p", {}, "No sync pairs have been saved."));
  }
  await draw();
  return page;
}
