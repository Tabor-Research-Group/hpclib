// This machine's settings, from the console menu: the theme switch, Interface Settings (#/interface: the CSS
// variables of default-styles.css), Console Settings (#/console: notifications, local ports, LLM clients,
// defaults for new clusters), About (#/about: versions, the console's link, diagnostics), and the notifications
// themselves. They use GET/PUT /api/preferences and the routes beside it (agent_console.py).
import { el, action, confirmDialog } from "./components.js";
import * as widgets from "./widgets.js";

let ctx = null;       // { api, errorBox, field, key: () => the session key }
let prefs = null;     // GET /api/preferences, once loaded
export function init(given) { ctx = given; }
export const preferences = () => prefs;
export async function loadPreferences() {
  try { prefs = await ctx.api("preferences"); } catch { prefs = null; }   // an older console
  return prefs;
}

const field = (...a) => ctx.field(...a);
const enc = encodeURIComponent;
const chip = (label, tone = "down") => el("span", { class: `chip ${tone}` }, label);
const saveButton = (label, fn) => action(label, fn, { primary: true, result: true });

// ---------------------------------------------------------------- theme (the menu's switch)

const THEME_KEY = "hpc-theme";
const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
function chosenTheme() {
  try { const t = localStorage.getItem(THEME_KEY); return t === "light" || t === "dark" ? t : null; } catch { return null; }
}
export const isDark = () => (chosenTheme() || (systemDark.matches ? "dark" : "light")) === "dark";
export function setTheme(theme) {       // "light", "dark", or null: the system's
  try { theme ? localStorage.setItem(THEME_KEY, theme) : localStorage.removeItem(THEME_KEY); } catch { /* this page only */ }
  if (theme) document.documentElement.setAttribute("data-theme", theme);
  else document.documentElement.removeAttribute("data-theme");
  syncThemeSwitch();
  document.dispatchEvent(new Event("hpc-theme"));
}
function syncThemeSwitch() {
  const sw = document.getElementById("theme-switch");
  if (!sw) return;
  sw.setAttribute("aria-checked", String(isDark()));
  document.getElementById("theme-system").hidden = !chosenTheme();
}
export function initThemeSwitch() {
  document.getElementById("theme-switch").addEventListener("click", () => setTheme(isDark() ? "light" : "dark"));
  document.getElementById("theme-system").addEventListener("click", () => setTheme(null));
  systemDark.addEventListener("change", syncThemeSwitch);
  syncThemeSwitch();
}

// ---------------------------------------------------------------- Interface Settings (#/interface)

// default-styles.css, read as the page reads it: groups ("Colours"), each variable's description (the comment
// above it), and its light and dark defaults.
async function styleVariables() {
  const text = await (await fetch("default-styles.css", { cache: "no-store" })).text();
  const rootBlock = (text.match(/:root\s*\{([\s\S]*?)\n\}/) || [])[1] || "";
  const darkBlock = (text.match(/:root\[data-theme="dark"\]\s*\{([\s\S]*?)\}/) || [])[1] || "";
  const dark = Object.fromEntries([...darkBlock.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
  const groups = [];
  let comments = [];
  for (const line of rootBlock.split("\n").map((l) => l.trim())) {
    const c = line.match(/^\/\*\s*(.*?)\s*\*\/$/);
    if (c) { comments.push(c[1]); continue; }
    const v = line.match(/^(--[a-z0-9-]+)\s*:\s*([^;]+);/);
    if (!v) continue;
    if (comments.length > 1 || !groups.length) groups.push({ title: comments.length > 1 ? comments[0] : "Variables", vars: [] });
    groups.at(-1).vars.push({ name: v[1], value: v[2].trim(), dark: dark[v[1]] || null, description: comments.at(-1) || "" });
    comments = [];
  }
  return groups;
}

// the same CSS as /interface.css (console_prefs.interface_css), for the preview while editing
function interfaceCss(ui) {
  const block = (sel, vals) => Object.keys(vals).length
    ? `${sel} {\n${Object.entries(vals).map(([k, v]) => `  ${k}: ${v};`).join("\n")}\n}\n` : "";
  const light = { ...ui.common, ...ui.light };
  return block(":root", light) + (Object.keys(ui.dark).length
    ? `@media (prefers-color-scheme: dark) {\n${block(':root:not([data-theme="light"])', ui.dark)}}\n` : "") +
    block(':root[data-theme="dark"]', ui.dark);
}

function previewStyle() {
  let s = document.getElementById("interface-preview");
  if (!s) {
    s = el("style", { id: "interface-preview" });
    document.getElementById("user-css").before(s);     // user.css still wins, as it will once saved
  }
  return s;
}
function reloadInterfaceCss() {
  const link = document.getElementById("interface-css");
  link.href = `/interface.css?t=${Date.now()}`;
  document.getElementById("interface-preview")?.remove();
}

const isHex = (v) => /^#[0-9a-f]{3}([0-9a-f]{3})?$/i.test(v || "");
const fullHex = (v) => v.length === 4 ? "#" + [...v.slice(1)].map((c) => c + c).join("") : v;

export async function interfacePage() {
  const [groups, p] = await Promise.all([styleVariables(), loadPreferences()]);
  const saved = (p && p.interface) || { common: {}, light: {}, dark: {} };
  const ui = { common: { ...saved.common }, light: { ...saved.light }, dark: { ...saved.dark } };
  const changed = el("span", { class: "muted small", role: "status" });
  const preview = () => { previewStyle().textContent = interfaceCss(ui); changed.textContent = "Previewing: Save keeps it."; };
  const input = (part, v, fallback) => {
    const text = el("input", { value: ui[part][v.name] || "", placeholder: fallback, size: 18,
                               "aria-label": `${v.name}${part === "dark" ? " (dark)" : part === "light" ? " (light)" : ""}` });
    const swatch = isHex(fallback) ? el("input", { type: "color", value: fullHex(isHex(text.value) ? text.value : fallback),
                                                   "aria-label": `${v.name} colour${part === "dark" ? " (dark)" : ""}` }) : null;
    const set = (value) => {
      if (value) ui[part][v.name] = value; else delete ui[part][v.name];
      text.value = value;
      if (swatch && isHex(value || fallback)) swatch.value = fullHex(value || fallback);
      preview();
    };
    text.addEventListener("input", () => set(text.value.trim()));
    swatch?.addEventListener("input", () => set(swatch.value));
    const reset = el("button", { type: "button", class: "link-button", title: "Back to the default" }, "default");
    reset.addEventListener("click", () => set(""));
    return el("div", { class: "var-input" }, swatch, text, reset);
  };
  const rows = groups.map((g) => el("section", { class: "card var-group" },
    el("h3", {}, g.title),
    el("table", { class: "vars" }, el("tbody", {}, g.vars.map((v) => el("tr", {},
      el("td", {}, el("code", {}, v.name), el("div", { class: "muted small" }, v.description)),
      v.dark
        ? el("td", {}, el("div", { class: "var-pair" }, el("span", { class: "muted small" }, "Light"), input("light", v, v.value),
                         el("span", { class: "muted small" }, "Dark"), input("dark", v, v.dark)))
        : el("td", {}, input("common", v, v.value))))))));
  const save = saveButton("Save", async () => {
    prefs = await ctx.api("preferences", { method: "PUT", body: { interface: ui } });
    reloadInterfaceCss();
    changed.textContent = "";
    return "Saved for this console.";
  });
  const resetAll = action("Reset all", async () => {
    if (!await confirmDialog("Reset the interface?", "Every value here goes back to its default " +
                             "(user.css, if you have one, is not changed).", "Reset")) return;
    prefs = await ctx.api("preferences", { method: "PUT", body: { interface: { common: {}, light: {}, dark: {} } } });
    reloadInterfaceCss();
    location.reload();
  });
  const openSheet = action("Open user.css", async () => {
    const out = await ctx.api("styles/open", { method: "POST" });
    return out.created ? `Created ${out.path} from the defaults and opened it.` : `Opened ${out.path}.`;
  }, { result: true });
  return [
    el("h2", {}, "Interface Settings"),
    el("p", { class: "muted" }, "The page's look: colours (with their dark-theme values), type, corners, spacing " +
      "and sizes. Empty fields use the default shown in them. Changes preview as you type; Save keeps them for " +
      "this console, in every browser that opens it."),
    el("section", { class: "card" },
      el("h3", {}, "Theme"),
      themeChoice()),
    widgetsCard(),
    el("div", { class: "toolbar sticky-actions" }, save, resetAll, changed),
    rows,
    el("section", { class: "card" },
      el("h3", {}, "The complete stylesheet"),
      el("p", { class: "muted" }, "For anything these fields don't cover, the console loads user.css after them: " +
        "a copy of the defaults that you edit in your own editor, with any CSS. Where both set a variable, user.css wins."),
      el("div", { class: "actions" },
        el("a", { class: "button", href: "default-styles.css", target: "_blank", rel: "noopener" }, "View the default stylesheet"),
        el("a", { class: "button", href: "/user.css", target: "_blank", rel: "noopener" }, "View user.css"),
        openSheet)),
  ];
}

// Packaged widgets: shown or hidden, and their order within each place (saved at once)
const SLOT_LABEL = { home: "Clusters page", "cluster.overview": "Each cluster's Overview", "cluster.tab": "Tabs on each cluster's page" };
function widgetsCard() {
  const box = el("section", { class: "card", id: "widgets" });
  const draw = () => {
    const list = widgets.all();
    const w = (prefs && prefs.widgets) || { hidden: [], order: [] };
    const hidden = new Set(w.hidden || []);
    const order = (slot) => {
      const shown = widgets.forSlot(slot);
      return [...shown, ...list.filter((x) => x.slot === slot && !shown.includes(x))];
    };
    const save = async (next) => {
      prefs = await ctx.api("preferences", { method: "PUT", body: { widgets: next } });
      draw();
    };
    const move = (slot, key, by) => {
      const keys = order(slot).map((x) => x.key);
      const i = keys.indexOf(key);
      if (i + by < 0 || i + by >= keys.length) return;
      [keys[i], keys[i + by]] = [keys[i + by], keys[i]];
      const others = (w.order || []).filter((k) => !keys.includes(k));
      return save({ hidden: [...hidden], order: [...others, ...keys] });
    };
    const rows = Object.keys(SLOT_LABEL).flatMap((slot) => {
      const items = order(slot);
      if (!items.length) return [];
      return [el("tr", {}, el("th", { colspan: 4 }, SLOT_LABEL[slot])), ...items.map((x, i) => {
        const show = el("input", { type: "checkbox", checked: !hidden.has(x.key), "aria-label": `Show ${x.title}` });
        show.addEventListener("change", () => {
          show.checked ? hidden.delete(x.key) : hidden.add(x.key);
          save({ hidden: [...hidden], order: w.order || [] });
        });
        const up = el("button", { type: "button", disabled: i === 0, "aria-label": `Move ${x.title} up` }, "↑");
        const down = el("button", { type: "button", disabled: i === items.length - 1, "aria-label": `Move ${x.title} down` }, "↓");
        up.addEventListener("click", () => move(slot, x.key, -1));
        down.addEventListener("click", () => move(slot, x.key, 1));
        return el("tr", {},
          el("td", {}, show),
          el("td", {}, el("strong", {}, x.title), el("div", { class: "muted small" },
            `${x.package} ${x.version} · reads ${x.reads.join(", ") || "nothing"}`), x.description
            ? el("div", { class: "muted small" }, x.description) : null),
          el("td", { class: "next" }, el("div", { class: "actions" }, up, down)));
      })];
    });
    box.replaceChildren(
      el("h3", {}, "Widgets"),
      el("p", { class: "muted" }, "Small dashboards that packages add (Packages, at the top). Each runs sandboxed: " +
        "it sees only the data listed for it, can't use the console's key or change anything, and has no network " +
        "access. Changes here apply at once."),
      rows.length ? el("table", { class: "compact widgets-table" }, el("tbody", {}, rows))
        : el("p", { class: "muted" }, "None installed."));
  };
  draw();
  return box;
}

function themeChoice() {
  const box = el("div", { class: "segmented", role: "radiogroup", "aria-label": "Theme" });
  const draw = () => {
    const now = chosenTheme();
    box.replaceChildren(...[[null, "Match system"], ["light", "Light"], ["dark", "Dark"]].map(([t, label]) => {
      const b = el("button", { type: "button", role: "radio", "aria-checked": String(now === t) }, label);
      b.addEventListener("click", () => setTheme(t));
      return b;
    }));
  };
  document.addEventListener("hpc-theme", draw);
  draw();
  return [box, el("p", { class: "muted small" }, "Kept in this browser; the menu's switch sets it too.")];
}

// ---------------------------------------------------------------- Console Settings (#/console)

const NOTES = [["tunnel", "A tunnel stops without Stop", "an agent tunnel or an app's"],
               ["prompt", "A tunnel asks for a password or a host key", "while this page isn't in front"],
               ["job", "An agent's job finishes", "completed, failed, cancelled or timed out"],
               ["proposal", "An agent proposes a template", "on any cluster"]];

function notificationsCard(p) {
  const supported = "Notification" in window;
  const boxes = Object.fromEntries(NOTES.map(([k]) => [k, el("input", { type: "checkbox", checked: !!p.notifications[k] })]));
  const permission = el("span");
  const ask = el("button", { type: "button" }, "Allow notifications in this browser");
  const drawPermission = () => {
    const state = supported ? Notification.permission : "unsupported";
    permission.replaceChildren(chip({ granted: "allowed in this browser", denied: "blocked in this browser",
                                      default: "not allowed yet", unsupported: "not supported here" }[state],
                                    { granted: "up", denied: "error" }[state] || "starting"));
    ask.hidden = state !== "default";
  };
  ask.addEventListener("click", async () => { await Notification.requestPermission(); drawPermission(); });
  drawPermission();
  const save = saveButton("Save", async () => {
    prefs = await ctx.api("preferences", { method: "PUT",
      body: { notifications: Object.fromEntries(NOTES.map(([k]) => [k, boxes[k].checked])) } });
    if (supported && Notification.permission === "default" && Object.values(boxes).some((b) => b.checked)) {
      await Notification.requestPermission();
      drawPermission();
    }
    restartWatch();
    return "Saved.";
  });
  const test = action("Send a test", () => {
    if (!supported || Notification.permission !== "granted") throw new Error("Allow notifications in this browser first.");
    new Notification("hpclib console", { body: "Notifications work in this browser." });
    return "Sent.";
  }, { result: true });
  return el("section", { class: "card", id: "notifications" },
    el("h3", {}, "Notifications"),
    el("p", { class: "muted" }, "Shown by this browser while the console's page is open (a background tab is " +
      "enough). Clicking one opens the page where it happened."),
    el("div", { class: "toolbar" }, permission, ask),
    el("div", { class: "checks" }, NOTES.map(([k, label, hint]) =>
      el("label", {}, boxes[k], ` ${label} `, el("span", { class: "muted small" }, `(${hint})`)))),
    el("div", { class: "actions" }, save, test));
}

async function portsCard() {
  const box = el("section", { class: "card", id: "ports" });
  const notice = el("p", { class: "notice", role: "status" });     // kept across redraws
  const draw = async () => {
    const r = await ctx.api("ports");
    const lo = el("input", { type: "number", min: 1024, max: 65535, value: r.range[0], size: 7, "aria-label": "Lowest port" });
    const hi = el("input", { type: "number", min: 1024, max: 65535, value: r.range[1], size: 7, "aria-label": "Highest port" });
    const save = saveButton("Save range", async () => {
      await ctx.api("ports/range", { method: "PUT", body: { low: Number(lo.value), high: Number(hi.value) } });
      return "Saved. New ports come from it; ports already given stay until you pick new ones.";
    });
    const reset = action("Default", async () => { await ctx.api("ports/range", { method: "PUT", body: { reset: true } }); await draw(); });
    const groups = new Map();
    for (const row of r.ports) {
      const k = `${row.cluster}\u0000${row.app || ""}`;
      if (!groups.has(k)) groups.set(k, { cluster: row.cluster, app: row.app, title: row.title, rows: [] });
      groups.get(k).rows.push(row);
    }
    const label = { free: ["free", "down"], tunnel: ["its tunnel is running", "up"], taken: ["taken by another program", "error"] };
    const rows = [...groups.values()].map((g) => {
      const worst = g.rows.some((x) => x.status === "taken") ? "taken" : g.rows.some((x) => x.status === "tunnel") ? "tunnel" : "free";
      const repick = action("New ports", async () => {
        const out = await ctx.api(`clusters/${enc(g.cluster)}/new-ports`, { method: "POST", body: { app: g.app } });
        const synced = out.llm_clients ? Object.keys(out.llm_clients).length : 0;
        notice.textContent = `${g.title} on ${g.cluster}: now ${out.port} and ${out.process_port}.` + (out.mcp_updated
          ? ` Its MCP entry changed${synced ? " and was written to your LLM clients" : ": write it to your LLM clients below"}.` : "");
        await draw();
      }, { primary: worst === "taken", disabled: worst === "tunnel",
           title: worst === "tunnel" ? "stop it first" : "pick two new free ports" });
      return el("tr", {},
        el("td", {}, el("strong", {}, g.cluster)), el("td", {}, g.title),
        el("td", { class: "num" }, g.rows.map((x) => el("div", {}, String(x.port),
          el("span", { class: "muted small" }, x.which === "port" ? " (local)" : " (second)")))),
        el("td", {}, g.rows.map((x) => el("div", {}, chip(...label[x.status])))),
        el("td", { class: "next" }, repick));
    });
    box.replaceChildren(
      el("h3", {}, "Local ports"),
      el("p", { class: "muted" }, "Each tunnel listens on two ports on this machine, picked at random from this " +
        `range (default ${r.default_range[0]}–${r.default_range[1]}, below the systems' own ranges). If another ` +
        "program takes one, give that tunnel new ports while it is stopped."),
      el("div", { class: "toolbar" }, field("From", lo), field("To", hi), save, reset),
      notice,
      rows.length ? el("table", { class: "compact" },
        el("thead", {}, el("tr", {}, ["Cluster", "Tunnel", "Ports", "Now", ""].map((h) => el("th", {}, h)))),
        el("tbody", {}, rows)) : el("p", { class: "muted" }, "No tunnels yet."));
  };
  await draw().catch((err) => box.replaceChildren(el("h3", {}, "Local ports"), ctx.errorBox(err)));
  return box;
}

async function clientsCard() {
  const box = el("section", { class: "card", id: "llm-clients" });
  const notice = el("p", { class: "notice", role: "status" });
  const draw = async () => {
    const { clients } = await ctx.api("llm-clients");
    const items = clients.map((c) => {
      const counts = { current: 0, different: 0, missing: 0 };
      for (const e of c.entries) counts[e.state]++;
      const keep = el("input", { type: "checkbox", checked: c.enabled, disabled: !c.available });
      keep.addEventListener("change", async () => {
        prefs = await ctx.api("preferences", { method: "PUT", body: { llm_clients: { [c.id]: keep.checked } } });
        if (keep.checked && (counts.different || counts.missing)) {
          try { await ctx.api(`llm-clients/${c.id}/write`, { method: "POST" }); } catch (err) { box.prepend(ctx.errorBox(err)); return; }
        }
        await draw();
      });
      const write = action(counts.different || counts.missing ? "Write entries" : "Rewrite", async () => {
        const out = await ctx.api(`llm-clients/${c.id}/write`, { method: "POST" });
        notice.textContent = out.changed.length ? `${c.title}: wrote ${out.changed.join(", ")}. ${c.after}`
                                                : `${c.title}: already up to date.`;
        await draw();
      }, { primary: !!(counts.different || counts.missing), disabled: !c.available || !c.entries.length });
      const summary = !c.available ? chip("not installed", "down")
        : c.error ? chip("can't read its configuration", "error")
        : !c.entries.length ? chip("no clusters set up yet", "down")
        : el("span", { class: "chips" },
            counts.current ? chip(`${counts.current} current`, "up") : null,
            counts.different ? chip(`${counts.different} out of date`, "starting") : null,
            counts.missing ? chip(`${counts.missing} missing`, "starting") : null);
      return el("tr", {},
        el("td", {}, el("strong", {}, c.title), c.windows ? el("span", { class: "muted small" }, " · Windows, through wsl.exe") : null,
          c.detail ? el("div", { class: "muted small" }, c.detail) : null,
          el("div", { class: "muted small" }, c.command ? `${c.command} · ${c.path}` : c.path)),
        el("td", {}, summary,
          c.error ? el("div", { class: "error-box small" }, c.error) : null,
          c.entries.length && c.available ? el("div", { class: "muted small" },
            c.entries.map((e) => `${e.name}: ${e.state === "different" ? "out of date" : e.state}`).join(" · ")) : null,
          c.stale.map((name) => el("div", { class: "muted small" }, `${name}: no cluster has it any more `,
            action("Remove", async () => {
              await ctx.api(`llm-clients/${c.id}/remove`, { method: "POST", body: { name } });
              await draw();
            })))),
        el("td", {}, el("label", { title: "rewrite its entries whenever setup or New ports changes them" }, keep, " Keep up to date")),
        el("td", { class: "next" }, write));
    });
    box.replaceChildren(
      el("h3", {}, "LLM clients"),
      el("p", { class: "muted" }, "Each cluster's MCP entry (written by setup) in the clients on this machine, under " +
        "its MCP name (rename one on its Agents tab). Only those entries are changed; each configuration file is " +
        "copied to FILE.before-hpclib the first time. Kept up to date, a client gets new entries after each setup, " +
        "New ports and rename. ChatGPT's are in Codex's config.toml, which the ChatGPT desktop app shares; " +
        "ChatGPT on the web can't reach local servers."),
      notice,
      el("table", { class: "compact" }, el("tbody", {}, items)));
  };
  await draw().catch((err) => box.replaceChildren(el("h3", {}, "LLM clients"), ctx.errorBox(err)));
  return box;
}

// This machine (local_platform.py): which platform the console dispatches on (how it opens files and the browser,
// and where the LLM clients' configuration is), detected unless chosen here; under WSL, the Windows profile folder.
async function machineCard() {
  const box = el("section", { class: "card", id: "machine" });
  const draw = async () => {
    const m = await ctx.api("platform");
    const titles = { auto: `Detect (${m.detected === "wsl" ? "WSL" : m.detected === "macos" ? "macOS" : m.detected === "windows" ? "Windows" : "Linux"})`,
                     linux: "Linux", macos: "macOS", wsl: "WSL (Linux on Windows)" };
    const choice = el("select", { "aria-label": "Platform" }, m.choices.map((v) =>
      el("option", { value: v, selected: v === m.setting }, titles[v] || v)));
    const home = el("input", { value: m.windows_home_setting || "", size: 34, placeholder: m.windows_home
      ? `found: ${m.windows_home}` : "/mnt/c/Users/NAME", "aria-label": "Windows profile folder" });
    const homeField = field("Windows profile folder", home, "as WSL sees it; empty: ask Windows (%USERPROFILE%)");
    const showHome = () => { homeField.hidden = !(choice.value === "wsl" || (choice.value === "auto" && m.detected === "wsl")); };
    choice.addEventListener("change", showHome);
    showHome();
    const save = saveButton("Save", async () => {
      prefs = await ctx.api("preferences", { method: "PUT", body: { system: {
        platform: choice.value, windows_home: home.value.trim() } } });
      if (ctx.render) ctx.render(); else await draw();     // the LLM clients' paths follow the platform
      return "";
    });
    const now = el("p", { class: "muted" }, "Now: ", el("strong", {}, m.title),
      ` (${{ env: "from $HPCLIB_PLATFORM, which wins over this setting", settings: "chosen here", detected: "detected" }[m.source] || m.source})`,
      m.platform === "wsl" ? `. Files and pages open with ${m.opener}; Claude Desktop, Cursor and ChatGPT are ` +
        `Windows programs, configured in ${m.windows_home || "your Windows profile folder (not found: set it below)"}, ` +
        `whose entries start the MCP server in WSL with wsl.exe${m.wsl_distro ? ` -d ${m.wsl_distro}` : ""}.` : ".");
    box.replaceChildren(
      el("h3", {}, "This machine"),
      el("p", { class: "muted" }, "How the console opens files and your browser, and where it finds the LLM clients' " +
        "configuration, depend on the platform it runs on. It is detected; choose it here if detection gets it " +
        "wrong (for example, WSL not recognized). setup_agents on the command line reads this too."),
      now,
      el("div", { class: "row" }, field("Platform", choice, "auto: detect it each time"), homeField),
      el("div", { class: "actions" }, save));
  };
  await draw().catch((err) => box.replaceChildren(el("h3", {}, "This machine"), ctx.errorBox(err)));
  return box;
}

function defaultsCard(p) {
  const d = p.new_cluster;
  const hours = el("input", { type: "number", min: 1, max: 168, value: d.connection_hours, size: 5 });
  const pattern = el("input", { value: d.work_dir_pattern || "", placeholder: "/scratch/user/{user}/llm", size: 34 });
  const mcpName = el("input", { value: d.mcp_name_pattern || "hpclib-{cluster}", placeholder: "hpclib-{cluster}", size: 22 });
  const gpus = el("select", {}, [["auto", "when the job asks for GPUs"], ["always", "always"], ["never", "never"]]
    .map(([v, t]) => el("option", { value: v, selected: d.sandbox_gpus === v }, t)));
  const maxGpus = el("input", { type: "number", min: 0, max: 64, value: d.max_gpus ?? "", placeholder: "server default (0)", size: 10 });
  const save = saveButton("Save", async () => {
    prefs = await ctx.api("preferences", { method: "PUT", body: { new_cluster: {
      connection_hours: Number(hours.value), work_dir_pattern: pattern.value.trim(), sandbox_gpus: gpus.value,
      max_gpus: maxGpus.value === "" ? null : Number(maxGpus.value),
      mcp_name_pattern: mcpName.value.trim() || "hpclib-{cluster}" } } });
    return "Saved. Clusters added from now on start with these; each can change them on its own Settings tab.";
  });
  return el("section", { class: "card", id: "new-cluster" },
    el("h3", {}, "Defaults for new clusters"),
    el("div", { class: "row" },
      field("Keep a login for (hours)", hours, "how long an idle ssh login is reused"),
      field("Work directory", pattern, "suggested by Add cluster; {user} is the login's user"),
      field("MCP server name", mcpName, "what LLM clients call it; {cluster}, {host}, {user}"),
      field("NVIDIA GPUs in sandboxed jobs", gpus, "the REST server's --nv"),
      field("GPUs per job, at most", maxGpus, "empty: the server's default")),
    el("p", { class: "muted small" }, "A cluster configuration from a package keeps its own login time; the GPU " +
      "settings apply where it doesn't set them."),
    el("div", { class: "actions" }, save));
}

export async function consolePage() {
  const p = await loadPreferences();
  if (!p) throw new Error("This console is older than its page: restart it (launch-tunnel-manager).");
  const [ports, clients, machine] = await Promise.all([portsCard(), clientsCard(), machineCard()]);
  return [
    el("h2", {}, "Console Settings"),
    el("p", { class: "muted" }, "Settings for this console on this machine, not for a cluster (those are on each " +
      "cluster's Settings tab)."),
    el("nav", { class: "subtabs" }, [["notifications", "Notifications"], ["ports", "Local ports"],
      ["llm-clients", "LLM clients"], ["new-cluster", "Defaults for new clusters"], ["machine", "This machine"]].map(([id, label]) => {
      const a = el("a", { href: `#/console` }, label);
      a.addEventListener("click", (e) => { e.preventDefault(); document.getElementById(id)?.scrollIntoView({ behavior: "smooth" }); });
      return a;
    })),
    notificationsCard(p), ports, clients, defaultsCard(p), machine,
  ];
}

// ---------------------------------------------------------------- About (#/about)

export async function aboutPage() {
  const a = await ctx.api("about");
  const link = `${location.origin}${location.pathname}#key=${enc(ctx.key() || "")}`;
  const keyShown = el("code", {}, "••••••••");
  const reveal = el("button", { type: "button", class: "link-button" }, "show");
  reveal.addEventListener("click", () => {
    const hidden = keyShown.textContent.startsWith("•");
    keyShown.textContent = hidden ? ctx.key() : "••••••••";
    reveal.textContent = hidden ? "hide" : "show";
  });
  const copy = (label, text) => action(label, async () => { await navigator.clipboard.writeText(await text()); return "Copied."; },
                                        { result: true });
  const diag = el("pre", { class: "file", hidden: true });
  const viewDiag = action("View", async () => {
    if (!diag.hidden) { diag.hidden = true; return; }
    diag.textContent = JSON.stringify(await ctx.api("diagnostics"), null, 2);
    diag.hidden = false;
  });
  const row = (k, ...v) => el("tr", {}, el("th", {}, k), el("td", {}, ...v));
  const git = a.git ? ` · ${a.git.branch ? a.git.branch + " " : ""}${a.git.commit || ""}` : "";
  return [
    el("h2", {}, "About"),
    a.restart_for ? el("section", { class: "next-step" },
      el("div", {}, el("strong", {}, "Restart to update: "),
        `hpclib ${a.restart_for} is on this machine; this console is still running ${a.hpclib_version}. ` +
        "Quit launch-tunnel-manager and start it again."), null) : null,
    el("section", { class: "card" },
      el("table", { class: "about" }, el("tbody", {},
        row("hpclib", el("strong", {}, a.hpclib_version || "unknown"), a.restart_for ? " " : "",
            a.restart_for ? chip(`${a.restart_for} on disk`, "starting") : chip("current", "up"),
            el("div", { class: "muted small" }, a.hpclib_dir + git)),
        row("Console", `API ${a.console_version} · process ${a.pid} · started ${new Date(a.started * 1000).toLocaleString()}`),
        row("Python", `${a.python} · ${a.python_path}`),
        row("System", a.platform, a.local_platform ? el("div", { class: "muted small" },
          `${a.local_platform.title} (${a.local_platform.source})` +
          (a.local_platform.windows_home ? ` · Windows profile ${a.local_platform.windows_home}` : "")) : null),
        row("Settings", el("code", {}, a.console_dir), el("div", { class: "muted small" }, `clusters: ${a.agents_dir}`)),
        row("Packages", a.packages.length ? a.packages.map((p) => `${p.name} ${p.version}`).join(", ") : el("span", { class: "muted" }, "none")),
        row("Clusters", String(a.clusters))))),
    el("section", { class: "card" },
      el("h3", {}, "This console's link"),
      el("p", { class: "muted" }, "Opens the console in another browser on this machine, already connected. The key " +
        "is new each time the console starts; anyone with it can use the console, so keep it to yourself."),
      el("table", { class: "about" }, el("tbody", {},
        row("Address", el("code", {}, a.url)),
        row("Session key", keyShown, " ", reveal))),
      el("div", { class: "actions" }, copy("Copy link", () => link), copy("Copy address", () => a.url))),
    el("section", { class: "card" },
      el("h3", {}, "Diagnostics"),
      el("p", { class: "muted" }, "Versions, settings and every cluster's state (tunnels, logins, apps, the last " +
        "operation), for a bug report. Tokens, passwords and keys are not included, and long token-like strings " +
        "are hidden; look it over before you send it."),
      el("div", { class: "actions" }, copy("Copy diagnostics", async () => JSON.stringify(await ctx.api("diagnostics"), null, 2)), viewDiag),
      diag),
  ];
}

// ---------------------------------------------------------------- notifications

const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED",
                          "BOOT_FAIL", "DEADLINE"]);
const on = (kind) => !!(prefs && prefs.notifications && prefs.notifications[kind]) &&
  "Notification" in window && Notification.permission === "granted";

export function notify(kind, title, body, hash = null) {
  if (!on(kind)) return;
  const n = new Notification(title, { body, tag: `hpc-${kind}-${title}-${body}` });
  n.onclick = () => { window.focus(); if (hash) location.hash = hash; n.close(); };
}

let watchTimer = null;
let jobsTimer = null;
let lastTunnels = null;      // {at, map key -> entry}
let lastJobs = null;         // map key -> finished

async function checkTunnels() {
  try {
    const w = await ctx.api("watch");
    const map = Object.fromEntries(w.tunnels.map((t) => [`${t.cluster}/${t.app || ""}`, t]));
    if (lastTunnels) {
      for (const [k, t] of Object.entries(map)) {
        const was = lastTunnels.map[k];
        const stoppedHere = t.stopped && t.stopped >= lastTunnels.at - 5;
        if (was && was.up && !t.up && !stoppedHere) {
          notify("tunnel", `${t.title} on ${t.cluster} stopped`, "Its tunnel dropped. Start it again from the cluster's page.",
                 t.app ? `#/c/${enc(t.cluster)}/apps/${enc(t.app)}` : `#/c/${enc(t.cluster)}/agents`);
        }
      }
    }
    lastTunnels = { at: w.at, map };
  } catch { /* not connected: try again */ }
}

async function checkJobs() {
  try {
    const { jobs } = await ctx.api("jobs?limit=50");
    const map = {};
    for (const j of jobs) {
      const k = `${j.cluster}/${j.job_id}`;
      const done = j.terminal === true || TERMINAL.has(j.state);
      map[k] = done;
      if (lastJobs && done && lastJobs[k] === false) {
        const ok = j.state === "COMPLETED";
        notify("job", `${j.label || j.template || "Job " + j.job_id} ${ok ? "finished" : (j.state || "ended").toLowerCase().replaceAll("_", " ")}`,
               `${j.cluster} · job ${j.job_id}${j.token ? ` · ${j.token}` : ""}`, `#/c/${enc(j.cluster)}/agents/activity`);
      }
    }
    lastJobs = map;
  } catch { /* not connected, or no live cluster */ }
}

export function restartWatch() {
  clearInterval(watchTimer); clearInterval(jobsTimer);
  watchTimer = jobsTimer = null;
  if (on("tunnel")) { checkTunnels(); watchTimer = setInterval(checkTunnels, 20000); } else lastTunnels = null;
  if (on("job")) { checkJobs(); jobsTimer = setInterval(checkJobs, 60000); } else lastJobs = null;
}

let seenProposals = null;
export function noteProposals(list) {
  const keys = new Set(list.map((p) => `${p.cluster}/${p.name}/${p.proposed}`));
  if (seenProposals) {
    for (const p of list) {
      if (!seenProposals.has(`${p.cluster}/${p.name}/${p.proposed}`)) {
        notify("proposal", `New template proposal: ${p.name}`, `${p.cluster}, from ${p.token}${p.rationale ? ": " + p.rationale : ""}`,
               "#/proposals");
      }
    }
  }
  seenProposals = keys;
}

const promptsSeen = new Set();
export function notePrompt(p, key) {
  if (promptsSeen.has(key)) return;
  promptsSeen.add(key);
  if (document.visibilityState === "visible" && document.hasFocus()) return;
  notify("prompt", `${p.app ? `${p.title} on ${p.cluster}` : p.cluster} is waiting for you`,
         p.prompt.kind === "hostkey" ? "It asks you to trust a node's host key." : "It asks for your password.");
}
