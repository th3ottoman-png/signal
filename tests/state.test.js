'use strict';

// Read/saved state has three modes: server, seeded, local. See invariant 6 in
// the skill. Each scenario below boots a fresh vm with a different combination
// of backend, committed seed and localStorage.

const { boot, SETTLE } = require('./harness');

const EXPOSE = `
globalThis.__probe = () => ({
  mode: STATE_MODE, api: API,
  read: [...STATE.read].sort(), saved: [...STATE.saved].sort()
});
globalThis.__set = (r, s) => { STATE.read = new Set(r); STATE.saved = new Set(s); };
globalThis.__persist = () => persist();
`;

module.exports.run = async t => {
  t.section('server mode: backend answering');
  {
    const p = await boot({
      api: { read: ['a', 'b'], saved: ['c'] },
      local: { read: ['zzz'], saved: [] },
      expose: EXPOSE
    });
    const s = p.sandbox.__probe();
    t.eq('mode', s.mode, 'server');
    t.eq('API flag', s.api, true);
    t.eq('server wins, local ignored', s.read, ['a', 'b']);
    t.eq('saved from server', s.saved, ['c']);
  }

  t.section('fresh browser with a seed: absorbed once');
  {
    const p = await boot({ seed: { read: ['a', 'b'], saved: ['c'] }, expose: EXPOSE });
    const s = p.sandbox.__probe();
    t.eq('mode', s.mode, 'seeded');
    t.eq('API flag', s.api, false);
    t.eq('read from seed', s.read, ['a', 'b']);
    t.eq('saved from seed', s.saved, ['c']);
    t.eq('seed written into localStorage', p.local(), { read: ['a', 'b'], saved: ['c'] });
  }

  t.section('REGRESSION: un-marking must survive a reload');
  {
    // The bug: union-merging the seed on every load handed un-marked items
    // straight back. Seed says a and b are read, user un-marks a, reload must
    // not resurrect it.
    const p = await boot({
      seed: { read: ['a', 'b'], saved: [] },
      local: { read: ['b'], saved: [] },
      expose: EXPOSE
    });
    const s = p.sandbox.__probe();
    t.eq('local authoritative once it exists', s.mode, 'local');
    t.eq('un-marked item stays un-marked', s.read, ['b']);
    t.ok('no resurrection from the seed', !s.read.includes('a'), s.read.join(','));
  }

  t.section('local mode: own marks, no backend, no seed');
  {
    const p = await boot({ local: { read: ['x'], saved: ['y'] }, expose: EXPOSE });
    const s = p.sandbox.__probe();
    t.eq('mode', s.mode, 'local');
    t.eq('read from localStorage', s.read, ['x']);
    t.eq('saved from localStorage', s.saved, ['y']);
  }

  t.section('first ever run: nothing anywhere');
  {
    const p = await boot({ expose: EXPOSE });
    const s = p.sandbox.__probe();
    t.eq('mode', s.mode, 'local');
    t.eq('read empty', s.read, []);
    t.eq('saved empty', s.saved, []);
  }

  t.section('malformed seed must not throw');
  {
    const a = await boot({ seed: { nope: 1 }, expose: EXPOSE });
    t.eq('wrong shape falls back to empty', a.sandbox.__probe(),
      { mode: 'local', api: false, read: [], saved: [] });

    const b = await boot({ seed: 'not an object', expose: EXPOSE });
    t.eq('non-object seed survives', b.sandbox.__probe().mode, 'local');
  }

  t.section('marks made locally persist out');
  {
    const p = await boot({ expose: EXPOSE });
    p.sandbox.__set(['m1', 'm2'], ['s1']);
    p.sandbox.__persist();
    await SETTLE();
    t.eq('written to localStorage', p.local(), { read: ['m1', 'm2'], saved: ['s1'] });
  }

  t.section('sidebar reports the active mode');
  {
    const srv = await boot({ api: { read: [], saved: [] }, expose: EXPOSE });
    t.ok('server mode says so', /server/.test(srv.el('state-where').innerHTML),
      srv.el('state-where').innerHTML);

    const loc = await boot({ expose: EXPOSE });
    t.ok('static mode names the browser', /this browser/.test(loc.el('state-where').innerHTML),
      loc.el('state-where').innerHTML);
  }
};
