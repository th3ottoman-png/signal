'use strict';

// node tests/run.js        run everything
// node tests/run.js state  run files whose name contains "state"

const fs = require('fs');
const path = require('path');
const { createReporter } = require('./harness');

(async () => {
  const filter = process.argv[2];
  const files = fs.readdirSync(__dirname)
    .filter(f => f.endsWith('.test.js'))
    .filter(f => !filter || f.includes(filter))
    .sort();

  if (!files.length) {
    console.error(`no test files${filter ? ` matching "${filter}"` : ''}`);
    process.exit(1);
  }

  const summaries = [];

  for (const file of files) {
    console.log('\n' + '='.repeat(60));
    console.log(file);
    console.log('='.repeat(60));

    const t = createReporter(file);
    let mod;
    try {
      mod = require(path.join(__dirname, file));
    } catch (e) {
      t.fail('file failed to load', e && e.stack ? e.stack : String(e));
      summaries.push({ file, ...t.summary(), loaded: false });
      continue;
    }

    if (typeof mod.run !== 'function') {
      t.fail('exports no run(t) function');
      summaries.push({ file, ...t.summary(), loaded: false });
      continue;
    }

    try {
      await mod.run(t);
    } catch (e) {
      t.fail('threw', e && e.stack ? e.stack : String(e));
    }

    const s = t.summary();

    // A file that asserts nothing is a failure, not a pass. Silently passing
    // zero assertions is how a typo in a key name went unnoticed: a loop over
    // feed.stories (a key that does not exist) iterated an empty array, so
    // every assertion inside it passed without ever running.
    if (s.total === 0) {
      t.fail('ran zero assertions, the test body probably did not execute');
    }

    summaries.push({ file, ...t.summary(), loaded: true });
  }

  const total = summaries.reduce((n, s) => n + s.total, 0);
  const failed = summaries.reduce((n, s) => n + s.failed, 0);

  console.log('\n' + '='.repeat(60));
  for (const s of summaries) {
    const mark = s.failed ? 'FAIL' : s.total ? 'ok  ' : '????';
    console.log(`  ${mark}  ${s.file}  ${s.total - s.failed}/${s.total}`);
  }
  console.log('='.repeat(60));
  console.log(`${total - failed}/${total} assertions passed`);
  console.log('='.repeat(60));

  process.exit(failed || total === 0 ? 1 : 0);
})();
