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
