// Times and sizes as people write them and as Slurm wants them. Pure functions (no DOM), so <hpc-resources> and the
// Workspace's tests share them; clusterinfo.py has the same rules on the server, which has the last word.
//
//   parseDuration("1d 2h") -> 1560 (minutes)       formatDuration(1560) -> "1-02:00:00"
//   parseMemory("8G") -> 8192 (MB)                 formatMemory(8192) -> "8G"
//
// Both parsers return undefined for what they can't read, and parseDuration returns Infinity for UNLIMITED.

const UNIT_MINUTES = { w: 7 * 1440, d: 1440, h: 60, m: 1, s: 1 / 60 };

export function parseDuration(text) {
  const value = String(text ?? "").trim().toLowerCase();
  if (!value) return undefined;
  if (value === "unlimited" || value === "infinite") return Infinity;
  let m = value.match(/^(\d+)-(\d+)(?::(\d+))?(?::(\d+))?$/);
  if (m) return +m[1] * 1440 + +m[2] * 60 + +(m[3] || 0) + +(m[4] || 0) / 60;
  m = value.match(/^(\d+):(\d+)(?::(\d+))?$/);
  if (m) return m[3] === undefined ? +m[1] + +m[2] / 60 : +m[1] * 60 + +m[2] + +m[3] / 60;
  if (/^\d+$/.test(value)) return +value;
  const rest = value.replace(/(\d+(?:\.\d+)?)\s*([wdhms])[a-z]*/g, "").replace(/[\s,]+|and/g, "");
  const words = [...value.matchAll(/(\d+(?:\.\d+)?)\s*([wdhms])[a-z]*/g)];
  if (words.length && rest === "") return words.reduce((sum, w) => sum + parseFloat(w[1]) * UNIT_MINUTES[w[2]], 0);
  return undefined;
}

export function formatDuration(minutes) {
  if (minutes === Infinity) return "UNLIMITED";
  const seconds = Math.round(minutes * 60);
  const days = Math.floor(seconds / 86400);
  const rest = seconds % 86400;
  const hms = [Math.floor(rest / 3600), Math.floor((rest % 3600) / 60), rest % 60].map((n) => String(n).padStart(2, "0"));
  return days ? `${days}-${hms.join(":")}` : `${+hms[0]}:${hms[1]}:${hms[2]}`;
}

export function parseMemory(text) {
  const m = String(text ?? "").trim().toLowerCase().match(/^(\d+(?:\.\d+)?)\s*([kmgt]?)b?$/);
  if (!m) return undefined;
  return parseFloat(m[1]) * { k: 1 / 1024, "": 1, m: 1, g: 1024, t: 1024 * 1024 }[m[2]];
}

export function formatMemory(mb) {
  mb = Math.round(mb);
  if (mb && mb % (1024 * 1024) === 0) return `${mb / (1024 * 1024)}T`;
  if (mb && mb % 1024 === 0) return `${mb / 1024}G`;
  return `${mb}M`;
}
