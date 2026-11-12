// Run from the repository root: node --test tests/test_agent_tunnel_start_ui.mjs
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { test } from 'node:test';

const source = fs.readFileSync(new URL('../agent-console/cluster.js', import.meta.url), 'utf8');
const start = source.indexOf('async function runStep(');
const end = source.indexOf('\nfunction stepButton(', start);
assert.ok(start >= 0 && end > start, 'native runStep must exist');

function fixture(api) {
  const scope = { ctx: { api, render: () => { scope.renders++; } }, renders: 0,
    path: (name, rest) => `clusters/${encodeURIComponent(name)}/${rest}` };
  vm.createContext(scope);
  vm.runInContext(source.slice(start, end), scope);
  return scope;
}

test('Start agent tunnel waits for Stop to finish before starting', async () => {
  const calls = [];
  let finishStop;
  const stopped = new Promise(resolve => { finishStop = resolve; });
  const scope = fixture(async (route, options) => {
    calls.push({ route, options: JSON.parse(JSON.stringify(options)) });
    if (route.endsWith('/stop')) await stopped;
  });
  const pending = scope.runStep({ name: 'me@cluster.example' }, 'start');
  assert.deepEqual(calls, [{ route: 'clusters/me%40cluster.example/tunnel/stop', options: { method: 'POST' } }]);
  assert.equal(scope.renders, 0);
  finishStop();
  await pending;
  assert.deepEqual(calls[1], { route: 'clusters/me%40cluster.example/tunnel/start', options: { method: 'POST', body: {} } });
  assert.equal(calls.length, 2);
  assert.equal(scope.renders, 1);
});

test('Stop failure prevents Start and preserves the original error', async () => {
  const calls = [], error = new Error('stopping the agent tunnel did not finish');
  const scope = fixture(async route => { calls.push(route); throw error; });
  await assert.rejects(scope.runStep({ name: 'demo' }, 'start'), e => e === error);
  assert.deepEqual(calls, ['clusters/demo/tunnel/stop']);
  assert.equal(scope.renders, 0);
});

test('Start failure is reported after a successful Stop', async () => {
  const calls = [], error = new Error('start failed');
  const scope = fixture(async route => {
    calls.push(route);
    if (route.endsWith('/start')) throw error;
  });
  await assert.rejects(scope.runStep({ name: 'demo' }, 'start'), e => e === error);
  assert.deepEqual(calls, ['clusters/demo/tunnel/stop', 'clusters/demo/tunnel/start']);
  assert.equal(scope.renders, 0);
});

test('The standalone Stop action remains a single stop request', async () => {
  const calls = [];
  const scope = fixture(async route => { calls.push(route); });
  await scope.runStep({ name: 'demo' }, 'stop');
  assert.deepEqual(calls, ['clusters/demo/tunnel/stop']);
  assert.equal(scope.renders, 1);
});

function followFixture(api) {
  let id = 0;
  const timers = new Map(), updates = [];
  const scope = { ctx: { api, render: () => updates.push('render') }, location: { hash: '#/c/demo/jobs' },
    path: (name) => `clusters/${name}`, tunnelStarting: c => ['starting', 'queued'].includes(c.tunnel?.state),
    setTimeout: (fn, delay) => { timers.set(++id, { fn, delay }); return id; },
    clearTimeout: key => timers.delete(key) };
  vm.createContext(scope);
  const begin = source.indexOf('let followTimer = null;');
  const finish = source.indexOf('\nasync function runStep(', begin);
  vm.runInContext(source.slice(begin, finish).replace('export function stopFollowing', 'function stopFollowing'), scope);
  return { scope, updates, timers,
    start: c => scope.followClusters([structuredClone(c)], now => updates.push(structuredClone(now[0]))),
    tick: async () => { assert.equal(timers.size, 1); const [key, timer] = timers.entries().next().value;
      timers.delete(key); await timer.fn(); },
    delay: () => timers.values().next().value?.delay };
}

test('Stable tunnels keep polling, report installed version changes, failures and recovery', async () => {
  const c = { name: 'demo', login: { state: 'connected' },
    tunnel: { state: 'up', hpclib_version: '1.14.2', hpclib_version_on_disk: '1.14.2' } };
  let failed = false;
  const f = followFixture(async () => { if (failed) throw new Error('offline'); return structuredClone(c); });
  f.start(c);
  assert.equal(f.delay(), 30000);
  await f.tick();
  assert.equal(f.updates.length, 0, 'unchanged status does not redraw');
  c.tunnel.hpclib_version_on_disk = '1.14.5';
  await f.tick();
  assert.equal(f.updates.at(-1).tunnel.hpclib_version, '1.14.2');
  assert.equal(f.updates.at(-1).tunnel.hpclib_version_on_disk, '1.14.5');
  for (const state of ['down', 'starting', 'up']) {
    c.tunnel.state = state;
    await f.tick();
    assert.equal(f.updates.at(-1).tunnel.state, state);
    assert.equal(f.delay(), state === 'starting' ? 5000 : 30000);
  }
  failed = true;
  await f.tick();
  assert.equal(f.updates.at(-1).tunnel.state, 'error');
  assert.match(f.updates.at(-1).tunnel.error, /offline/);
  failed = false;
  await f.tick();
  assert.equal(f.updates.at(-1).tunnel.state, 'up');
});

test('Polling does not overlap or update a page after navigation or a new render', async () => {
  for (const stop of [f => f.scope.stopFollowing(), f => { f.scope.location.hash = '#/menu/settings'; }]) {
    let finish;
    const f = followFixture(() => new Promise(resolve => { finish = resolve; }));
    f.start({ name: 'demo', tunnel: { state: 'up' } });
    const pending = f.tick();
    assert.equal(f.timers.size, 0, 'next check waits for this response');
    stop(f);
    finish({ name: 'demo', tunnel: { state: 'down' } });
    await pending;
    assert.equal(f.updates.length, 0);
    assert.equal(f.timers.size, 0);
  }
});

test('Cluster page updates its header without replacing the body and follows row updates', async () => {
  const c = { name: 'demo', login: { state: 'connected' }, tunnel: { state: 'up' } };
  let rowUpdate;
  const body = { unsaved: 'keep this', querySelectorAll: () => [{
    addEventListener: (kind, fn) => { assert.equal(kind, 'update'); rowUpdate = fn; }
  }] };
  const f = followFixture(async route => route === 'health' ? { hpclib_version: '1.14.5' } : structuredClone(c));
  Object.assign(f.scope, {
    el: (tag, props, ...children) => ({ tag, props, children,
      replaceChildren(...next) { this.children = next; } }),
    chip: (label, tone, title) => ({ label, tone, title }), action: label => ({ label }),
    login: c => c.login?.state === 'connected', tunnelTone: c => c.tunnel.state,
    overview: () => body, jobsTab: () => body, appsTab: () => body, installed: () => body,
    storageTab: () => body, files: () => body, sshTab: () => body, agents: () => body,
    settings: () => body, widgetTab: () => body,
  });
  const begin = source.indexOf('export async function clusterPage(');
  const finish = source.indexOf('\nasync function overview(', begin);
  vm.runInContext(source.slice(begin, finish).replace('export async function', 'async function'), f.scope);
  const [head, content] = await f.scope.clusterPage('demo', 'jobs');
  assert.equal(content, body);
  const chips = head.children[1];
  assert.equal(chips.children[1].label, 'agent tunnel up');
  c.tunnel.state = 'down';
  await f.tick();
  assert.equal(chips.children[1].label, 'agent tunnel down');
  assert.equal(body.unsaved, 'keep this');
  assert.deepEqual(f.updates, [], 'header-only update does not rerender the page');
  rowUpdate({ detail: { ...c, tunnel: { state: 'up' } } });
  assert.equal(chips.children[1].label, 'agent tunnel up', 'Start/Stop row updates also update the badge');
});

test('Version labels and next steps distinguish installation from the running tunnel', () => {
  const components = fs.readFileSync(new URL('../agent-console/components.js', import.meta.url), 'utf8');
  const begin = components.indexOf('export function hpclibVersionInfo(');
  const end = components.indexOf('\nexport class HpcClusterRow', begin);
  const scope = { login: () => true, tunnelStarting: () => false, tunnelUp: () => true };
  vm.createContext(scope);
  vm.runInContext(components.slice(begin, end).replace('export function', 'function'), scope);
  const nextBegin = source.indexOf('function versionCompare(');
  const nextEnd = source.indexOf('\nfunction size(', nextBegin);
  vm.runInContext(source.slice(nextBegin, nextEnd), scope);
  const stepBegin = source.indexOf('function nextStep('), stepEnd = source.indexOf('\n// the step', stepBegin);
  vm.runInContext(source.slice(stepBegin, stepEnd), scope);
  const tunnel = { state: 'up', hpclib_version: '1.14.2', hpclib_version_on_disk: '1.14.5' };
  assert.match(scope.hpclibVersionInfo(tunnel, '1.14.5').label, /1.14.2 running.*1.14.5 installed/);
  assert.equal(scope.nextStep({ tunnel }, '1.14.5', () => {})[0], 'Restart agent tunnel');
  assert.equal(scope.nextStep({ tunnel, set_up: true }, '1.14.6', () => {})[0], 'Update hpclib');
  delete tunnel.hpclib_version_on_disk;
  assert.equal(scope.hpclibVersionInfo(tunnel, '1.14.5').restart, false, 'older servers stay compatible');
  assert.equal(scope.nextStep({ tunnel }, '1.14.5', () => {})[0], 'Update hpclib');
});
