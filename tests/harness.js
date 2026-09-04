'use strict';

// Shared test scaffolding.
//
// The page under test is a single index.html with one inline <script>. There is
// no bundler, no modules and no npm install, so these tests load that script
// into a Node vm with a stubbed DOM and poke at it directly.
//
// Two things here are load bearing. Read the comments before changing them.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const INDEX_HTML = path.join(ROOT, 'index.html');
const FEED_JSON = path.join(ROOT, 'data', 'feed.json');

// Extracted at run time, never committed as a copy. A committed copy would go
// stale the moment index.html changed and would keep passing against code that
// no longer exists, which is worse than having no test.
function pageScript() {
  const html = fs.readFileSync(INDEX_HTML, 'utf8');
  const tags = html.match(/<script[^>]*>/g) || [];
  if (tags.length !== 1) {
    throw new Error(`expected exactly 1 <script> in index.html, found ${tags.length}`);
  }
  const m = html.match(/<script[^>]*>([\s\S]*?)<\/script>/);
  if (!m) throw new Error('index.html has no inline <script>, nothing to test');
  return m[1];
}

// Minimal feed so a boot does not need the real one. Shape must match what
// fetch.py writes: `items`, not `stories`.
function feedFixture() {
  return {
    generated_at: new Date().toISOString(),
    site: { title: 'Signal' },
    stats: { items: 2, sources: 2 },
    items: [
      { url: 'https://example.test/1', title: 'A1', source: 'Hacker News', published: new Date().toISOString(), category: 'news', score: 3 },
      { url: 'https://example.test/2', title: 'A2', source: 'TechCrunch', published: new Date().toISOString(), category: 'news', score: 2 }
    ],
    repos: [],
    releases: []
  };
}

// The committed feed. Returns null when absent so a caller can skip rather than
// fail on a clone that has not run fetch.py yet.
function realFeed() {
  if (!fs.existsSync(FEED_JSON)) return null;
  return JSON.parse(fs.readFileSync(FEED_JSON, 'utf8'));
}

function stubEl(id) {
  return {
    id,
    textContent: '', innerHTML: '', title: '', value: '',
    files: null, dataset: {}, style: {}, href: '', download: '',
    classList: { add() {}, remove() {}, toggle() { return false; }, contains() { return false; } },
    addEventListener() {}, removeEventListener() {},
    appendChild() {}, remove() {}, click() {}, focus() {}, blur() {},
    closest() { return null; },
    querySelector() { return stubEl('nested'); },
    querySelectorAll() { return []; }
  };
}

/**
 * Boot index.html's script in a fresh vm context.
 *
 * @param {object}  [opts.feed]   feed served at data/feed.json, defaults to a fixture
 * @param {object}  [opts.api]    truthy -> GET api/state answers, null -> 404 (static host)
 * @param {object}  [opts.seed]   truthy -> data/state.json answers, null -> 404
 * @param {object}  [opts.local]  pre-populates localStorage under "signal_state"
 * @param {string}  [opts.expose] JS appended inside the context, see the note below
 * @param {fn}      [opts.onElement] called with every element createElement returns
 *
 * `expose` exists because top level `let` and `const` in the page land in the
 * context's lexical scope, not on the sandbox object. `sandbox.STATE` is
 * therefore undefined even though the script can see STATE fine. Appending
 * code that assigns to globalThis is the only way to reach those bindings from
 * outside. Function declarations like `srcColor` are visible directly.
 */
async function boot(opts = {}) {
  const {
    feed = feedFixture(),
    api = null,
    seed = null,
    local = null,
    expose = '',
    onElement = null
  } = opts;

  const els = {};
  const getEl = id => (els[id] = els[id] || stubEl(id));
  const store = new Map();
  if (local) store.set('signal_state', JSON.stringify(local));

  const created = [];
  const blobs = [];

  const fetchStub = async (url, init) => {
    if (url === 'api/state') {
      if (init && init.method === 'POST') return { ok: true, json: async () => ({ ok: true }) };
      return api ? { ok: true, json: async () => api } : { ok: false, status: 404 };
    }
    if (url === 'api/status') return { ok: false, status: 404 };
    if (url === 'data/state.json') {
      return seed ? { ok: true, json: async () => seed } : { ok: false, status: 404 };
    }
    if (url === 'data/feed.json') {
      return feed ? { ok: true, json: async () => feed } : { ok: false, status: 404 };
    }
    return { ok: false, status: 404 };
  };

  const sandbox = {
    // Not present in a bare vm context, so they must be handed in. Everything
    // else (Math, JSON, Set, Promise) is a context intrinsic and is omitted.
    console,
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval,
    fetch: fetchStub,
    Blob: class { constructor(parts) { this._text = parts.join(''); blobs.push(this._text); } },
    URL: { createObjectURL: () => 'blob:test', revokeObjectURL() {} },
    FileReader: class {
      readAsText(file) {
        setTimeout(() => {
          if (file && file.__fail) { if (this.onerror) this.onerror(); return; }
          this.result = file && file.__text;
          if (this.onload) this.onload();
        }, 0);
      }
    },
    document: {
      // Memoised: renderStateWhere writes to #state-where and the toast probe
      // reads it back, so the same object has to come back each time.
      getElementById: getEl,
      // Must return a stub, never null. The page calls addEventListener on the
      // result at load time, so null throws before anything can be tested.
      querySelector: () => stubEl('stub'),
      querySelectorAll: () => [],
      createElement: () => {
        const el = stubEl('created');
        created.push(el);
        if (onElement) onElement(el);
        return el;
      },
      addEventListener() {},
      body: { appendChild() {} },
      activeElement: null
    },
    window: { open() {} },
    localStorage: {
      getItem: k => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: k => store.delete(k)
    }
  };
  sandbox.globalThis = sandbox;

  vm.createContext(sandbox);
  vm.runInContext(pageScript() + '\n' + expose, sandbox);

  // boot() is async and nothing awaits it, so give the microtask queue and the
  // fetch stubs a beat to settle. Probing immediately reads the pre-load
  // defaults and every assertion passes or fails for the wrong reason.
  await new Promise(r => setTimeout(r, 120));

  return {
    sandbox,
    el: getEl,
    created,
    blobs,
    local: () => (store.has('signal_state') ? JSON.parse(store.get('signal_state')) : null)
  };
}

const SETTLE = () => new Promise(r => setTimeout(r, 30));

function createReporter(name) {
  const rows = [];
  const api = {
    section(title) { console.log(`\n  ${title}`); },
    eq(label, actual, expected) {
      const a = JSON.stringify(actual);
      const e = JSON.stringify(expected);
      const ok = a === e;
      rows.push({ ok, label });
      console.log(`  ${ok ? 'pass' : 'FAIL'}  ${label}`);
      if (!ok) console.log(`          expected ${e}\n          actual   ${a}`);
    },
    ok(label, cond, detail) {
      const ok = !!cond;
      rows.push({ ok, label });
      console.log(`  ${ok ? 'pass' : 'FAIL'}  ${label}`);
      if (!ok && detail !== undefined) console.log(`          ${detail}`);
    },
    fail(label, detail) {
      rows.push({ ok: false, label });
      console.log(`  FAIL  ${label}`);
      if (detail !== undefined) console.log(`          ${detail}`);
    },
    skip(label, why) {
      console.log(`  skip  ${label}${why ? ' (' + why + ')' : ''}`);
    },
    summary() {
      const total = rows.length;
      const failed = rows.filter(r => !r.ok).length;
      return { total, failed };
    }
  };
  return api;
}

module.exports = {
  ROOT, INDEX_HTML, FEED_JSON,
  pageScript, feedFixture, realFeed, stubEl, boot, SETTLE, createReporter
};
