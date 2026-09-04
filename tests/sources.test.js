'use strict';

// sources.json is the only file meant to be hand edited, so it is also the
// easiest thing to break. A mistyped category silently drops items out of the
// nav; a duplicate name collides in the colour hash and the source filter.
// This validates the config shape, not the network.

const fs = require('fs');
const path = require('path');
const { ROOT, INDEX_HTML } = require('./harness');

const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'sources.json'), 'utf8'));
const html = fs.readFileSync(INDEX_HTML, 'utf8');

// Nav is hardcoded markup. Read the real list so the test moves with the UI
// rather than duplicating it.
const navCats = [...new Set([...html.matchAll(/data-cat="([a-z]+)"/g)].map(m => m[1]))];

module.exports.run = async t => {
  t.section('shape');
  t.ok('nav categories were found', navCats.length > 0, navCats.join(', '));
  t.ok('feeds array is populated', Array.isArray(cfg.news) && cfg.news.length > 0,
    `${cfg.news && cfg.news.length} feeds`);

  t.section('every feed is well formed');
  {
    // "top" and "saved" are nav targets, not assignable source categories.
    const allowed = new Set(navCats.filter(c => c !== 'top' && c !== 'saved'));

    const badCat = [], badField = [], badWeight = [], badLimit = [], notHttps = [];
    for (const s of cfg.news) {
      if (!s.name || !s.url || !s.category) { badField.push(s.name || JSON.stringify(s)); continue; }
      if (!allowed.has(s.category)) badCat.push(`${s.name} -> ${s.category}`);
      if (!(s.weight >= 1 && s.weight <= 5)) badWeight.push(`${s.name} -> ${s.weight}`);
      if (!(s.limit > 0)) badLimit.push(`${s.name} -> ${s.limit}`);
      if (!/^https?:\/\//.test(s.url)) notHttps.push(s.name);
    }

    t.eq('no feed is missing a required field', badField, []);
    t.eq('every category exists in the nav', badCat, []);
    t.eq('weights are all 1-5', badWeight, []);
    t.eq('limits are all positive', badLimit, []);
    t.eq('urls are absolute', notHttps, []);
  }

  t.section('no duplicates');
  {
    const names = cfg.news.map(s => s.name);
    const urls = cfg.news.map(s => s.url);
    const dup = a => [...new Set(a.filter((x, i) => a.indexOf(x) !== i))];

    // A duplicate name is worse than it looks: srcColor hashes the name, so two
    // feeds sharing one produce two identical colours, and the source filter
    // merges them into a single chip.
    t.eq('no duplicate source names', dup(names), []);
    t.eq('no duplicate urls', dup(urls), []);
    t.eq('no duplicate release watches', dup(cfg.releases || []), []);
  }

  t.section('release watches are owner/repo');
  {
    const bad = (cfg.releases || []).filter(r => !/^[\w.-]+\/[\w.-]+$/.test(r));
    t.eq('every entry parses as owner/repo', bad, []);
    t.ok('releases are watched', (cfg.releases || []).length > 0, `${(cfg.releases || []).length}`);
  }

  t.section('json sources and discovery');
  {
    for (const j of cfg.json_sources || []) {
      t.ok(`json source ${j.name} has a type`, !!j.type, JSON.stringify(j.type));
      t.ok(`json source ${j.name} has a limit`, j.limit > 0, String(j.limit));
    }

    for (const d of cfg.discovery || []) {
      t.ok(`discovery ${d.name} has a query`, !!d.query, '');
      t.ok(`discovery ${d.name} category is real`, navCats.includes(d.category), d.category);
    }
  }

  t.section('coverage of the layers that matter');
  {
    const has = re => cfg.news.some(s => re.test(s.name) || re.test(s.url));
    t.ok('an open-weights community source is present', has(/local/i), '');
    t.ok('an inference runtime is release-watched',
      (cfg.releases || []).some(r => /llama\.cpp|vllm|sglang/i.test(r)), '');
    t.ok('a changelog feed is present', has(/changelog/i), '');
  }
};
