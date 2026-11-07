/** Shared node:test/jsdom bootstrap. No browser or real network is permitted. */
import { createRequire, syncBuiltinESMExports } from 'node:module';
import { fileURLToPath } from 'node:url';
import net from 'node:net';
import tls from 'node:tls';
import dns from 'node:dns';
import http from 'node:http';
import https from 'node:https';
import http2 from 'node:http2';
import dgram from 'node:dgram';
import childProcess from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

export class OfflineViolation extends Error {
  constructor() {
    super('next-iterations offline guard blocked real network/subprocess/dotenv');
    this.name = 'OfflineViolation';
  }
}
const refuse = () => { throw new OfflineViolation(); };
net.Socket.prototype.connect = refuse;
net.connect = net.createConnection = refuse;
tls.connect = refuse;
http.request = http.get = https.request = https.get = refuse;
http2.connect = refuse;
dgram.createSocket = refuse;
for (const object of [dns, dns.promises]) {
  for (const key of Object.keys(object)) {
    if (/^(lookup|resolve|reverse)/.test(key) && typeof object[key] === 'function') object[key] = refuse;
  }
}
for (const key of ['spawn', 'spawnSync', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork']) {
  childProcess[key] = refuse;
}
globalThis.fetch = refuse;
globalThis.WebSocket = class { constructor() { refuse(); } };
// ESM imports of Node's builtins must see the replacements as well.
const checkFile = (filename) => {
  if (typeof filename === 'number') return;
  const value = filename instanceof URL ? fileURLToPath(filename) : String(filename);
  const name = path.basename(value);
  if (name === '.env' || name.startsWith('.env.')) refuse();
};
for (const key of ['readFile', 'readFileSync', 'open', 'openSync', 'createReadStream']) {
  const original = fs[key];
  fs[key] = function (filename, ...args) {
    checkFile(filename);
    return original.call(this, filename, ...args);
  };
}
for (const key of ['readFile', 'open']) {
  const original = fs.promises[key];
  fs.promises[key] = function (filename, ...args) {
    checkFile(filename);
    return original.call(this, filename, ...args);
  };
}
syncBuiltinESMExports();

const mobileRequire = createRequire(new URL('../../mobile/package.json', import.meta.url));
const { JSDOM } = mobileRequire('jsdom');
// No resources: 'usable' or runScripts: 'dangerously'. DOM tests explicitly
// replace window.fetch with fixtures when exercising application interactions.
export const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'https://offline.invalid/',
});
export const { window } = dom;
window.fetch = refuse;
window.XMLHttpRequest.prototype.open = refuse;
window.WebSocket = globalThis.WebSocket;
for (const key of ['window', 'document', 'navigator', 'HTMLElement', 'Element', 'Node',
                   'Event', 'CustomEvent', 'DOMParser', 'MutationObserver', 'localStorage',
                   'sessionStorage', 'location', 'history', 'HTMLInputElement', 'HTMLFormElement',
                   'HTMLButtonElement', 'AbortController', 'AbortSignal']) {
  Object.defineProperty(globalThis, key, { configurable: true, value: window[key] });
}
process.env.NEXT_ITERATIONS_OFFLINE = '1';
