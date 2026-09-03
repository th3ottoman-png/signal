# Signal

One link that always shows the latest AI news, agent coverage, research, GitHub releases and trending repos. It refreshes itself. No database, no build step, no dependencies.

## What it tracks

62 inputs, six categories.

| Section | Sources | What lands there |
|---|---|---|
| Agents | 4 | Agent frameworks, harnesses, Claude Code / Cursor / Codex coverage |
| News | 9 | General AI news wires |
| Vendors | 8 | OpenAI, Google, Microsoft, Meta and friends |
| Analysis | 12 | Simon Willison, Import AI, Stratechery, SemiAnalysis, AI Snake Oil |
| Research | 4 | arXiv cs.AI / cs.LG / cs.CL / cs.MA, Hugging Face Daily Papers |
| Community | 5 | Hacker News, HN Best, Lobsters, Product Hunt, Dev.to |

Plus 4 GitHub search queries (agent frameworks, new AI repos, hot LLM tooling, self-hosted) and 15 release watches.

A typical run: **482 raw items, 28 duplicates merged into clusters, 400 stories, 43 repos, 15 releases. About 27 seconds.**

## How it works

```
sources.json  ->  fetch.py  ->  data/feed.json  ->  server.py  ->  index.html
 (config)        (fetcher)       (data + state)     (serves)       (the UI)
```

`fetch.py` is standard library only. It pulls every feed concurrently, clusters duplicate stories, scores them, and writes one JSON file.

## Two ways to run it

### 1. Always-on server (recommended)

```bash
python3 server.py          # or: PORT=8000 python3 server.py
```

This is the real "it updates itself" mode. The server:

- refreshes the feed in a background thread whenever it goes stale
- stores your read and saved items in `data/state.json`, so read state follows you across devices instead of drifting per browser
- exposes `POST /api/refresh` to force an update

It binds `0.0.0.0` and honours `$PORT`, so it drops straight into any container host.

### 2. Static site + GitHub Actions cron

The `.github/workflows/refresh.yml` workflow runs `fetch.py` on a cron, commits `data/feed.json`, and deploys to GitHub Pages. Host the output anywhere static.

Read and saved state falls back to `localStorage` when no server is present, which works but is per-browser.

## Change what it tracks

Edit `sources.json`. Nothing else.

```jsonc
{
  "news": [
    { "name": "Hacker News", "url": "https://hnrss.org/frontpage",
      "category": "community", "weight": 3, "limit": 20,
      "trim_publisher": true }   // strips the trailing " - Publisher"
  ],

  // GitHub search syntax. {since_30d} expands to a real date at runtime.
  "discovery": [
    { "name": "New AI repos", "query": "topic:ai created:>{since_30d}", "sort": "stars", "limit": 12 }
  ],

  // Latest release per repo, read from /releases.atom (not rate limited).
  "releases": [
    "anthropics/claude-code",
    "oven-sh/bun"
  ]
}
```

`weight` (1-5) biases ranking. `limit` caps items per feed. `category` picks the sidebar section.

## Story clustering

The same event hits eight outlets. Showing it eight times is noise, so near-duplicate headlines merge into one story with a coverage count and an "also covered by" line.

How it works: tokenise each headline, drop stopwords, weight terms by inverse document frequency, then union-find any pair clearing a similarity threshold. Two guards stop false merges:

- `MIN_SHARED_TOKENS = 2` - headlines must share at least two distinctive words
- `RARE_DF = 12` - at least one shared word must be genuinely rare

Without the rarity guard, unrelated stories merge on phrases like "large language models" that are common across the whole corpus. With it too tight, a genuinely big story covered by seven outlets fails to merge. Both constants were tuned against a real 482-item run, not guessed.

Ranking blends three signals:

```
recency (exponential decay, ~1 day half-life) * 3.0
+ outlet coverage (capped at 5) * 0.55
+ source weight * 0.5
```

## API

Only in server mode.

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/state` | GET | Read saved and read item IDs |
| `/api/state` | POST | Write them back |
| `/api/status` | GET | Feed age, next refresh, last error |
| `/api/refresh` | POST | Force a refresh now |

## Rate limits

Releases read `github.com/{repo}/releases.atom`, which is **not rate limited**. This is deliberate: the REST API caps at 60 requests/hour unauthenticated, and 15 release lookups burned a quarter of that budget on every run, enough to starve the section completely.

Only GitHub repo discovery touches the API, about 43 calls per run. The workflow passes the built-in `GITHUB_TOKEN`, which raises the cap to 1000/hour. Without a token, 60/hour still covers a run every 3 hours, but not much headroom for retries.

If a run does fail partway, `keep_last_good` preserves the previous section contents rather than blanking them. A bad hour degrades to stale data, never to an empty page.

## Costs

| Thing | Cost |
|---|---|
| GitHub Actions (8 runs/day, ~1 min each) | Free, well under 2000 min/month |
| GitHub Pages / Cloudflare Pages | Free |
| Container host for server mode | Free tier on most, or ~5 GBP/month |
| Domain (optional) | ~10 GBP/year |

Total: zero if you use Actions plus static hosting.

## Notes

- Rename it by editing `site.title` and `site.subtitle` in `sources.json`.
- `refresh_hours` should match your cron cadence. It drives the auto-refresh and the header countdown.
- Dead feeds are skipped, not fatal. One broken source never kills a run.
- Transferred repos are detected on fetch and reported as `MOVED -> owner/repo` so the config can be corrected.
- Prereleases are detected from the tag name with a regex, since the Atom feed carries no flag. Tags matching `alpha`, `beta`, `rc`, `canary`, `nightly`, `preview`, `.dev`, `-pre` are marked.
