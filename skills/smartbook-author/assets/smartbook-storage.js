(function () {
  'use strict';
  const channel = 'smartbook-storage-v1';
  const values = Object.create(null);
  const pending = Object.create(null);
  const validKey = (key) => typeof key === 'string' && /^[a-zA-Z0-9_.:-]{1,64}$/.test(key) &&
    !['__proto__', 'prototype', 'constructor'].includes(key);
  const framed = window.parent !== window;
  let ready;
  if (framed) {
    const request = Math.random().toString(36).slice(2) + Date.now().toString(36);
    ready = new Promise((resolve) => {
      let finished = false;
      const finish = (data) => {
        if (finished) return;
        finished = true;
        clearInterval(retry);
        clearTimeout(deadline);
        window.removeEventListener('message', onMessage);
        if (data && typeof data === 'object' && !Array.isArray(data)) {
          Object.keys(data).forEach((key) => {
            if (validKey(key) && typeof data[key] === 'string' && data[key].length <= 4096) values[key] = data[key];
          });
        }
        resolve();
      };
      const onMessage = (event) => {
        const data = event.data;
        if (event.source === window.parent && data && data.channel === channel && data.action === 'snapshot' &&
            data.request === request) finish(data.values);
      };
      const ask = () => window.parent.postMessage({ channel, action: 'read', request }, '*');
      window.addEventListener('message', onMessage);
      const retry = setInterval(ask, 300);
      const deadline = setTimeout(() => finish(null), 4000);
      ask();
    });
  } else {
    ready = Promise.resolve();
  }
  window.SmartbookStorage = {
    ready,
    getItem(key) {
      if (!validKey(key)) return null;
      if (!framed) { try { return localStorage.getItem(key); } catch (_) { return null; } }
      return Object.prototype.hasOwnProperty.call(values, key) ? values[key] : null;
    },
    setItem(key, value) {
      value = String(value);
      if (!validKey(key) || value.length > 4096) return;
      if (!framed) { try { localStorage.setItem(key, value); } catch (_) {} return; }
      values[key] = value;
      clearTimeout(pending[key]);
      pending[key] = setTimeout(() => {
        delete pending[key];
        window.parent.postMessage({ channel, action: 'set', key, value }, '*');
      }, 200);
    },
    removeItem(key) {
      if (!validKey(key)) return;
      if (!framed) { try { localStorage.removeItem(key); } catch (_) {} return; }
      delete values[key];
      clearTimeout(pending[key]);
      delete pending[key];
      window.parent.postMessage({ channel, action: 'remove', key }, '*');
    },
  };
})();
