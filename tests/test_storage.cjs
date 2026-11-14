'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const clientPath = path.resolve(__dirname, '../skills/smartbook-author/assets/smartbook-storage.js');
const source = fs.readFileSync(clientPath, 'utf8');

function createEnvironment({ framed = true, blockedStorage = false } = {}) {
  const timers = new Map();
  const listeners = new Set();
  const sent = [];
  const storageValues = new Map();
  let nextTimer = 1;
  const parent = { postMessage(message, target) { sent.push({ message, target }); } };
  const window = {
    parent: framed ? parent : null,
    addEventListener(type, handler) { if (type === 'message') listeners.add(handler); },
    removeEventListener(type, handler) { if (type === 'message') listeners.delete(handler); },
  };
  if (!framed) window.parent = window;
  const localStorage = {
    getItem(key) {
      if (blockedStorage) throw new Error('storage blocked');
      return storageValues.has(key) ? storageValues.get(key) : null;
    },
    setItem(key, value) {
      if (blockedStorage) throw new Error('storage blocked');
      storageValues.set(key, String(value));
    },
    removeItem(key) {
      if (blockedStorage) throw new Error('storage blocked');
      storageValues.delete(key);
    },
  };
  function schedule(fn, delay, repeat) {
    const id = nextTimer++;
    timers.set(id, { fn, delay, repeat });
    return id;
  }
  const context = {
    window,
    localStorage,
    setTimeout: (fn, delay) => schedule(fn, delay, false),
    setInterval: (fn, delay) => schedule(fn, delay, true),
    clearTimeout: (id) => timers.delete(id),
    clearInterval: (id) => timers.delete(id),
  };
  vm.runInNewContext(source, context, { filename: clientPath });
  return {
    storage: window.SmartbookStorage,
    parent,
    sent,
    storageValues,
    dispatch(event) { for (const listener of Array.from(listeners)) listener(event); },
    runTimers(delay) {
      for (const [id, timer] of Array.from(timers)) {
        if (timer.delay !== delay || !timers.has(id)) continue;
        if (!timer.repeat) timers.delete(id);
        timer.fn();
      }
    },
    listeners,
  };
}

test('framed handshake accepts only a matching response from the parent', async () => {
  const env = createEnvironment();
  assert.equal(env.sent.length, 1);
  const request = env.sent[0].message.request;
  assert.equal(env.sent[0].message.channel, 'smartbook-storage-v1');
  assert.equal(env.sent[0].message.action, 'read');
  env.dispatch({ source: {}, data: { channel: 'smartbook-storage-v1', action: 'snapshot', request, values: { theme: 'forged' } } });
  env.dispatch({ source: env.parent, data: { channel: 'smartbook-storage-v1', action: 'snapshot', request: 'wrong', values: { theme: 'forged' } } });
  assert.equal(env.storage.getItem('theme'), null);
  env.dispatch({ source: env.parent, data: { channel: 'smartbook-storage-v1', action: 'snapshot', request, values: { theme: 'dark', '__proto__': 'bad', 'bad key': 'bad', huge: 'x'.repeat(4097) } } });
  await env.storage.ready;
  assert.equal(env.storage.getItem('theme'), 'dark');
  assert.equal(env.storage.getItem('bad key'), null);
  assert.equal(env.storage.getItem('huge'), null);
  assert.equal(env.listeners.size, 0);
});

test('framed writes are debounced and remove cancels a pending write', async () => {
  const env = createEnvironment();
  const request = env.sent[0].message.request;
  env.dispatch({ source: env.parent, data: { channel: 'smartbook-storage-v1', action: 'snapshot', request, values: {} } });
  await env.storage.ready;
  env.storage.setItem('theme', 'light');
  env.storage.setItem('theme', 'dark');
  assert.equal(env.storage.getItem('theme'), 'dark');
  assert.equal(env.sent.length, 1);
  env.runTimers(200);
  assert.equal(env.sent.length, 2);
  assert.equal(env.sent[1].message.action, 'set');
  assert.equal(env.sent[1].message.key, 'theme');
  assert.equal(env.sent[1].message.value, 'dark');
  env.storage.setItem('theme', 'light');
  env.storage.removeItem('theme');
  assert.equal(env.storage.getItem('theme'), null);
  assert.equal(env.sent[2].message.action, 'remove');
  env.runTimers(200);
  assert.equal(env.sent.length, 3);
});

test('invalid keys and oversized values are ignored', async () => {
  const env = createEnvironment();
  env.runTimers(4000);
  await env.storage.ready;
  for (const key of ['', 'bad key', '__proto__', 'prototype', 'constructor', 'a'.repeat(65)]) {
    env.storage.setItem(key, 'value');
    env.storage.removeItem(key);
    assert.equal(env.storage.getItem(key), null);
  }
  env.storage.setItem('valid', 'x'.repeat(4097));
  env.runTimers(200);
  assert.equal(env.storage.getItem('valid'), null);
  assert.equal(env.sent.length, 1);
});

test('standalone mode reads, writes and removes local values', async () => {
  const env = createEnvironment({ framed: false });
  await env.storage.ready;
  env.storage.setItem('progress', 2);
  assert.equal(env.storage.getItem('progress'), '2');
  env.storage.removeItem('progress');
  assert.equal(env.storage.getItem('progress'), null);
  assert.equal(env.sent.length, 0);
});

test('standalone mode tolerates unavailable local storage', async () => {
  const env = createEnvironment({ framed: false, blockedStorage: true });
  await env.storage.ready;
  assert.doesNotThrow(() => env.storage.setItem('progress', '2'));
  assert.equal(env.storage.getItem('progress'), null);
  assert.doesNotThrow(() => env.storage.removeItem('progress'));
});
