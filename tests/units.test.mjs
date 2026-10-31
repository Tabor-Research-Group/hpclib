// The browser's time and size parsers (hpclib/ui/units.js) agree with clusterinfo.py's: run with `node --test tests/`.
import test from "node:test";
import assert from "node:assert/strict";
import { parseDuration, formatDuration, parseMemory, formatMemory } from "../hpclib/ui/units.js";

test("durations", () => {
  for (const [text, minutes] of [["6h", 360], ["1d 2h", 1560], ["90m", 90], ["1h30m", 90], ["6:00:00", 360], ["1-02:00:00", 1560], ["1-02", 1560], ["45", 45]]) {
    assert.equal(parseDuration(text), minutes, text);
  }
  assert.equal(parseDuration("UNLIMITED"), Infinity);
  for (const bad of ["", "soon", "6x", "1h banana"]) assert.equal(parseDuration(bad), undefined, bad);
  assert.equal(formatDuration(1560), "1-02:00:00");
  assert.equal(formatDuration(90), "1:30:00");
});

test("memory", () => {
  assert.equal(parseMemory("8G"), 8192);
  assert.equal(formatMemory(parseMemory("8 GB")), "8G");
  assert.equal(formatMemory(500), "500M");
  assert.equal(parseMemory("lots"), undefined);
});
