'use strict';

// Every card template must set --item-color, or the accent bar silently falls
// back to blue. See invariant 7 in the skill. This checks the CSS wiring and
// then walks the real feed to confirm each card carries the right hue.

const fs = require('fs');
const { boot, realFeed, INDEX_HTML } = require('./harness');

const EXPOSE = `
globalThis.__fns = { itemCard, repoCard, releaseCard, srcColor };
`;

const barColour = html => {
  const m = html.match(/--item-color:\s*(#[0-9a-fA-F]{3,8})/);
  return m ? m[1] : null;
};

module.exports.run = async t => {
  t.section('CSS wiring');
  {
    const html = fs.readFileSync(INDEX_HTML, 'utf8');
    t.ok('accent bar reads --item-color',
      /\.item::before\s*\{[^}]*background:\s*var\(--item-color/.test(html),
      'the bar is not wired to --item-color, it will render one colour');

    t.ok('fallback to the old accent blue is retained',
      /var\(--item-color,\s*var\(--accent\)\)/.test(html),
      'missing fallback means a card with no source renders transparent');

    t.ok('bar stays hidden unless unread',
      /\.item\.unread::before\s*\{\s*opacity:\s*\.85/.test(html),
      'the bar doubles as the unread marker, confirm before changing');
  }

  const feed = realFeed();
  if (!feed) {
    t.skip('real feed checks', 'data/feed.json missing, run fetch.py first');
    return;
  }

  // Guard against the vacuous pass. A wrong key name iterates an empty array
  // and every assertion inside it "passes" without ever running.
  t.ok('feed has items to walk', Array.isArray(feed.items) && feed.items.length > 0,
    `items=${feed.items && feed.items.length}`);

  const p = await boot({ feed, expose: EXPOSE });
  const { itemCard, repoCard, releaseCard, srcColor } = p.sandbox.__fns;

  t.section('story cards');
  {
    const sample = feed.items.slice(0, 80);
    let missing = 0, mismatched = 0;
    for (const it of sample) {
      const c = barColour(itemCard(it));
      if (!c) { missing++; continue; }
      if (c !== srcColor(it.source)) mismatched++;
    }
    t.eq('every story card carries --item-color', missing, 0);
    t.eq('bar colour matches the source colour', mismatched, 0);
  }

  t.section('repo cards');
  {
    const sample = (feed.repos || []).slice(0, 43);
    t.ok('feed has repos to walk', sample.length > 0, `repos=${sample.length}`);
    let missing = 0, mismatched = 0;
    for (const r of sample) {
      const c = barColour(repoCard(r));
      if (!c) { missing++; continue; }
      if (c !== srcColor(r.section || 'GitHub')) mismatched++;
    }
    t.eq('every repo card carries --item-color', missing, 0);
    t.eq('bar colour matches the section colour', mismatched, 0);
  }

  t.section('release cards');
  {
    const sample = feed.releases || [];
    t.ok('feed has releases to walk', sample.length > 0, `releases=${sample.length}`);
    let missing = 0, mismatched = 0;
    for (const r of sample) {
      const html = releaseCard(r);
      const c = barColour(html);
      if (!c) { missing++; continue; }
      // Keyed off the repo, NOT the "Release" label, which is identical on
      // every card and would paint the whole section one colour.
      if (c !== srcColor(r.repo)) mismatched++;
    }
    t.eq('every release card carries --item-color', missing, 0);
    t.eq('bar colour matches the repo colour', mismatched, 0);

    const labels = new Set(sample.map(r => srcColor(r.repo)));
    t.ok('releases are not all one colour', labels.size > 1,
      `${labels.size} distinct across ${sample.length} releases`);
  }

  t.section('palette behaviour');
  {
    // 13 hues, 40 sources: collisions are expected and mirror the source-name
    // text. Do not "fix" this by generating more colours.
    const colours = new Set(feed.items.slice(0, 80).map(i => srcColor(i.source)));
    t.ok('stories span multiple hues', colours.size >= 6, `${colours.size} distinct`);
    t.ok('no source maps to the old accent blue', !colours.has('#5aa9ff'));

    const before = srcColor('Hacker News');
    const after = srcColor('Hacker News');
    t.eq('colour is stable for a given source', before, after);
    t.ok('different sources differ', before !== srcColor('TechCrunch'));
  }

  t.section('source name text matches the bar');
  {
    const it = feed.items[0];
    const html = itemCard(it);
    const bar = barColour(html);
    const span = html.match(/class="src"\s+style="color:(#[0-9a-fA-F]{3,8})"/);
    t.ok('src span is coloured', !!span, html.slice(0, 120));
    if (span) t.eq('bar and source name share one colour', span[1], bar);
  }
};
