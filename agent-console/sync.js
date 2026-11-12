// Data transfer -> Sync: saved pairs, warning settings, and explicit local ignore setup.
// Uses existing page elements. First sync is reviewed; opt-in background workers live in the console.
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
  let settingUp = false;
  let refreshTimer;
  const fail = (e) => page.prepend(el("p", { class: "error-box", role: "alert" }, e.message));
  const call = async (path, method, body) => api(path, { method, body });

  async function edit(record) {
    editing = { ...record };
    if (record.spec.mode === "git") {
      try { editing.settings = await api(`sync/${encodeURIComponent(record.spec.id)}/setup`); }
      catch (err) { editing.settings_error = `Repository settings could not be loaded: ${err.message}`; }
    }
    await draw();
  }

  async function provision(record) {
    settingUp = true;
    const id = encodeURIComponent(record.spec.id);
    const read = () => api(`sync/${id}/provision`);
    let preview;
    const choose = async (action, endpoint, extra = {}) => {
      preview = await call(`sync/${id}/provision`, "POST", { revision: preview.revision,
        review: preview.review, action, endpoint, confirm: true, ...extra });
    };
    try {
      preview = await read();
      if (preview.inactive.length) {
        toast("This sync is inactive. Restore the original directory or set up a new registration for its new location.");
        return;
      }
      for (const [endpoint, probe] of Object.entries(preview.endpoints)) {
        if (probe.exists) continue;
        if (!await confirmDialog("Create missing directory?", `${probe.root} does not exist. Create it and its missing parent directories? ` +
            "This choice is only for initial setup; moved directories are never recreated automatically.")) return;
        await choose("mkdir", endpoint);
      }
      if (preview.needs_git_choice) {
        if (await confirmDialog("Initialize Git repositories?", "Neither directory is a Git repository. Sync works through Git and cannot function if Git is unavailable. " +
            "Initialize repositories in the displayed directories? You can instead keep Git history in hpclib’s private storage.")) {
          const endpoints = Object.keys(preview.endpoints);
          for (const endpoint of endpoints) await choose("init", endpoint);
        } else {
          if (!await confirmDialog("Use private Git storage?", "Continue using Git history outside the working directories, without adding .git folders? Git is still required.")) return;
          await choose("private-git");
        }
      }
      for (const [endpoint, probe] of Object.entries(preview.endpoints)) {
        if (probe.unavailable) { toast(`Connect the cluster to finish setup for ${probe.root}.`); continue; }
        const ignore = await api(`sync/${id}/provision-ignore?endpoint=${encodeURIComponent(endpoint)}`);
        if (!ignore.needs_setup) continue;
        let mode;
        if (ignore.gitignore_exists) {
          if (await confirmDialog("Merge default ignore rules?", `${probe.root}: add these defaults to the existing .gitignore?\n${ignore.defaults.join("\n")}`)) mode = "gitignore";
          else mode = await confirmDialog("Use defaults for this sync only?", "Keep .gitignore unchanged and use the default exclusion list for hpclib sync?") ? "sync-only" : "off";
        } else mode = await confirmDialog("Use default sync exclusions?", `${probe.root}: exclude environments and caches for this sync?\n${ignore.defaults.join("\n")}`) ? "sync-only" : "off";
        if (!await confirmDialog("Save ignore choice?", `${probe.root}: save ${mode} in .hpclib-sync.json${mode === "gitignore" ? " and update .gitignore" : ""}?`)) return;
        await choose("ignore-apply", endpoint, { mode, ignore_review: ignore.review });
      }
      toast(Object.values(preview.endpoints).some((probe) => probe.unavailable) ? "Local setup saved. Connect the cluster to finish setup." : "Directory setup complete. Review sync before enabling automatic changes.");
    } finally { settingUp = false; await draw(); }
  }

  function backgroundValue(spec) {
    return spec.policy?.background ? spec.policy.on_change : "off";
  }

  function backgroundControl(spec, change) {
    const select = el("select", { "aria-label": "Background sync" },
      [["off", "Off"], ["auto-push", "Upload local changes"], ["auto-both", "Sync both directions"], ["notify", "Check only"]].map(([value, label]) =>
        el("option", { value, selected: value === backgroundValue(spec) }, label)));
    if (change) select.addEventListener("change", () => change(select).catch(fail));
    return select;
  }

  function snapshotLabel(snapshot) {
    if (!snapshot) return el("div", { class: "muted small" }, "Snapshot not checked");
    const commit = snapshot?.commit || snapshot?.tip;
    return el("div", { class: "muted small", title: commit || "No snapshot has been captured for this directory" },
      commit ? `Saved snapshot: ${commit.slice(0, 12)}` : "No saved snapshot");
  }

  function inventory(record) {
    const { spec, status } = record;
    const review = status.review;
    const snapshot = review?.snapshot || status.local_snapshot;
    if (!snapshot?.summary) return null;
    const summary = snapshot.summary;
    const fullVerify = summary.scan_limits.full_verify;
    const preview = summary.eligible_preview;
    const bytes = (n) => `${(n / 1000000).toLocaleString(undefined, { maximumFractionDigits: 3 })} MB`;
    const id = encodeURIComponent(spec.id);
    return el("details", { open: status.state === "needs-volume-review", class: "sync-volume-review" },
      el("summary", {}, status.state === "needs-volume-review" ? "Review snapshot size" : "File inventory"),
      el("p", {}, `${snapshot.root}: ${summary.complete ? "" : "at least "}${summary.eligible_files} eligible files; ` +
        `${summary.new_changed_files} ${fullVerify ? "files selected for full verification" : "new/changed files"}, ${bytes(summary.new_changed_bytes)} estimated content. ` +
        "Sizes are uncompressed file estimates."),
      snapshotLabel(snapshot),
      !fullVerify && summary.complete && snapshot.tip && summary.new_changed_files === 0 && summary.deleted_files === 0
        ? el("p", {}, "No metadata changes since this directory's saved snapshot. This does not compare or synchronize the two working folders.") : null,
      summary.deleted_files ? el("p", {}, `${summary.deleted_files} previously snapshotted files are now absent.`) : null,
      el("p", { class: "muted" }, `Warning limits: ${summary.thresholds.warn_file_mb} MB per file, ` +
        `${summary.thresholds.warn_snapshot_mb} MB total, ${summary.thresholds.warn_file_count} new/changed files.`),
      summary.warnings.length ? el("p", { class: "notice" }, "Snapshot paused: " + summary.warnings.join(", ") + ".") : null,
      preview ? el("table", { class: "compact sync-eligible-files" },
        el("thead", {}, el("tr", {}, ["Eligible files", "Size", "Snapshot status"].map((s) => el("th", {}, s)))),
        el("tbody", {}, preview.length ? preview.map((f) => el("tr", {}, el("td", { class: "sync-path" }, f.path),
          el("td", {}, bytes(f.bytes)), el("td", {}, fullVerify ? "Selected for rehash" :
            f.metadata_changed ? "New/changed metadata" : "Unchanged metadata"))) :
          el("tr", {}, el("td", { colspan: 3 }, "No eligible files found in this scan.")))) :
        el("p", { class: "muted" }, "This helper reports changed files only. Update hpclib on this endpoint to list unchanged eligible files."),
      summary.eligible_preview_truncated ? el("p", { class: "muted" }, `Showing ${preview.length} of ${summary.eligible_files} eligible files.`) : null,
      !summary.complete ? el("p", { class: "notice" }, "The scan stopped at its budget; this inventory is incomplete.") : null,
      summary.largest_files.length ? el("details", {}, el("summary", {}, fullVerify ? "Largest files selected for verification" : "Largest new/changed files"),
        el("table", { class: "compact" }, el("thead", {}, el("tr", {}, el("th", {}, "File"), el("th", {}, "Size"))),
          el("tbody", {}, summary.largest_files.map((f) => el("tr", {}, el("td", { class: "sync-path" }, f.path), el("td", {}, bytes(f.bytes))))))) : null,
      el("p", { class: "muted" }, summary.directories.map((d) => `${d.path}: ${bytes(d.bytes)}`).join(" · ")),
      review && !status.running ? el("div", { class: "actions" },
        summary.complete ? action("Approve snapshot…", async () => {
          if (!await confirmDialog("Approve snapshot ingestion?", `Allow Git to ingest the reviewed content in ${snapshot.root}? ` +
              (status.review_intent === "automatic" ? "This creates sync history, then clean working-file changes can continue automatically under your saved background setting. Conflicts and other warnings still stop for review." :
                "This creates sync history; working files, HEAD and your ordinary index stay unchanged."))) return;
          await call(`sync/${id}/resolve`, "POST", { revision: spec.revision, plan: snapshot.plan,
            action: "approve-snapshot", confirm: true });
          await draw();
        }) : action("Continue scan…", async () => {
          const max_files = Math.min(summary.scan_limits.max_files * 4, 1000000);
          const seconds = Math.min(summary.scan_limits.seconds * 2, 60);
          if (!await confirmDialog("Continue the metadata scan?", `Rescan with a budget of ${max_files} files and ${seconds} seconds. ` +
              "Git will not ingest file content during this check.")) return;
          await call(`sync/${id}/dry-run`, "POST", { revision: spec.revision, max_files, seconds,
            full_verify: summary.scan_limits.full_verify });
          await draw();
        }),
        action("Adjust settings", async () => edit(record))) : null,
      !status.running ? action("Create verified snapshots…", async () => {
        if (!await confirmDialog("Create verified sync snapshots?", (flow(spec) === "push" ? "Git will rehash eligible local files and matching source paths on online targets; cluster-only outputs stay remote. " : "Git will rehash every eligible local and online remote file, including unchanged files, ") +
            "It creates or updates sync commits and transfers source objects as needed. Identical content creates no new commit. " +
            "Working folders are not synchronized. This costs more I/O; warning limits apply to the full inventory before ingestion.")) return;
        await call(`sync/${id}/run`, "POST", { revision: spec.revision, full_verify: true }); await draw();
      }, { disabled: spec.paused || spec.mode !== "git" }) : null);
  }

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

  function syncReview(record) {
    const { spec, status } = record;
    const plan = status.sync_review;
    if (!plan || (status.plan !== plan.plan && !status.unfinished_apply && !["partial", "recovery-needed", "interrupted", "error"].includes(status.state))) return null;
    const conflict = plan.state === "conflict";
    const changes = (title, diff) => el("details", { open: true }, el("summary", {}, title),
      el("p", {}, `${diff.counts.A} additions, ${diff.counts.M} edits, ${diff.counts.D} deletions, ${diff.counts.T} type changes.`),
      el("table", { class: "compact" }, el("thead", {}, el("tr", {}, el("th", {}, "Change"), el("th", {}, "File"))),
        el("tbody", {}, diff.files.length ? diff.files.map((f) => el("tr", {}, el("td", {},
          ({ A: "Add", M: "Edit", D: "Delete", T: "Change type" })[f.action] || f.action),
          el("td", { class: "sync-path" }, f.path))) : el("tr", {}, el("td", { colspan: 2 }, "No working-file changes.")))),
      diff.truncated ? el("p", { class: "muted" }, `Showing 500 of ${diff.total} changed paths.`) : null);
    const retry = status.unfinished_apply || ["partial", "recovery-needed", "interrupted", "error"].includes(status.state);
    return el("details", { class: "sync-merge-review", open: !["in-sync", "uploaded"].includes(status.state) && !status.running },
      el("summary", {}, conflict ? "Git conflicts — no files applied" : plan.type === "upload-group-review" ? "Reviewed Git uploads" : "Reviewed Git sync"),
      plan.targets.some((t) => t.classification === "initial-review") ? el("p", { class: "notice" },
        "First sync: Git compared the independent snapshots against an empty base. Review the combined files before applying.") : null,
      plan.excluded.length ? el("p", { class: "notice" },
        `${plan.excluded.length} offline or failed target(s) are excluded from this plan and will retain their previous sync base.`) : null,
      conflict ? el("div", {}, el("p", {}, "Resolve the differing source files manually, then choose Review sync again."),
        el("pre", {}, plan.conflict.messages.map((m) => m.message).join("\n")),
        el("ul", {}, plan.conflict.paths.map((p) => el("li", { class: "sync-path" }, p)))) :
        el("div", {}, el("p", { class: "muted" }, `Merged snapshot: ${plan.candidate.slice(0, 12)}`),
          changes(`Local: ${spec.local}`, plan.local_changes),
          plan.targets.map((t) => changes(`${t.cluster}: ${t.remote}`, t.changes))),
      !conflict && !["in-sync", "uploaded"].includes(status.state) ? action(retry ? "Retry reviewed sync…" : "Apply reviewed sync…", async () => {
        if (!await confirmDialog("Apply this reviewed Git sync?", (plan.type === "upload-group-review" ? "Upload the displayed additions, edits and deletions to " : "Apply the displayed additions, edits and deletions to the local directory and ") +
            `${plan.targets.length} participating remote directory(s)? ` +
            "Later file edits invalidate this approval. HEAD, main and ordinary indexes are preserved. " +
            (plan.excluded.length ? `${plan.excluded.length} excluded target(s) will not be synchronized.` : ""))) return;
        await call(`sync/${encodeURIComponent(spec.id)}/resolve`, "POST", { revision: spec.revision,
          plan: plan.plan, action: "apply-sync", confirm: true });
        await draw();
      }, { disabled: status.running || spec.paused || (status.state === "stale-plan" && !status.unfinished_apply) }) : null);
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
    const background = backgroundControl(spec || {});
    background.addEventListener("change", () => {
      if (background.value === "auto-push") direction.value = "push";
      if (background.value === "auto-both") direction.value = "both";
      updateArrows();
    });
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
      el("div", { class: "sync-basics" }, field("Name", name), field("Direction", direction), field("Background sync", background, "Choose uploads to leave cluster outputs on the cluster.")),
      targetRows,
      el("details", { class: "sync-advanced" }, el("summary", {}, "Advanced settings"),
        el("div", { class: "sync-settings-grid" }, field("Sync branch", branch),
          field("Poll interval (seconds)", interval, "0 is manual. Automatic sync runs while the console is open and the cluster is connected.")),
        el("h4", {}, "Snapshot warnings"),
        el("div", { class: "sync-warning-grid" }, field("One file (MB)", one, sources.warn_file_mb),
          field("New/changed content (MB)", total, sources.warn_snapshot_mb),
          field("New/changed files", count, sources.warn_file_count))),
      ...(editing?.settings_error ? [el("p", { class: "error-box" }, editing.settings_error)] : []),
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
          interval: Number(interval.value), policy: { on_change: background.value === "off" ? "notify" : background.value, merge: "review", background: background.value !== "off" },
          targets: targets.map((t) => ({ cluster: t.cluster.value, remote: t.remote.value })),
          git: { ...(spec?.git || {}), branch: branch.value, ignored: spec?.git.ignored || "exclude", warn_file_mb: Number(one.value),
            warn_snapshot_mb: Number(total.value), warn_file_count: Number(count.value) } };
        if (spec?.rclone) body.rclone = { ...spec.rclone, ...(spec.mode === "rclone" ? { direction: direction.value } : {}) };
        if (spec) Object.assign(body, { id: spec.id, revision: spec.revision, paused: spec.paused });
        if (background.value !== "off" && !await confirmDialog("Enable background sync?", "After the first reviewed sync, clean changes (including deletions) will run automatically at the configured interval. " +
            "Large snapshots, conflicts and setup changes still pause for review. Keep the console open and the cluster connected.")) return;
        const saved = await call(spec ? `sync/${encodeURIComponent(spec.id)}` : "sync", "POST", body);
        editing = null;
        toast("Sync saved");
        await draw();
        if (!spec) await provision(saved);
      } catch (err) { fail(err); } finally { submit.disabled = false; }
    });
    return el("hpc-panel", { heading: spec ? `Edit ${spec.name}` : "Add directory sync", class: "sync-editor-card" }, form);
  }

  async function draw() {
    clearTimeout(refreshTimer);
    const [{ syncs, defaults: givenDefaults }, { clusters }] = await Promise.all([api("sync"), api("clusters")]);
    defaults = givenDefaults;
    page.replaceChildren(el("h2", {}, "Directory sync"),
      el("p", { class: "muted" }, "Review the first sync, then choose background uploads or two-way sync. Keep the console open and the cluster connected. Upload mode leaves cluster outputs on the cluster."));
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
            const lastSync = cached.last_successful_sync || status.last_successes?.[t.id];
            return el("tr", {}, el("td", { class: "sync-path" }, spec.local,
              spec.mode === "git" ? snapshotLabel(status.local_snapshot) : null), el("td", {}, arrow(flow(spec))),
              el("td", { class: "sync-path" }, el("div", { class: "muted small" }, t.cluster), t.remote,
                spec.mode === "git" ? snapshotLabel(cached.snapshot) : null),
              el("td", {}, cached.state || "Unverified"),
              el("td", {}, lastSync ? new Date(lastSync * 1000).toLocaleString() : "Never"));
          })))),
        (status.targets || []).filter((t) => t.message).map((t) => el("p", { class: "notice" },
          `${spec.targets.find((target) => target.id === t.target)?.cluster || t.target}: ${t.message}`)),
        status.details?.paths ? el("pre", {}, status.details.paths.join("\n")) : null,
        el("hpc-field", { label: "Background sync" }, backgroundControl(spec, async (select) => {
          const value = select.value;
          if (value !== "off" && !await confirmDialog("Enable background sync?", "After the first reviewed sync, clean changes and deletions will run automatically. " +
              (value === "auto-push" ? "Cluster outputs stay remote." : value === "auto-both" ? "Eligible cluster edits and new files can also download to this computer." : "Checks will not change working files."))) {
            select.value = backgroundValue(spec); return;
          }
          const body = { ...spec, policy: { ...spec.policy, background: value !== "off", on_change: value === "off" ? "notify" : value },
            direction: value === "auto-push" ? "push" : value === "auto-both" ? "both" : spec.direction };
          await call(`sync/${id}`, "POST", body); await draw();
        })),
        el("p", { class: "muted small" }, spec.policy?.background && spec.interval ? `Checks every ${spec.interval} seconds; last check: ${status.finished ? new Date(status.finished * 1000).toLocaleString() : "not yet"}.` : "Background sync is off (or the interval is manual)."),
        inventory(record),
        syncReview(record),
        el("div", { class: "actions" },
          action("Set up directories…", async () => provision(record), { disabled: status.running || status.unfinished_apply }),
          action("Edit", async () => edit(record), { disabled: status.running || status.unfinished_apply }),
          action(record.agent_control ? "Revoke agent access…" : "Allow agent access…", async () => {
            const allow = !record.agent_control;
            if (!await confirmDialog(allow ? "Allow agent control of this sync?" : "Revoke agent control?", allow ?
              "Allow enabled tunnel actions to inspect this group, capture Git snapshots, and apply explicitly confirmed changes to these local and remote directories? " +
              "Every target must be on the requesting cluster and within the agent token's directories." :
              "Stop future agent commands for this sync. Operations already accepted will finish or report recovery status.")) return;
            await call(`sync/${id}/agent-control`, "POST", { revision: spec.revision, allow, confirm: true });
            await draw();
          }, { disabled: spec.mode !== "git" }),
          action("Ignore setup…", async () => setup(record, area), { disabled: status.running || status.unfinished_apply }),
          action("Check files", async () => {
            await call(`sync/${id}/dry-run`, "POST", { revision: spec.revision }); await draw();
          }, { disabled: status.running || status.unfinished_apply || spec.paused || spec.mode !== "git" }),
          action("Create snapshots…", async () => {
            if (!await confirmDialog("Create sync snapshots?", (flow(spec) === "push" ? "Capture local source files and matching paths on targets; cluster-only outputs stay remote. " : "Capture eligible local and online remote files on the sync branch. ") +
                "Large inventories pause for review before ingestion. Working files, HEAD and ordinary indexes stay unchanged.")) return;
            await call(`sync/${id}/run`, "POST", { revision: spec.revision }); await draw();
          }, { disabled: status.running || status.unfinished_apply || spec.paused || spec.mode !== "git" }),
          action(spec.paused ? "Resume" : "Pause", async () => {
            await call(`sync/${id}/${spec.paused ? "resume" : "pause"}`, "POST", { revision: spec.revision }); await draw();
          }, { disabled: status.running || status.unfinished_apply }),
          action("Review sync…", async () => {
            await call(`sync/${id}/review`, "POST", { revision: spec.revision }); await draw();
          }, { primary: true, disabled: status.running || status.unfinished_apply || spec.paused || spec.mode !== "git" || flow(spec) === "pull",
            title: flow(spec) === "pull" ? "Automatic downloads alone are not supported; choose uploads or both directions" : "Review the merged Git working-file changes" }),
          action("Unregister", async () => {
            if (await confirmDialog("Unregister this sync?", "Working directories, sync history, and repository metadata will be preserved.")) {
              await call(`sync/${id}`, "DELETE", { confirm: true, revision: spec.revision }); await draw();
            }
          })), area));
    }
    if (!syncs.length) page.append(el("p", {}, "No sync pairs have been saved."));
    if (!editing && !settingUp && syncs.some((record) => record.status.running || record.spec.policy?.background)) refreshTimer = setTimeout(() => {
      if (page.isConnected && !editing && !settingUp) draw().catch(fail);
    }, syncs.some((record) => record.status.running) ? 1000 : 5000);
  }
  await draw();
  return page;
}
