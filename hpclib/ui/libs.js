// Third-party libraries (CodeMirror, xterm.js, Wunderbaum), loaded when first needed from the CDN that libs.json pins
// them to. CodeMirror 6 has no ready-made script on npm, so a bundle built from pinned packages (vendor/_build/build.sh) ships
// with hpclib itself: a library with "local" in libs.json loads from there, hash-checked like the rest.
//
//   import { loadLib, configureLibs } from "hpclib-ui/index.js";
//   configureLibs({ cdn: "https://my.mirror/npm" });          // optional: another npm mirror
//   const { CM6 } = await loadLib("codemirror");              // the globals the library defines (and what it needs)
//
// libs.json names each library's package, exact version, files and their sha384 (update_libs.py writes it). From
// the default CDN, or from `vendor` (a directory laid out like the CDN: PACKAGE@VERSION/FILE, which a package can
// carry for sites whose browsers can't reach a CDN), a file is loaded with `integrity`, so the page runs exactly
// the bytes that were checked. From any other mirror the hashes are left out, since a mirror may serve other bytes.

export const DEFAULT_CDN = "https://cdn.jsdelivr.net/npm";

const settings = { cdn: DEFAULT_CDN, vendor: null };
let manifest = null;
const loading = new Map();

/** cdn: an npm mirror's base URL; vendor: a base URL (or path) holding PACKAGE@VERSION/ directories. */
export function configureLibs({ cdn, vendor } = {}) {
  if (cdn) settings.cdn = String(cdn).replace(/\/+$/, "");
  if (vendor !== undefined) settings.vendor = vendor ? String(vendor).replace(/\/+$/, "") : null;
}

async function manifestOnce() {
  manifest = manifest || fetch(new URL("./libs.json", import.meta.url), { cache: "no-cache" })
    .then((res) => { if (!res.ok) throw new Error(`libs.json: ${res.status}`); return res.json(); });
  return manifest;
}

function attach(node) {
  return new Promise((resolve, reject) => {
    node.onload = resolve;
    node.onerror = () => reject(new Error(`couldn't load ${node.src || node.href}`));
    document.head.append(node);
  });
}

function fileNode(kind, url, hash) {
  const checked = hash && (settings.vendor || settings.cdn === DEFAULT_CDN);
  const node = document.createElement(kind === "style" ? "link" : "script");
  if (kind === "style") { node.rel = "stylesheet"; node.href = url; } else node.src = url;
  node.crossOrigin = "anonymous";
  if (checked) node.integrity = hash;
  return node;
}

/** Loads a library (and what it needs); resolves with {GLOBAL: window.GLOBAL} for each of them. */
export function loadLib(name) {
  if (!loading.has(name)) {
    loading.set(name, (async () => {
      const lib = (await manifestOnce())[name];
      if (!lib || typeof lib !== "object") throw new Error(`no library ${name}`);
      const globals = {};
      const base = `${settings.vendor || settings.cdn}/${lib.package}@${lib.version}`;   // (not used by a local library)
      const url = (file) => `${base}/${file}`;
      await Promise.all((lib.needs || []).map((n) => loadLib(n).then((g) => Object.assign(globals, g))));
      await Promise.all((lib.wants || []).map((n) => loadLib(n).then((g) => Object.assign(globals, g), () => null)));
      await Promise.all((lib.styles || []).map((f) => attach(fileNode("style", url(f), lib.integrity[f])).catch(() => null)));
      if (lib.local) {   // shipped beside this file, so the hash always applies
        const node = fileNode("script", new URL(lib.local, import.meta.url).href, lib.integrity[lib.local]);
        node.integrity = lib.integrity[lib.local];
        await attach(node);
      }
      for (const f of lib.scripts || []) await attach(fileNode("script", url(f), lib.integrity[f]));
      // these add to the library (modes, addons): one that doesn't load is left out
      await Promise.all((lib.after || []).map((f) => attach(fileNode("script", url(f), lib.integrity[f])).catch(() => null)));
      globals[lib.global] = window[lib.global];
      return globals;
    })().catch((err) => { loading.delete(name); throw err; }));
  }
  return loading.get(name);
}
