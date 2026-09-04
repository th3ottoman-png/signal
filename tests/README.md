# tests

Browser-free tests for the dashboard UI. No dependencies, no install step.

```
node tests/run.js          run everything
node tests/run.js state    run files whose name contains "state"
```

Exit code 0 means all assertions passed, 1 means something failed. Tests also
run automatically on every push and schedule via the `test` job in
`.github/workflows/refresh.yml`.

## How it works

`index.html` is a single file with one inline `<script>`. There is no bundler
and no module system, so `harness.js` pulls that script out at run time and
runs it in a Node `vm` with a stubbed DOM.

The script is extracted from `index.html` on every run, never committed as a
copy. A committed copy would go stale the moment the page changed and would
keep passing against code that no longer exists.

## Files

| File | Covers |
|---|---|
| `harness.js` | Script extraction, DOM stubs, boot helper, assert helpers |
| `state.test.js` | The three read/saved state modes, plus the un-mark regression |
| `transfer.test.js` | Export payload shape, import merge semantics, bad input |
| `colour.test.js` | `--item-color` wiring and per-card source colours |
| `run.js` | Discovers `*.test.js`, runs them, aggregates results |

## Writing a test

```js
'use strict';
const { boot } = require('./harness');

// Top level `let` bindings in the page are not reachable from the sandbox
// object, so hand back what you need to inspect. Function declarations like
// `srcColor` are visible directly as sandbox.name.
const EXPOSE = `
globalThis.__peek = () => ({ read: [...STATE.read], mode: STATE_MODE });
`;

module.exports.run = async t => {
  const p = await boot({ expose: EXPOSE });
  t.eq('mode', p.sandbox.__peek().mode, 'local');
};
```

`boot()` accepts `feed`, `api`, `seed` and `local` to drive the state machine
into whichever branch you want. See the doc comment on `boot` in `harness.js`.

## Two traps

**`boot()` is async and nothing awaits it.** Sleep before probing or every
assertion reads the pre-load defaults. The harness already does this, but if
you probe after an action of your own, await `SETTLE()` first.

**A file that asserts nothing fails.** A loop over a key that does not exist
iterates an empty array, so every assertion inside it silently never runs. This
bit once already: `feed.stories` does not exist, the real key is `feed.items`.
The runner treats zero assertions as a failure, and the colour test asserts its
own array lengths before trusting anything.
