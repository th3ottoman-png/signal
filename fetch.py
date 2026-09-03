#!/usr/bin/env python3
"""
Signal - feed fetcher

Pulls RSS/Atom news, GitHub repo discovery, and GitHub releases into data/feed.json.
Zero dependencies. Python 3.9+. Runs locally or in GitHub Actions.

Usage:
    python3 fetch.py
    python3 fetch.py --config sources.json --out data/feed.json

Set GITHUB_TOKEN env var to lift rate limits from 60/hr to 5000/hr.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

UA = "Mozilla/5.0 (compatible; SignalFeed/1.0; +https://github.com)"
TIMEOUT = 20
GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------- helpers

def http_get(url, accept=None):
    """Fetch a URL and return bytes. Raises on failure."""
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def http_json(url):
    return json.loads(http_get(url, accept="application/vnd.github+json").decode("utf-8"))


def strip_tags(text):
    if not text:
        return ""
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(text, limit=260):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "..."


def parse_date(value):
    """Parse RFC822 (RSS) or ISO8601 (Atom / GitHub) into an aware datetime."""
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
        if dt:
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        iso = value.replace("Z", "+00:00")
        iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso)
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def localname(tag):
    """Strip XML namespace: {http://...}item -> item"""
    return tag.split("}", 1)[1] if "}" in tag else tag


def find_text(node, *names):
    """Find first child (any namespace) matching a name, return its text."""
    wanted = set(names)
    for child in node:
        if localname(child.tag) in wanted:
            return child.text or ""
    return ""


def pick_link(node):
    """Atom links are elements with href attr. Prefer rel=alternate."""
    fallback = None
    for child in node:
        if localname(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if not href:
            if child.text and child.text.strip().startswith("http"):
                href = child.text.strip()
            else:
                continue
        if child.attrib.get("rel") == "alternate":
            return href
        if fallback is None:
            fallback = href
    return fallback or find_text(node, "link", "guid")


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


# ---------------------------------------------------------------- news

def fetch_feed(source):
    """Fetch one RSS or Atom feed. Returns list of items."""
    raw = http_get(source["url"])
    root = ElementTree.fromstring(raw)

    # Walk to the item/entry container, namespace-agnostic.
    entries = []
    queue = [root]
    while queue and not entries:
        node = queue.pop(0)
        for child in node:
            if localname(child.tag) in ("item", "entry"):
                entries.append(child)
        if not entries:
            queue.extend(list(node))

    items = []
    for e in entries[:25]:
        title = strip_tags(find_text(e, "title"))
        link = pick_link(e)
        if not title or not link:
            continue
        summary = strip_tags(
            find_text(e, "description", "summary", "content", "encoded")
        )
        published = parse_date(
            find_text(e, "pubDate", "published", "updated", "date", "modified")
        )
        items.append({
            "title": title,
            "url": link,
            "source": source["name"],
            "tag": source.get("tag", "news"),
            "summary": truncate(summary, 280),
            "published": iso(published) or iso(datetime.now(timezone.utc)),
        })
    return items


def collect_news(sources):
    all_items = []
    for src in sources:
        try:
            got = fetch_feed(src)
            all_items.extend(got)
            print(f"  [ok]   {src['name']:<18} {len(got)} items")
        except Exception as exc:
            print(f"  [fail] {src['name']:<18} {type(exc).__name__}: {exc}")
    return all_items


# ---------------------------------------------------------------- GitHub

def resolve_query(query):
    """Expand {since_Nd} placeholders into real dates."""
    today = datetime.now(timezone.utc).date()

    def repl(match):
        return (today - timedelta(days=int(match.group(1)))).isoformat()

    return re.sub(r"\{since_(\d+)d\}", repl, query)


def collect_discovery(queries):
    results = []
    for q in queries:
        query = resolve_query(q["query"])
        url = (
            f"{GITHUB_API}/search/repositories?q={urllib.parse.quote(query)}"
            f"&sort={q.get('sort', 'stars')}&order=desc&per_page={q.get('limit', 10)}"
        )
        try:
            data = http_json(url)
            repos = []
            for r in data.get("items", []):
                repos.append({
                    "full_name": r.get("full_name"),
                    "url": r.get("html_url"),
                    "description": truncate(r.get("description") or "", 200),
                    "stars": r.get("stargazers_count", 0),
                    "language": r.get("language") or "",
                    "section": q["name"],
                    "updated": r.get("pushed_at") or r.get("updated_at"),
                    "topics": (r.get("topics") or [])[:4],
                })
            results.extend(repos)
            print(f"  [ok]   {q['name']:<18} {len(repos)} repos")
        except Exception as exc:
            print(f"  [fail] {q['name']:<18} {type(exc).__name__}: {exc}")
        time.sleep(2)  # respect GitHub search rate limit (10/min unauthenticated)
    return results


def collect_releases(repos):
    results = []
    for full in repos:
        try:
            data = http_json(f"{GITHUB_API}/repos/{full}/releases/latest")
            results.append({
                "repo": full,
                "tag_name": data.get("tag_name"),
                "name": data.get("name") or data.get("tag_name"),
                "url": data.get("html_url"),
                "published": data.get("published_at") or data.get("created_at"),
                "prerelease": data.get("prerelease", False),
                "body": truncate(strip_tags(data.get("body") or ""), 400),
            })
            print(f"  [ok]   {full:<34} {data.get('tag_name')}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                print(f"  [skip] {full:<34} no releases published")
            else:
                print(f"  [fail] {full:<34} HTTP {exc.code}")
        except Exception as exc:
            print(f"  [fail] {full:<34} {type(exc).__name__}: {exc}")
        time.sleep(0.4)
    return results


# ---------------------------------------------------------------- main

def dedupe(items, key):
    seen = set()
    out = []
    for it in items:
        k = key(it)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="sources.json")
    parser.add_argument("--out", default="data/feed.json")
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    cfg_path = args.config if os.path.isabs(args.config) else os.path.join(here, args.config)

    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    print("Fetching news feeds...")
    news = dedupe(collect_news(cfg.get("news", [])), lambda x: x["url"])

    print("Fetching GitHub discovery...")
    repos = dedupe(collect_discovery(cfg.get("discovery", [])), lambda x: x["full_name"])

    print("Fetching latest releases...")
    releases = collect_releases(cfg.get("releases", []))

    by_newest = lambda x: x.get("published") or x.get("updated") or ""
    news.sort(key=by_newest, reverse=True)
    releases.sort(key=by_newest, reverse=True)
    repos.sort(key=lambda x: x.get("stars", 0), reverse=True)

    payload = {
        "generated_at": iso(datetime.now(timezone.utc)),
        "site": cfg.get("site", {}),
        "news": news[:120],
        "repos": repos,
        "releases": releases,
        "stats": {
            "news": len(news),
            "repos": len(repos),
            "releases": len(releases),
            "sources": len(cfg.get("news", [])),
        },
    }

    out_path = args.out if os.path.isabs(args.out) else os.path.join(here, args.out)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(
        f"\nWrote {out_path}\n"
        f"  {payload['stats']['news']} news / "
        f"{payload['stats']['repos']} repos / "
        f"{payload['stats']['releases']} releases"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
