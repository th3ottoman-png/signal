#!/usr/bin/env python3
"""
Signal - feed fetcher

Pulls news, agent coverage, research, GitHub repos and releases into data/feed.json.
Zero dependencies. Python 3.9+. Runs locally or in GitHub Actions.

Usage:
    python3 fetch.py [--config sources.json] [--out data/feed.json]

Set GITHUB_TOKEN to lift GitHub API limits from 60/hr to 1000/hr.
"""

import argparse
import concurrent.futures as cf
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
TIMEOUT = 20
GITHUB_API = "https://api.github.com"

# Similarity threshold for merging two headlines into one story.
CLUSTER_THRESHOLD = 0.42
# Ignore tokens this common, they carry no signal for matching.
MAX_DF_RATIO = 0.04
# Two headlines must share at least this many distinctive words, and at least
# one of them must be genuinely rare (appears in <= this many headlines).
# Without the rarity rule, unrelated AI stories merge on words like
# "large language models" which are common across this entire corpus.
MIN_SHARED_TOKENS = 2
# Tuned empirically against a 482-item corpus. Below 12 the GPT-6 Astra story
# (7 outlets) fails to merge. Above 12 unrelated stories start merging on
# generic wording like "large language models".
RARE_DF = 12

# Hard cap on stories in the payload.
MAX_ITEMS = 400
# Every source gets at least this many slots before that cap applies. See
# apply_source_floor: recency-weighted ranking structurally starves anything
# that posts slower than daily, and a configured source that silently renders
# nothing is worse than one that is not configured at all.
SOURCE_FLOOR = 1

# Recency decay time constant, in hours. A story is ~1/e as relevant after
# this long. See source_cadence_tau for why this is a floor, not a constant.
RECENCY_TAU_HOURS = 30.0
# Ceiling on the per-source constant. Without it a yearly poster gets a
# year-long horizon and genuinely ancient posts float back to the top.
RECENCY_TAU_MAX_HOURS = 720.0


# ---------------------------------------------------------------- http

def http_get(url, accept=None):
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


# ---------------------------------------------------------------- text

def strip_tags(text):
    if not text:
        return ""
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(text, limit=280):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "..."


def parse_date(value):
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
    return tag.split("}", 1)[1] if "}" in tag else tag


def find_text(node, *names):
    wanted = set(names)
    for child in node:
        if localname(child.tag) in wanted:
            return child.text or ""
    return ""


def pick_link(node):
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


# ---------------------------------------------------------------- feeds

def fetch_feed(source):
    """Fetch one RSS or Atom feed and return normalised items."""
    raw = http_get(source["url"])
    root = ElementTree.fromstring(raw)

    entries = []
    queue = [root]
    while queue and not entries:
        node = queue.pop(0)
        for child in node:
            if localname(child.tag) in ("item", "entry"):
                entries.append(child)
        if not entries:
            queue.extend(list(node))

    limit = source.get("limit", 12)
    items = []
    for e in entries[:limit]:
        title = strip_tags(find_text(e, "title"))
        link = pick_link(e)
        if not title or not link:
            continue

        # Google News packs the publisher into the title: "Headline - Publisher"
        if source.get("trim_publisher") and " - " in title:
            title = title.rsplit(" - ", 1)[0].strip()

        summary = strip_tags(find_text(e, "description", "summary", "content", "encoded"))
        published = parse_date(find_text(e, "pubDate", "published", "updated", "date", "modified"))

        items.append({
            "title": title,
            "url": link,
            "source": source["name"],
            "category": source.get("category", "news"),
            "weight": source.get("weight", 3),
            "summary": truncate(summary),
            "published": iso(published) or iso(datetime.now(timezone.utc)),
        })
    return items


def fetch_hf_papers(source):
    """Hugging Face daily papers is JSON, not RSS."""
    data = json.loads(http_get(source["url"]).decode("utf-8"))
    items = []
    for row in data[: source.get("limit", 15)]:
        paper = row.get("paper") or {}
        pid = paper.get("id") or ""
        if not pid:
            continue
        title = (row.get("title") or paper.get("title") or "").strip()
        if not title:
            continue
        upvotes = paper.get("upvotes") or 0
        items.append({
            "title": title,
            "url": f"https://huggingface.co/papers/{pid}",
            "source": source["name"],
            "category": source.get("category", "research"),
            "weight": source.get("weight", 3),
            "summary": truncate(strip_tags(paper.get("summary") or row.get("summary") or "")),
            "published": row.get("publishedAt") or iso(datetime.now(timezone.utc)),
            "upvotes": upvotes,
        })
    return items


def collect_news(sources):
    """Fetch all feeds concurrently, then flatten in config order."""
    results = {}

    def work(src):
        try:
            return src["name"], fetch_feed(src), None
        except Exception as exc:
            return src["name"], [], f"{type(exc).__name__}: {exc}"

    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for name, items, err in ex.map(work, sources):
            results[name] = items
            if err:
                print(f"  [fail] {name:<30} {err}")
            else:
                print(f"  [ok]   {name:<30} {len(items)} items")

    ordered = []
    for src in sources:
        ordered.extend(results.get(src["name"], []))
    return ordered


# ---------------------------------------------------------------- GitHub

def resolve_query(query):
    today = datetime.now(timezone.utc).date()
    return re.sub(r"\{since_(\d+)d\}",
                  lambda m: (today - timedelta(days=int(m.group(1)))).isoformat(),
                  query)


def collect_discovery(queries):
    results = []
    for q in queries:
        query = resolve_query(q["query"])
        url = (f"{GITHUB_API}/search/repositories?q={urllib.parse.quote(query)}"
               f"&sort={q.get('sort','stars')}&order=desc&per_page={q.get('limit',10)}")
        try:
            data = http_json(url)
            repos = [{
                "full_name": r.get("full_name"),
                "url": r.get("html_url"),
                "description": truncate(r.get("description") or "", 200),
                "stars": r.get("stargazers_count", 0),
                "language": r.get("language") or "",
                "section": q["name"],
                "category": q.get("category", "repos"),
                "updated": r.get("pushed_at") or r.get("updated_at"),
                "topics": (r.get("topics") or [])[:4],
            } for r in data.get("items", [])]
            results.extend(repos)
            print(f"  [ok]   {q['name']:<30} {len(repos)} repos")
        except Exception as exc:
            print(f"  [fail] {q['name']:<30} {type(exc).__name__}: {exc}")
        time.sleep(2)
    return results


# Tag-based, not API-based, because the Atom feed carries no prerelease flag.
# The \b on "rc" matters: without it, words like "vscode" or "arch" false match.
PRERELEASE_HINT = re.compile(
    r"(alpha|beta|canary|nightly|preview|\brc[-.\d]|[.\-]dev|[.\-]pre)", re.I
)


def collect_releases(repos):
    """Latest release per repo, read from /releases.atom.

    Atom, not the REST API, on purpose. The API is capped at 60 requests/hour
    without a token and 15 release lookups burn a quarter of that budget every
    single run, which was enough to starve the whole section. The Atom feed
    carries the same data and is not rate limited.
    """
    results = []

    def one(full):
        url = f"https://github.com/{full}/releases.atom"
        try:
            root = ElementTree.fromstring(http_get(url, accept="application/atom+xml"))
        except urllib.error.HTTPError as exc:
            print(f"  [skip] {full:<34} HTTP {exc.code}")
            return None
        except Exception as exc:
            print(f"  [fail] {full:<34} {type(exc).__name__}: {exc}")
            return None

        for node in root:
            if localname(node.tag) != "entry":
                continue
            title = (find_text(node, "title") or "").strip()
            link = pick_link(node) or ""
            body = strip_tags(find_text(node, "content", "summary"))
            return {
                "repo": full,
                "tag_name": title,
                "name": title,
                "url": link,
                "published": iso(parse_date(find_text(node, "updated", "published"))),
                "prerelease": bool(PRERELEASE_HINT.search(title)),
                "body": truncate(body, 400),
            }
        print(f"  [skip] {full:<34} no releases")
        return None

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        for full, rel in zip(repos, pool.map(one, repos)):
            if not rel:
                continue
            results.append(rel)
            flag = " [pre]" if rel["prerelease"] else ""
            new_home = repo_in_url(rel["url"])
            warn = ""
            # A transferred repo redirects to its new home. Surface it so the
            # config gets corrected instead of silently tracking a dead path.
            if new_home and new_home.lower() != full.lower():
                warn = f"  MOVED -> {new_home} (update sources.json)"
            print(f"  [ok]   {full:<34} {rel['tag_name'][:26]}{flag}{warn}")
    return results


def repo_in_url(url):
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/releases", url or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------- clustering

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "than", "that", "this",
    "these", "those", "is", "are", "was", "were", "be", "been", "being", "have",
    "has", "had", "do", "does", "did", "will", "would", "could", "should", "can",
    "may", "might", "must", "of", "in", "on", "at", "to", "for", "with", "from",
    "by", "as", "into", "about", "after", "before", "over", "under", "again",
    "its", "it", "his", "her", "their", "our", "your", "new", "now", "how",
    "what", "when", "where", "who", "why", "all", "any", "both", "each", "more",
    "most", "other", "some", "such", "only", "own", "same", "too", "very",
    "just", "not", "out", "up", "down", "get", "gets", "got", "make", "makes",
    "made", "say", "says", "said", "want", "wants", "use", "uses", "using",
    "used", "way", "ways", "things", "thing", "you", "your", "myself", "also",
    "amid", "via", "per", "says", "report", "reports", "according",
}


def tokenize(title):
    t = re.sub(r"[^a-z0-9\s\-]", " ", title.lower())
    return [w for w in t.split() if len(w) > 2 and w not in STOPWORDS]


def build_clusters(items):
    """
    Group headlines describing the same event using IDF-weighted cosine
    similarity. Rare shared words (chatgpt, grok, claude) dominate the score,
    which is what makes cross-outlet matching work when the wording differs.
    """
    docs = [tokenize(it["title"]) for it in items]
    n = len(docs)
    if n == 0:
        return []

    df = Counter()
    for d in docs:
        for w in set(d):
            df[w] += 1

    # Skip tokens that appear nearly everywhere, they match nothing useful.
    max_df = max(3, int(n * MAX_DF_RATIO))
    useful = {w for w, c in df.items() if c <= max_df}

    vectors = []
    for d in docs:
        tf = Counter(w for w in d if w in useful)
        if not tf:
            vectors.append({})
            continue
        vec = {w: (1.0 + math.log(c)) * math.log((n + 1) / (df[w] + 1)) for w, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in vec.values())) or 1.0
        vectors.append({w: x / norm for w, x in vec.items()})

    # Candidate pairs: only compare docs sharing a rare token.
    buckets = {}
    for i, vec in enumerate(vectors):
        for w in sorted(vec, key=vec.get, reverse=True)[:6]:
            buckets.setdefault(w, []).append(i)

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    seen_pairs = set()
    for token, ids in buckets.items():
        if len(ids) > 40:  # token too common despite the df filter, skip
            continue
        for a in range(len(ids)):
            for b in range(a + 1, len(ids)):
                i, j = ids[a], ids[b]
                if (i, j) in seen_pairs:
                    continue
                seen_pairs.add((i, j))
                vi, vj = vectors[i], vectors[j]
                if not vi or not vj:
                    continue
                # dot product, smaller vector drives the loop
                if len(vi) > len(vj):
                    vi, vj = vj, vi
                shared = set(vi) & set(vj)
                if len(shared) < MIN_SHARED_TOKENS:
                    continue
                if min(df[w] for w in shared) > RARE_DF:
                    continue
                sim = sum(wt * vj[w] for w, wt in vi.items() if w in vj)
                if sim >= CLUSTER_THRESHOLD:
                    union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def score(item, cluster_size, now, tau_hours=RECENCY_TAU_HOURS):
    """Rank for the Top view: fresh, well-covered, from a trusted source."""
    published = parse_date(item.get("published"))
    age_hours = 999.0
    if published:
        age_hours = max(0.0, (now - published).total_seconds() / 3600.0)
    # decays over tau_hours, normally ~a day but per-source for slow posters
    recency = math.exp(-age_hours / tau_hours)
    coverage = min(cluster_size, 5) * 0.55          # more outlets = bigger story
    weight = item.get("weight", 3) * 0.5
    return round(recency * 3.0 + coverage + weight, 3)


# ---------------------------------------------------------------- main

def load_previous(path):
    """Read the last good feed so a failed run cannot blank a whole section."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def keep_last_good(new_list, old_list, label):
    if not new_list and old_list:
        print(f"  [warn] {label}: nothing returned, keeping {len(old_list)} from the previous run")
        return old_list
    return new_list


def dedupe(items, keyfn):
    seen, out = set(), []
    for it in items:
        k = keyfn(it)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def source_cadence_tau(items):
    """Per-source recency time constant, in hours.

    The default 30h decay quietly assumes every source posts daily. For a
    monthly poster that makes everything it publishes score ~0 forever:
    exp(-1488/30) is about 1e-21. Its best work can then never reach the feed
    no matter how high the weight, because weight tops out at 2.5 and the
    recency gap is around 3 points. Lilian Weng, Eugene Yan and EleutherAI all
    landed here: configured, fetching fine, rendering nothing.

    Scaling the constant to each source's own rhythm means "new for them"
    reads as fresh. The max() guard leaves daily sources on the default, so
    nothing about the fast feeds changes at all.
    """
    by_source = {}
    for it in items:
        by_source.setdefault(it["source"], []).append(it.get("published") or "")

    tau = {}
    for source, dates in by_source.items():
        stamps = sorted(d for d in dates if d)
        # Two posts is one gap, and one gap is not a cadence. Need three.
        if len(stamps) < 3:
            continue
        parsed = [p for p in (parse_date(d) for d in stamps) if p]
        if len(parsed) < 3:
            continue
        gaps = [g for g in ((b - a).total_seconds() / 3600.0
                            for a, b in zip(parsed, parsed[1:])) if g > 0]
        if not gaps:
            continue
        gaps.sort()
        tau[source] = min(RECENCY_TAU_MAX_HOURS,
                          max(RECENCY_TAU_HOURS, gaps[len(gaps) // 2]))
    return tau


def apply_source_floor(items, floor, cap):
    """Guarantee every source a minimum number of slots, then cap.

    `items` must already be sorted by score descending, since each source's
    reserved picks are taken in that order.

    The reserve has to come out of the cap *first*. Cutting to `cap` and then
    appending the floored items gets them trimmed straight back off by the very
    cap they are meant to survive, and the whole thing silently does nothing.
    """
    if floor <= 0:
        return items[:cap]

    by_source = {}
    for it in items:
        by_source.setdefault(it["source"], []).append(it)

    budget = max(0, cap - floor * len(by_source))
    ranked = list(items[:budget])
    taken = {id(it) for it in ranked}

    for source, group in by_source.items():
        have = sum(1 for it in group if id(it) in taken)
        for it in group[have:floor]:
            ranked.append(it)
            taken.add(id(it))

    # Sources already inside the budget never spent their reserved slot. Hand
    # the space back to the next best stories so the page ships full rather
    # than short.
    for it in items:
        if len(ranked) >= cap:
            break
        if id(it) not in taken:
            ranked.append(it)
            taken.add(id(it))

    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="sources.json")
    ap.add_argument("--out", default="data/feed.json")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    cfg_path = args.config if os.path.isabs(args.config) else os.path.join(here, args.config)
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    print(f"Fetching {len(cfg.get('news', []))} feeds...")
    raw = collect_news(cfg.get("news", []))

    print("Fetching JSON sources...")
    for js in cfg.get("json_sources", []):
        try:
            got = fetch_hf_papers(js)
            raw.extend(got)
            print(f"  [ok]   {js['name']:<30} {len(got)} items")
        except Exception as exc:
            print(f"  [fail] {js['name']:<30} {type(exc).__name__}: {exc}")

    out_path = args.out if os.path.isabs(args.out) else os.path.join(here, args.out)
    previous = load_previous(out_path) or {}

    print("Fetching GitHub discovery...")
    repos = dedupe(collect_discovery(cfg.get("discovery", [])), lambda x: x["full_name"])
    repos = keep_last_good(repos, previous.get("repos", []), "repos")

    print("Fetching latest releases...")
    releases = collect_releases(cfg.get("releases", []))
    releases = keep_last_good(releases, previous.get("releases", []), "releases")

    # ---- cluster, collapse, score
    print("Clustering duplicate stories...")
    raw = dedupe(raw, lambda x: x["url"].split("?")[0])
    raw = dedupe(raw, lambda x: x["title"].lower()[:90])

    now = datetime.now(timezone.utc)
    clusters = build_clusters(raw)
    # Computed from raw, before clustering collapses duplicates, because the
    # rhythm is a property of the source's own output, not of the merge.
    tau = source_cadence_tau(raw)

    items = []
    merged = 0
    for group in clusters:
        group_items = [raw[i] for i in group]
        # Primary: trusted source first, then freshest.
        # Two stable passes: newest first, then highest-weight source floats to
        # the top while keeping newest-first ordering within the same weight.
        group_items.sort(key=lambda x: x.get("published") or "", reverse=True)
        group_items.sort(key=lambda x: x.get("weight", 3), reverse=True)
        primary = dict(group_items[0])
        others = group_items[1:]
        merged += len(others)

        primary["cluster_size"] = len(group_items)
        primary["also"] = [{"source": o["source"], "url": o["url"], "title": o["title"]} for o in others]
        primary["score"] = score(primary, len(group_items), now,
                                 tau.get(primary["source"], RECENCY_TAU_HOURS))
        primary["id"] = hashlib.sha1(primary["url"].encode("utf-8")).hexdigest()[:12]
        items.append(primary)

    items.sort(key=lambda x: x["score"], reverse=True)
    items = keep_last_good(items, previous.get("items", []), "items")
    # Cap here rather than in the payload, so stats match what is actually served.
    items = apply_source_floor(items, SOURCE_FLOOR, MAX_ITEMS)

    categories = Counter(it["category"] for it in items)

    payload = {
        "generated_at": iso(now),
        "site": cfg.get("site", {}),
        "items": items,
        "repos": sorted(repos, key=lambda x: x.get("stars", 0), reverse=True),
        "releases": sorted(releases, key=lambda x: x.get("published") or "", reverse=True),
        "categories": dict(categories),
        "stats": {
            "items": len(items),
            "raw": len(raw),
            "merged": merged,
            "repos": len(repos),
            "releases": len(releases),
            "sources": len(cfg.get("news", [])) + len(cfg.get("json_sources", [])),
        },
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    s = payload["stats"]
    print(f"\nWrote {out_path}")
    print(f"  {s['raw']} raw -> {s['items']} stories ({s['merged']} duplicates merged)")
    print(f"  {s['repos']} repos, {s['releases']} releases")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
