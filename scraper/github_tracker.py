#!/usr/bin/env python3
"""Post-hub update tracker: watch mod source repos on GitHub.

The hub snapshot (site/data/detail/*.json) is the seed. This script:

  1. VERSIONS  - maps every mod to its GitHub repo(s) (source_code_links +
     resolved release URLs), batch-queries the GitHub GraphQL API for the
     latest releases, and flags mods whose repo has a release NEWER than the
     last version the hub ever saw.
  2. DISCOVERY - runs a set of GitHub repo searches and reports repos that are
     not referenced anywhere in the snapshot: candidate new mods.

Stateless: output (site/data/updates.json) is recomputed from scratch each
run, so it never drifts. Requires GITHUB_TOKEN (in Actions the built-in token
works; locally: $env:GITHUB_TOKEN = gh auth token).

Usage: python scraper/github_tracker.py --site site
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

API = "https://api.github.com"
SEARCH_QUERIES = [
    "topic:spt", "topic:spt-aki", "topic:spt-mod", "topic:sptarkov",
    '"spt-aki" in:name,description', '"sp-tarkov" in:name,description',
    '"single player tarkov" in:name,description',
    "spt tarkov in:name,description", "spt mod in:name,description",
    "tarkov bepinex in:name,description",
]

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def parse_repo(url: str):
    """https://github.com/Owner/Repo[/anything] -> 'owner/repo' (lowercased)."""
    try:
        u = urlparse(url)
    except ValueError:
        return None
    if u.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    if owner.lower() in ("orgs", "topics", "search", "settings"):
        return None
    return f"{owner}/{repo}".lower(), f"{owner}/{repo}"


def gh_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "exfil-depot-tracker",
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
    })
    return s


def graphql(s: requests.Session, query: str) -> dict:
    for attempt in range(4):
        r = s.post(f"{API}/graphql", json={"query": query}, timeout=(10, 120))
        if r.status_code in (502, 503, 429):
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("graphql retries exhausted")


def fetch_releases(s: requests.Session, repos: list[str]) -> dict:
    """{canonical_repo: {pushedAt, isArchived, releases:[...]}} via batched GraphQL."""
    out = {}
    CHUNK = 80
    for start in range(0, len(repos), CHUNK):
        chunk = repos[start:start + CHUNK]
        parts = []
        for i, full in enumerate(chunk):
            owner, name = full.split("/", 1)
            parts.append(
                f'r{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{'
                f' nameWithOwner isArchived pushedAt'
                f' releases(first: 3, orderBy: {{field: CREATED_AT, direction: DESC}}) {{'
                f' nodes {{ tagName name publishedAt isPrerelease url }} }} }}'
            )
        body = graphql(s, "query {\n" + "\n".join(parts) + "\n}")
        data = body.get("data") or {}
        for i, full in enumerate(chunk):
            node = data.get(f"r{i}")
            if node:
                out[full.lower()] = node
        time.sleep(1)
        print(f"[versions] {min(start + CHUNK, len(repos))}/{len(repos)} repos checked")
    return out


def search_repos(s: requests.Session, query: str) -> list[dict]:
    r = s.get(f"{API}/search/repositories",
              params={"q": query, "per_page": 50, "sort": "updated"}, timeout=(10, 60))
    if r.status_code != 200:
        print(f"[discover] search failed for [{query}]: HTTP {r.status_code}")
        return []
    time.sleep(2.2)  # authenticated search: 30/min
    return r.json().get("items", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="site")
    ap.add_argument("--max-updates", type=int, default=200)
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN (or GH_TOKEN) is required")
    s = gh_session(token)

    detail_dir = Path(args.site) / "data" / "detail"
    if not detail_dir.is_dir():
        raise SystemExit(f"no snapshot at {detail_dir} - run export_static.py first")

    # --- map repos -> mods from the snapshot --------------------------------
    repo_mods: dict[str, list[dict]] = {}
    repo_display: dict[str, str] = {}
    for f in detail_dir.glob("*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        urls = [l.get("url") for l in d.get("source_code_links") or []]
        urls += [v.get("github_url") for v in d.get("versions") or []]
        last_forge = max((v.get("published_at") or "" for v in d.get("versions") or []), default="")
        for url in urls:
            if not url:
                continue
            parsed = parse_repo(url)
            if not parsed:
                continue
            key, display = parsed
            repo_display[key] = display
            entry = {"kind": d["kind"], "id": d["id"], "name": d["name"],
                     "last_forge_version_at": last_forge}
            bucket = repo_mods.setdefault(key, [])
            if not any(e["kind"] == entry["kind"] and e["id"] == entry["id"] for e in bucket):
                bucket.append(entry)

    known = set(repo_mods)
    print(f"[map] {len(known)} distinct source repos across {sum(len(v) for v in repo_mods.values())} mod links")

    # --- phase 1: releases newer than the hub snapshot ----------------------
    releases = fetch_releases(s, sorted(repo_display[k] for k in known))
    updates = []
    for key, node in releases.items():
        mods = repo_mods.get(key, [])
        if not mods:
            continue
        rels = [r for r in (node.get("releases") or {}).get("nodes") or [] if r and r.get("publishedAt")]
        if not rels:
            continue
        newest = max(rels, key=lambda r: r["publishedAt"])
        cutoff = max(m["last_forge_version_at"] for m in mods)
        if cutoff and newest["publishedAt"] > cutoff:
            updates.append({
                "repo": node.get("nameWithOwner") or repo_display[key],
                "tag": newest.get("tagName"), "title": newest.get("name"),
                "published_at": newest["publishedAt"], "url": newest.get("url"),
                "prerelease": bool(newest.get("isPrerelease")),
                "repo_archived": bool(node.get("isArchived")),
                "mods": [{"kind": m["kind"], "id": m["id"], "name": m["name"]} for m in mods],
            })
    updates.sort(key=lambda u: u["published_at"], reverse=True)
    updates = updates[:args.max_updates]
    print(f"[versions] {len(updates)} repos have releases newer than the hub snapshot")

    # linked repos GraphQL could not resolve: deleted or renamed on GitHub
    dead_repos = [
        {"repo": repo_display[k],
         "mods": [{"kind": m["kind"], "id": m["id"], "name": m["name"]} for m in repo_mods[k]]}
        for k in sorted(known) if k not in releases
    ]
    print(f"[versions] {len(dead_repos)} linked repos no longer resolve (deleted or renamed)")

    # --- phase 2: discovery -------------------------------------------------
    discovered, seen = [], set()
    for q in SEARCH_QUERIES:
        for it in search_repos(s, q):
            key = it["full_name"].lower()
            if key in known or key in seen:
                continue
            seen.add(key)
            discovered.append({
                "repo": it["full_name"], "url": it["html_url"],
                "description": (it.get("description") or "")[:200],
                "stars": it.get("stargazers_count") or 0,
                "pushed_at": it.get("pushed_at"), "created_at": it.get("created_at"),
                "matched_query": q,
            })
    discovered.sort(key=lambda d: (d["pushed_at"] or ""), reverse=True)
    discovered = discovered[:100]
    print(f"[discover] {len(discovered)} repos not present in the snapshot")

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repos_tracked": len(known),
        "updates": updates,
        "discovered": discovered,
        "dead_repos": dead_repos,
    }
    dest = Path(args.site) / "data" / "updates.json"
    dest.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"[done] wrote {dest}")


if __name__ == "__main__":
    main()
