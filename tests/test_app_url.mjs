// appUrl() in agent-console/components.js: an app that asked for it (theme_link) gets the console's theme and address
// after a #, which no server sees; every other app's link is left alone.
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import path from 'node:path';
const source = fs.readFileSync(path.join(import.meta.dirname, '../agent-console/components.js'), 'utf8');
const start = source.indexOf('export function appUrl');
const end = source.indexOf('export function formatBytes');
assert.ok(start > 0 && end > start, 'appUrl is in components.js');
const body = source.slice(start, end).replace('export function', 'function');
const make = (theme) => vm.runInNewContext(body + '; appUrl', {
  document: { documentElement: { dataset: theme ? { theme } : {} } },
  location: { origin: 'http://127.0.0.1:8650' },
  URLSearchParams,
});
const url = 'http://127.0.0.1:8640/?token=abc';
assert.equal(make('dark')(url, { theme_link: true }), url + '#theme=dark&console=http%3A%2F%2F127.0.0.1%3A8650');
assert.equal(make(null)(url, { theme_link: true }), url + '#theme=system&console=http%3A%2F%2F127.0.0.1%3A8650');
assert.equal(make('dark')(url, {}), url, 'an app that did not ask is left alone');
assert.equal(make('dark')(url), url);
assert.equal(make('dark')(url + '#x', { theme_link: true }), url + '#x', 'an address with a fragment is left alone');
assert.equal(make('dark')(null, { theme_link: true }), null);
console.log('PASS: appUrl adds the theme and console address only for apps with theme_link');
