// State regressions; DOM interactions are covered in test_workspace_ui.py.
// Run with: node --test tests/test_file_browser.mjs
import { test } from "node:test";
import assert from "node:assert/strict";

// the element's module needs these defined (core.js builds a stylesheet when it loads); nothing else of a browser
globalThis.HTMLElement = class {};
globalThis.CSSStyleSheet = class { replaceSync() {} };
globalThis.customElements = { get: () => undefined, define: () => {} };
const { HpcFileBrowser: Browser } = await import("../hpclib/ui/components/file-browser.js");
const listing = (path, names = []) => ({ path, entries: names.map((name) => ({ name, type: "file" })) });
function browser(list) {
  const value = new Browser();
  value.options = { list };
  value.draw = () => {};
  return value;
}

test("a slow directory cannot replace a newer navigation or its address", async () => {
  let finish;
  const value = browser((path) => path === "/slow" ? new Promise((resolve) => { finish = resolve; }) : listing(path));
  const seen = [];
  value.options.onDirectory = (out) => seen.push(out.path);
  const slow = value.navigate("/slow");
  await Promise.resolve();
  await value.navigate("/new");
  finish(listing("/slow")); await slow;
  assert.equal(value.listing.path, "/new");
  assert.deepEqual(seen, ["/new"]);
});

test("hidden-file changes discard in-flight listings from the old scope", async () => {
  let finish;
  const value = browser(() => new Promise((resolve) => { finish = resolve; }));
  const old = value.load("/project");
  await Promise.resolve();
  value.invalidate();
  value.options.list = () => listing("/project", [".hidden"]);
  await value.load("/project");
  finish(listing("/project", ["visible"])); await old;
  assert.equal(value.cache.get("/project").entries[0].name, ".hidden");
});

test("opening a file cancels a pending folder selection without cancelling expansion", async () => {
  let finish;
  const value = browser((path) => path === "/slow" ? new Promise((resolve) => { finish = resolve; }) : listing(path));
  value.select = (path) => { value.selected = path; };
  const opened = [];
  value.options.onOpen = (entry) => opened.push(entry.path);
  await value.navigate("/");
  const slow = value.navigate("/slow");
  await Promise.resolve();
  await value.open({ path: "/notes.txt", name: "notes.txt", type: "file" });
  finish(listing("/slow")); await slow;
  assert.equal(value.listing.path, "/");
  assert.equal(value.selected, "/notes.txt");
  assert.deepEqual(opened, ["/notes.txt"]);
  assert.ok(value.cache.has("/slow"));
});

test("opening a branch shares the existing request and loads no grandchildren", async () => {
  let calls = 0;
  const value = browser((path) => { calls++; return { path, entries: [{ name: "sub", type: "directory" }] }; });
  const [first, second] = await Promise.all([value.load("/project"), value.load("/project")]);
  assert.equal(calls, 1); assert.equal(first, second);
  const node = value.node(first.entries[0]);
  assert.equal(node.lazy, true); assert.equal(node.children, undefined); assert.equal(calls, 1);
});

test("failed listings can be retried without replacing the current folder", async () => {
  const value = browser((path) => listing(path));
  await value.navigate("/good");
  value.options.list = () => { throw new Error("Permission denied"); };
  await assert.rejects(value.navigate("/denied"), /Permission denied/);
  assert.equal(value.listing.path, "/good");
  value.options.list = (path) => listing(path);
  await value.navigate("/denied");
  assert.equal(value.listing.path, "/denied");
});

test("refresh fetches changed directory contents and keeps expansion state", async () => {
  let names = ["old"];
  const value = browser((path) => listing(path, names));
  await value.navigate("/project");
  value.expanded.add("/project/sub"); names = ["new"];
  await value.navigate("/project", { refresh: true });
  assert.equal(value.listing.entries[0].name, "new");
  assert.ok(value.expanded.has("/project/sub"));
});

test("refresh retains a selected file that still exists", async () => {
  const value = browser((path) => listing(path, ["notes.txt"]));
  await value.navigate("/project");
  value.selected = "/project/notes.txt";
  await value.navigate("/project", { refresh: true });
  assert.equal(value.selected, "/project/notes.txt");
});

test("opening a file in an expanded branch updates the toolbar directory", async () => {
  const value = browser((path) => listing(path, ["notes.txt"]));
  value.select = (path) => { value.selected = path; };
  value.drawCrumbs = value.updateStatus = () => {};
  value.options.onOpen = () => {};
  await value.navigate("/project");
  await value.load("/project/sub");
  const directories = [];
  value.options.onDirectory = (out) => directories.push(out.path);
  await value.open({ path: "/project/sub/notes.txt", name: "notes.txt", type: "file" });
  assert.equal(value.listing.path, "/project/sub");
  assert.deepEqual(directories, ["/project/sub"]);
});

test("wildcard filters work the shell way, ignore case, and take several patterns", async () => {
  const { globs } = await import("../hpclib/ui/components/file-browser.js");
  assert.equal(globs("  "), null);
  const m = globs("*.py run_?.sh [a-c]x* , notes");
  for (const yes of ["a.py", "RUN_1.sh", "bx.txt", "my-notes.md", "x.PY"]) assert.ok(m(yes), yes);
  for (const no of ["run_12.sh", "dx.txt", "a.pyc", "readme"]) assert.ok(!m(no), no);
  assert.ok(globs("[!a]*")("b") && !globs("[!a]*")("ab"));
  assert.ok(globs("a.b")("a.b") && !globs("a.b*")("axb"));       // a dot is a dot
  assert.ok(globs("(x)+")("(x)+") && globs("[")("[") && !globs("[")("a[b"));          // stray punctuation is literal
});
