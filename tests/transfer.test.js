'use strict';

// Export/Import is the only way to move marks between devices on a static
// host, since Pages has no backend to accept a POST. Covers the payload shape
// and the merge semantics.

const { boot, SETTLE } = require('./harness');

const EXPOSE = `
globalThis.__fns = { exportState, importState };
globalThis.__set = (r, s) => { STATE.read = new Set(r); STATE.saved = new Set(s); };
globalThis.__peek = () => ({ read: [...STATE.read].sort(), saved: [...STATE.saved].sort() });
globalThis.__toast = () => ($("toast") ? $("toast").textContent : "");
`;

module.exports.run = async t => {
  const p = await boot({ expose: EXPOSE });
  const fns = p.sandbox.__fns;
  const set = p.sandbox.__set;
  const peek = p.sandbox.__peek;
  const toast = p.sandbox.__toast;

  t.section('export');
  set(['a', 'b', 'c'], ['z']);
  fns.exportState();
  await SETTLE();

  t.ok('exactly one blob produced', p.blobs.length === 1, `got ${p.blobs.length}`);
  const payload = JSON.parse(p.blobs[0]);
  t.eq('read array exported', payload.read.slice().sort(), ['a', 'b', 'c']);
  t.eq('saved array exported', payload.saved, ['z']);
  t.ok('carries an exported_at stamp',
    typeof payload.exported_at === 'string' && payload.exported_at.length > 10,
    String(payload.exported_at));

  const anchor = p.created.find(e => e.download);
  t.ok('filename is signal-state-YYYY-MM-DD.json',
    !!anchor && /^signal-state-\d{4}-\d{2}-\d{2}\.json$/.test(anchor.download),
    anchor ? anchor.download : 'no anchor created');
  t.eq('toast reports the counts', toast(), 'Exported 3 read, 1 saved');

  t.section('import: union merge with dedupe');
  set(['a'], ['z']);
  fns.importState({ __text: JSON.stringify({ read: ['a', 'b'], saved: ['y'] }) });
  await SETTLE();
  t.eq('read merged without dupes', peek().read, ['a', 'b']);
  t.eq('saved merged', peek().saved, ['y', 'z']);
  t.eq('toast counts only new marks', toast(), 'Merged 2 new marks');

  t.section('import: nothing new');
  set(['a'], ['z']);
  fns.importState({ __text: JSON.stringify({ read: ['a'], saved: ['z'] }) });
  await SETTLE();
  t.eq('state unchanged', peek(), { read: ['a'], saved: ['z'] });
  t.eq('toast says nothing new', toast(), 'Nothing new in that file');

  t.section('import: rejects bad input');
  set(['keep'], []);
  fns.importState({ __text: 'this is not json' });
  await SETTLE();
  t.eq('invalid JSON rejected, state intact', peek().read, ['keep']);
  t.eq('toast for invalid JSON', toast(), 'That file is not valid JSON');

  fns.importState({ __text: JSON.stringify({ foo: 'bar' }) });
  await SETTLE();
  t.eq('wrong shape rejected, state intact', peek().read, ['keep']);
  t.eq('toast for wrong shape', toast(), 'That is not a Signal state file');

  fns.importState({ __fail: true });
  await SETTLE();
  t.eq('unreadable file handled', toast(), 'Could not read that file');

  t.section('import never loses existing marks');
  set(['mine'], ['keepme']);
  fns.importState({ __text: JSON.stringify({ read: ['other'], saved: [] }) });
  await SETTLE();
  t.eq('own marks survive a merge', peek(), { read: ['mine', 'other'], saved: ['keepme'] });

  t.section('import persists to localStorage in local mode');
  set(['a'], []);
  fns.importState({ __text: JSON.stringify({ read: ['a', 'b'], saved: [] }) });
  await SETTLE();
  t.eq('merged set written to localStorage', p.local().read.slice().sort(), ['a', 'b']);
};
