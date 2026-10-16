'use strict';

// The night/day toggle. Two things here are easy to break quietly:
//
//   1. A hardcoded colour reappearing anywhere in <style> outside :root. That
//      renders fine in whichever theme you happen to be looking at and is
//      invisible or unreadable in the other, so it never fails a manual check.
//   2. A palette entry that reads as text but was tuned by eye. The source
//      name is 11px, which is small text, so it needs the full 4.5:1.
//
// Both are asserted against the file rather than a rendering.

const fs = require('fs');
const { boot, realFeed, INDEX_HTML } = require('./harness');

const EXPOSE = `
globalThis.__fns = { applyTheme, initTheme, toggleTheme, srcColor,
  getTheme: () => THEME };
globalThis.__pal = { light: SRC_PALETTE_LIGHT, dark: SRC_PALETTE_DARK };
`;

// ---------- CSS helpers ----------

function styleBlock() {
  const html = fs.readFileSync(INDEX_HTML, 'utf8');
  const m = html.match(/<style>([\s\S]*?)<\/style>/);
  return m ? m[1] : '';
}

// Cut every `:root ... { ... }` block out, keeping the rest. Tokenisation is
// only meaningful for the rules outside them, since :root is where literals
// are supposed to live.
function withoutRootBlocks(css) {
  let out = '';
  let i = 0;
  const open = /:root[^{]*\{/g;
  for (;;) {
    open.lastIndex = i;
    const m = open.exec(css);
    if (!m) { out += css.slice(i); break; }
    out += css.slice(i, m.index);
    let depth = 1;
    let j = m.index + m[0].length;
    while (j < css.length && depth > 0) {
      if (css[j] === '{') depth++;
      else if (css[j] === '}') depth--;
      j++;
    }
    i = j;
  }
  return out;
}

// Read one `:root`-ish block's custom properties into a plain object.
function tokenMap(css, selector) {
  const idx = css.indexOf(selector);
  if (idx < 0) return null;
  const open = css.indexOf('{', idx);
  let depth = 1;
  let j = open + 1;
  while (j < css.length && depth > 0) {
    if (css[j] === '{') depth++;
    else if (css[j] === '}') depth--;
    j++;
  }
  const body = css.slice(open + 1, j - 1);
  const map = {};
  const re = /--([\w-]+)\s*:\s*([^;]+);/g;
  let m;
  while ((m = re.exec(body))) map[m[1]] = m[2].trim();
  return map;
}

// ---------- WCAG relative luminance ----------

function parseHex(h) {
  let s = String(h).trim().replace(/^#/, '');
  if (s.length === 3) s = s[0] + s[0] + s[1] + s[1] + s[2] + s[2];
  if (s.length === 8) s = s.slice(0, 6); // ignore alpha, worst case is opaque
  return [0, 2, 4].map(i => parseInt(s.slice(i, i + 2), 16));
}

function luminance(h) {
  const [r, g, b] = parseHex(h).map(v => {
    const c = v / 255;
    return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a, b) {
  const la = luminance(a);
  const lb = luminance(b);
  const hi = Math.max(la, lb);
  const lo = Math.min(la, lb);
  return (hi + 0.05) / (lo + 0.05);
}

module.exports.run = async t => {
  const css = styleBlock();
  const html = fs.readFileSync(INDEX_HTML, 'utf8');

  t.section('theme wiring');
  {
    t.ok('a light :root exists', /:root\s*\{/.test(css), 'no default light palette');
    t.ok('a dark override exists', /:root\[data-theme="dark"\]\s*\{/.test(css),
      'nothing switches when data-theme flips');

    const light = tokenMap(css, ':root {') || tokenMap(css, ':root{');
    const dark = tokenMap(css, ':root[data-theme="dark"]');
    t.ok('light block parsed', !!light);
    t.ok('dark block parsed', !!dark);

    if (light && dark) {
      const shared = ['bg', 'card', 'text', 'muted', 'dim', 'accent', 'border'];
      const missing = shared.filter(k => !light[k] || !dark[k]);
      t.eq('both themes define the same core tokens', missing, []);

      const unchanged = shared.filter(k => light[k] && dark[k] && light[k] === dark[k]);
      // bg/card/text/muted/dim/accent must diverge or the toggle does nothing.
      const mustDiffer = ['bg', 'card', 'text'];
      const same = mustDiffer.filter(k => unchanged.includes(k));
      t.eq('surfaces actually change between themes', same, []);
    }

    t.ok('topbar has a theme button', /id="btn-theme"/.test(html),
      'no #btn-theme, the toggle has no handle');
    t.ok('theme button is wired to a click', /btn-theme["']?\)\.addEventListener\(["']click["']/.test(html),
      'the button exists but nothing listens to it');
    t.ok('theme is restored on boot',
      /initTheme\(\);/.test(html) && /signal_theme/.test(html),
      'the choice would reset on every reload');
  }

  t.section('no hardcoded colours outside :root');
  {
    const body = withoutRootBlocks(css);
    const hexes = body.match(/#[0-9a-fA-F]{3,8}\b/g) || [];
    const fns = body.match(/\b(?:rgba?|hsla?|hwb|lab|lch|oklch|color-mix)\s*\(/g) || [];
    t.eq('no literal hex in the rules', hexes, []);
    t.eq('no literal rgb()/hsl() in the rules', fns, []);
  }

  const feed = realFeed();
  const p = await boot({ feed: feed || undefined, expose: EXPOSE });
  const fns = p.sandbox.__fns;
  const pal = p.sandbox.__pal;

  t.section('palette');
  {
    t.ok('both palettes exist', Array.isArray(pal.light) && Array.isArray(pal.dark));
    t.eq('palettes are the same length', pal.light.length, pal.dark.length);
    t.ok('palette is not empty', pal.light.length > 0, `${pal.light.length} entries`);

    // Hue alignment: the hash picks an index, so a source must land on the
    // same slot in both. Reordering one array silently repaints every source.
    const names = ['Hacker News', 'TechCrunch', 'The Verge', 'Ars Technica',
      'GitHub', 'OpenAI Blog', 'arXiv', 'Lobsters'];
    const misaligned = names.filter(n =>
      pal.light.indexOf(fns.srcColor(n)) !== -1 &&
      pal.dark.indexOf(fns.srcColor(n)) !== -1 &&
      pal.light.indexOf(fns.srcColor(n)) !== pal.dark.indexOf(fns.srcColor(n)));
    t.eq('a source keeps its hue slot across themes', misaligned, []);

    const defaults = pal.light.includes(fns.srcColor('Hacker News'))
      ? 'light' : (pal.dark.includes(fns.srcColor('Hacker News')) ? 'dark' : 'neither');
    t.eq('pages start in the light theme', defaults, 'light');
    t.eq('theme variable agrees', fns.getTheme(), 'light');
  }

  t.section('source name legibility');
  {
    // Small text, so the full 4.5:1. Checked against every surface a card can
    // sit on: the card itself, the page, the card hover tint, the briefing tint.
    const css2 = styleBlock();
    const light = tokenMap(css2, ':root {') || tokenMap(css2, ':root{');
    const dark = tokenMap(css2, ':root[data-theme="dark"]');

    // Keys come back without their leading dashes, see tokenMap.
    const bgs = m => ['card', 'bg', 'card-hover', 'briefing', 'hover', 'sunken']
      .map(k => m && m[k]).filter(Boolean);

    const lightBgs = bgs(light);
    const darkBgs = bgs(dark);

    t.ok('light backgrounds resolved', lightBgs.length >= 4, `${lightBgs.length} resolved`);
    t.ok('dark backgrounds resolved', darkBgs.length >= 4, `${darkBgs.length} resolved`);

    // Guard against the vacuous pass: with no backgrounds this loop body never
    // runs and the assertion below reports green without testing anything.
    const check = (palette, backgrounds) => {
      const failing = [];
      let worst = { ratio: Infinity, hex: null };
      let pairs = 0;
      for (const h of palette) {
        for (const bg of backgrounds) {
          const c = contrast(h, bg);
          pairs++;
          if (c < worst.ratio) { worst.ratio = c; worst.hex = `${h} on ${bg}`; }
          if (c < 4.5) failing.push(`${h} on ${bg} = ${c.toFixed(2)}`);
        }
      }
      return { failing, worst, pairs };
    };

    const l = check(pal.light, lightBgs);
    t.ok('light pairings were actually measured', l.pairs > 0, `${l.pairs} pairings`);
    t.eq('every light palette colour clears 4.5:1', l.failing, []);
    console.log(`          worst of ${l.pairs}: ${l.worst.hex} at ${l.worst.ratio.toFixed(2)}:1`);

    const d = check(pal.dark, darkBgs);
    t.ok('dark pairings were actually measured', d.pairs > 0, `${d.pairs} pairings`);
    t.eq('every dark palette colour clears 4.5:1', d.failing, []);
    console.log(`          worst of ${d.pairs}: ${d.worst.hex} at ${d.worst.ratio.toFixed(2)}:1`);

    // The dark palette is pastel and cannot be reused on white. This is the
    // whole reason there are two arrays.
    const reuseFails = pal.dark.filter(h => contrast(h, '#ffffff') < 4.5);
    t.ok('the dark palette would indeed fail on white', reuseFails.length > 0,
      `${reuseFails.length}/13 under 4.5:1, so one shared palette is not viable`);
  }

  t.section('body text legibility');
  {
    // The palette is not the only coloured text on the page. --muted carries
    // summaries and --dim carries timestamps, and both were tuned for the old
    // dark-only page where a low ratio reads as "quiet". On white the same
    // value reads as broken.
    const css2 = styleBlock();
    const light = tokenMap(css2, ':root {') || tokenMap(css2, ':root{');
    const dark = tokenMap(css2, ':root[data-theme="dark"]');
    const surf = m => ['card', 'bg', 'hover', 'sunken', 'card-hover', 'briefing']
      .map(k => m && m[k]).filter(Boolean);

    for (const [name, tokens] of [['light', light], ['dark', dark]]) {
      t.ok(`${name} surfaces resolved`, surf(tokens).length >= 4, `${surf(tokens).length}`);
      for (const key of ['text', 'muted', 'dim']) {
        const fg = tokens && tokens[key];
        const bad = [];
        for (const bg of surf(tokens)) {
          const c = contrast(fg, bg);
          if (c < 4.5) bad.push(`${fg} on ${bg} = ${c.toFixed(2)}`);
        }
        t.eq(`${name} --${key} clears 4.5:1 everywhere`, bad, []);
      }
    }

    // --sep is the "·" between meta fields. Decorative punctuation, so it is
    // deliberately below AA, but it still has to be visible: at the old 1.4:1
    // it did not render at all on a light card.
    for (const [name, tokens, floor] of [['light', light, 2.0], ['dark', dark, 2.0]]) {
      const c = contrast(tokens.sep, tokens.card);
      t.ok(`${name} --sep is visible but stays subordinate`, c >= floor && c < 4.5,
        `${c.toFixed(2)}:1 (want ${floor}-4.5, under --muted)`);
    }
  }

  t.section('switching');
  {
    fns.applyTheme('dark');
    t.eq('applyTheme("dark") sets the theme', fns.getTheme(), 'dark');
    t.ok('srcColor follows to the dark palette',
      pal.dark.includes(fns.srcColor('Hacker News')),
      fns.srcColor('Hacker News'));
    t.eq('choice is persisted', p.sandbox.localStorage.getItem('signal_theme'), 'dark');

    fns.applyTheme('light');
    t.eq('applyTheme("light") sets the theme', fns.getTheme(), 'light');
    t.ok('srcColor follows back to the light palette',
      pal.light.includes(fns.srcColor('Hacker News')),
      fns.srcColor('Hacker News'));
    t.eq('choice is persisted', p.sandbox.localStorage.getItem('signal_theme'), 'light');

    // Unknown values must fall back rather than paint an undefined palette.
    fns.applyTheme('banana');
    t.eq('an unknown theme falls back to light', fns.getTheme(), 'light');

    fns.applyTheme('dark');
    fns.toggleTheme();
    t.eq('toggleTheme flips dark to light', fns.getTheme(), 'light');
    fns.toggleTheme();
    t.eq('toggleTheme flips light to dark', fns.getTheme(), 'dark');
  }

  t.section('restore on reload');
  {
    p.sandbox.localStorage.setItem('signal_theme', 'dark');
    fns.initTheme();
    t.eq('a saved dark preference is restored', fns.getTheme(), 'dark');

    p.sandbox.localStorage.setItem('signal_theme', 'light');
    fns.initTheme();
    t.eq('a saved light preference is restored', fns.getTheme(), 'light');

    p.sandbox.localStorage.setItem('signal_theme', 'nonsense');
    fns.initTheme();
    t.eq('a corrupt preference falls back to light', fns.getTheme(), 'light');
  }
};
