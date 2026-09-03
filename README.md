# Signal

A single link that always shows the latest AI news, GitHub releases and trending repos. It updates itself. No server, no database, no monthly bill.

## How it works

Three pieces, that's it.

```
sources.json   ->   fetch.py   ->   data/feed.json   ->   index.html
(your config)      (the cron)      (the data)            (the link)
```

A GitHub Action runs `fetch.py` every 3 hours. It pulls every RSS feed, queries the GitHub API, writes one JSON file, commits it, and republishes the page. The HTML is static and just reads that JSON.

Because the output is static, you can host it on GitHub Pages, Cloudflare Pages, Netlify, or Vercel. All free tiers. Pick one, point it at the repo, done.

## Run it locally

```bash
python3 fetch.py          # writes data/feed.json
python3 -m http.server 8000
```

Open `http://localhost:8000`. That's it. No pip install, no dependencies, standard library only.

## Change what it tracks

Edit `sources.json`. Nothing else. Push the change and the site picks it up on the next cycle (or immediately, since pushes to main trigger a rebuild).

```jsonc
{
  "news": [
    { "name": "Hacker News", "url": "https://hnrss.org/frontpage", "tag": "community" }
  ],

  // GitHub search syntax. {since_30d} expands to a real date at runtime.
  "discovery": [
    { "name": "New AI repos", "query": "topic:ai created:>{since_30d}", "sort": "stars", "limit": 12 }
  ],

  // Latest release per repo, via the GitHub releases API.
  "releases": [
    "anthropics/claude-code",
    "oven-sh/bun"
  ]
}
```

Add a feed, delete a feed, change a search query, swap the release watchlist. All from that one file.

## Deploy

1. Create a GitHub repo, push this folder.
2. Settings, Pages, Source: **GitHub Actions**.
3. Trigger the `Refresh feed` workflow once manually.

You get a permanent URL. It updates every 3 hours forever, at zero cost.

### Rate limits

Unauthenticated GitHub API is 60 requests/hour per IP. One full run uses about 13, so it works fine without a token. The workflow already passes the built-in `GITHUB_TOKEN`, which raises it to 1000/hour. If you add a lot of repos later, drop a personal access token into repo secrets as `GH_TOKEN`.

## Costs

| Thing | Cost |
|---|---|
| GitHub Actions (8 runs/day, ~1 min each) | Free, well under the 2000 min/month limit |
| GitHub Pages / Cloudflare Pages hosting | Free |
| Domain (optional) | ~10 GBP/year |

Total: zero, unless you buy a domain.

## Notes

- Rename the site by editing `site.title` and `site.subtitle` in `sources.json`.
- `refresh_hours` in `sources.json` should match your cron cadence. It only drives the "next refresh" countdown in the header.
- Dead feeds are skipped, not fatal. One broken source never kills a run.
- News is deduped by URL. Repos are deduped by full name.
- The "New" badge compares item dates against your last visit, stored in `localStorage`. Per browser, per device.
