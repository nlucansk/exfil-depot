#!/usr/bin/env python3
"""Archive forge.sp-tarkov.com (SPT mod hub) before its 2026-08-12 shutdown.

Uses the Forge's open read-only v0 API (discovered from the mirrored source:
sp-tarkov/forge, routes/api_v0.php) rather than HTML scraping.

Phases:
  metadata  - full catalogue: every mod + addon, all versions, resolved
              dependencies, descriptions, licenses, source links, VT links.
  download  - mod/addon files (latest version per mod by default; use
              --all-versions for every version) + thumbnails. Verifies sizes,
              records SHA-256 hashes, resumable (re-run to continue).
  all       - metadata, then download.

Usage:
  python forge_scraper.py all
  python forge_scraper.py metadata
  python forge_scraper.py download --all-versions --max-gb 250
  python forge_scraper.py all --limit 3          # smoke test

Politeness: single connection, --delay between request starts (default 0.7 s,
~85 req/min vs the origin's 300/min limit), honors Retry-After on 429.
"""

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

GITHUB_HOSTS = ("github.com", "githubusercontent.com", "codeload.github.com")


def is_github(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in GITHUB_HOSTS)

BASE = "https://forge.sp-tarkov.com/api/v0"
UA = "spt-forge-archiver/1.0 (personal preservation; forge closes 2026-08-12)"
DEFAULT_OUT = r"C:\Users\rdp_local\test\spt-archive\forge"

# Windows console may not be UTF-8; mod names are.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class Api:
    def __init__(self, delay: float):
        self.delay = delay
        self.last = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept": "application/json"})

    def pace(self):
        wait = self.last + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()

    def get_json(self, path: str, params: dict | None = None) -> dict:
        url = path if path.startswith("http") else BASE + path
        for attempt in range(1, 7):
            self.pace()
            try:
                r = self.session.get(url, params=params, timeout=(10, 120))
            except requests.RequestException as e:
                print(f"    network error ({e.__class__.__name__}), retry {attempt}/6")
                time.sleep(min(5 * 2 ** attempt, 120))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                wait = int(r.headers.get("Retry-After", 0)) or min(5 * 2 ** attempt, 120)
                print(f"    HTTP {r.status_code}, backing off {wait}s (attempt {attempt}/6)")
                time.sleep(wait)
                continue
            if r.status_code == 400:
                raise RuntimeError(f"Bad query for {url}: {r.text[:300]}")
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Gave up on {url} after 6 attempts")

    def paginated(self, path: str, params: dict | None = None):
        """Yield items across all pages of a v0 listing endpoint."""
        params = dict(params or {})
        params.setdefault("per_page", 50)
        page = 1
        while True:
            params["page"] = page
            body = self.get_json(path, params)
            data = body.get("data", [])
            yield from data
            meta = body.get("meta", {})
            if page >= int(meta.get("last_page", page)) or not data:
                return
            page += 1


def atomic_write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def sanitize(name: str, fallback: str = "unnamed") -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name)).strip(" .")
    return (name or fallback)[:150]


# --------------------------------------------------------------------------
# Phase 1: metadata
# --------------------------------------------------------------------------

def fetch_catalogue(api: Api, out: Path, kind: str, limit: int | None):
    """kind: 'mods' or 'addons'. Fetches index + per-item detail & versions."""
    single = "mod" if kind == "mods" else "addon"
    cat = out / "catalogue"
    items_dir = cat / kind
    items_dir.mkdir(parents=True, exist_ok=True)

    index_path = cat / f"{kind}-index.json"
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
        print(f"[{kind}] index cached: {len(index)} entries")
    else:
        print(f"[{kind}] fetching index...")
        inc = "license,category,source_code_links" if kind == "mods" else None
        index = list(api.paginated(f"/{kind}", {"include": inc} if inc else None))
        atomic_write_json(index_path, index)
        print(f"[{kind}] index: {len(index)} entries")

    todo = index[:limit] if limit else index
    done = skipped = 0
    for i, summary in enumerate(todo, 1):
        mid = summary["id"]
        item_path = items_dir / f"{mid}.json"
        if item_path.exists():
            skipped += 1
            continue
        try:
            detail = api.get_json(f"/{single}/{mid}").get("data", {})
            # addon versions only allow the virus_total_links include (no dependencies)
            inc = "dependencies,virus_total_links" if kind == "mods" else "virus_total_links"
            versions = list(api.paginated(
                f"/{single}/{mid}/versions",
                {"include": inc},
            ))
        except Exception as e:
            # one broken record must not kill the run - log it and move on
            print(f"[{kind}] SKIP id={mid}: {e.__class__.__name__}: {e}")
            with (cat / "skipped.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"kind": kind, "id": mid, "error": str(e)}) + "\n")
            continue
        detail["all_versions"] = versions
        atomic_write_json(item_path, detail)
        done += 1
        if done % 25 == 0 or i == len(todo):
            print(f"[{kind}] {i}/{len(todo)} processed ({skipped} already cached)")
    print(f"[{kind}] complete: {done} fetched, {skipped} cached")
    return index


def latest_version(versions: list[dict]) -> dict | None:
    if not versions:
        return None
    return max(versions, key=lambda v: (v.get("published_at") or v.get("created_at") or ""))


def phase_metadata(api: Api, out: Path, limit: int | None, addons: bool):
    cat = out / "catalogue"
    cat.mkdir(parents=True, exist_ok=True)

    if not (cat / "spt-versions.json").exists():
        atomic_write_json(cat / "spt-versions.json", list(api.paginated("/spt/versions")))
    if not (cat / "categories.json").exists():
        atomic_write_json(cat / "categories.json", list(api.paginated("/mod-categories")))
    print("[dictionaries] spt versions + categories saved")

    fetch_catalogue(api, out, "mods", limit)
    if addons:
        fetch_catalogue(api, out, "addons", limit)

    # summary with size projections
    total_latest = total_all = n_versions = 0
    for f in (cat / "mods").glob("*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        vs = d.get("all_versions", [])
        n_versions += len(vs)
        total_all += sum(v.get("content_length") or 0 for v in vs)
        lv = latest_version(vs)
        if lv:
            total_latest += lv.get("content_length") or 0
    summary = {
        "mods": len(list((cat / "mods").glob("*.json"))),
        "mod_versions": n_versions,
        "bytes_latest_versions": total_latest,
        "gb_latest_versions": round(total_latest / 1e9, 2),
        "bytes_all_versions": total_all,
        "gb_all_versions": round(total_all / 1e9, 2),
    }
    atomic_write_json(cat / "summary.json", summary)
    print(f"[summary] {json.dumps(summary)}")


# --------------------------------------------------------------------------
# Phase 2: files
# --------------------------------------------------------------------------

def download_file(api: Api, url: str, dest_dir: Path, expected: int | None,
                  label: str, manifest, failures, github_links=None) -> int:
    """Download one file. Returns bytes written (0 if skipped/failed).

    If github_links is a list, files that resolve to GitHub hosting are NOT
    downloaded; the resolved URL is recorded there instead (GitHub survives the
    Forge shutdown, and redirects are followed header-only before any body
    transfer, so skipping costs nothing).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    # skip fast if any previously-downloaded file matches the expected size
    if expected:
        for existing in dest_dir.iterdir():
            if existing.is_file() and existing.stat().st_size == expected:
                return 0
    for attempt in range(1, 4):
        api.pace()
        try:
            with api.session.get(url, stream=True, timeout=(10, 300), allow_redirects=True) as r:
                if github_links is not None and is_github(r.url):
                    github_links.append({"label": label, "url": url, "github_url": r.url.split("?")[0]})
                    return 0
                if r.status_code != 200:
                    if attempt == 3:
                        failures.append({"label": label, "url": url, "status": r.status_code})
                        print(f"    FAIL {label}: HTTP {r.status_code}")
                    time.sleep(5 * attempt)
                    continue
                ctype = r.headers.get("Content-Type", "")
                if "text/html" in ctype:
                    # not a file: an external page (e.g. mega/gdrive) - record for manual pickup
                    failures.append({"label": label, "url": url, "final_url": r.url,
                                     "reason": "external-page"})
                    print(f"    EXTERNAL {label} -> {r.url}")
                    return 0
                cd = r.headers.get("Content-Disposition", "")
                m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^\";]+)', cd)
                fname = sanitize(m.group(1) if m else r.url.split("?")[0].rstrip("/").rsplit("/", 1)[-1],
                                 fallback="download.bin")
                dest = dest_dir / fname
                if dest.exists() and expected and dest.stat().st_size == expected:
                    return 0
                part = dest.with_suffix(dest.suffix + ".part")
                sha = hashlib.sha256()
                size = 0
                with open(part, "wb") as fh:
                    for chunk in r.iter_content(1024 * 256):
                        fh.write(chunk)
                        sha.update(chunk)
                        size += len(chunk)
                if expected and size != expected:
                    print(f"    size mismatch {label}: got {size}, expected {expected} (kept anyway)")
                part.replace(dest)
                manifest.append({"label": label, "file": str(dest.relative_to(dest_dir.parents[1])),
                                 "size": size, "sha256": sha.hexdigest(), "url": url,
                                 "final_url": r.url})
                return size
        except requests.RequestException as e:
            if attempt == 3:
                failures.append({"label": label, "url": url, "error": str(e)})
                print(f"    FAIL {label}: {e}")
            else:
                time.sleep(10 * attempt)
    return 0


def phase_download(api: Api, out: Path, all_versions: bool, max_gb: float,
                   limit: int | None, addons: bool, skip_github: bool = True):
    cat = out / "catalogue"
    budget = int(max_gb * 1e9)
    written = 0
    manifest: list[dict] = []
    failures: list[dict] = []
    github_links: list[dict] | None = [] if skip_github else None

    kinds = ["mods"] + (["addons"] if addons else [])
    for kind in kinds:
        files_root = out / "files" / kind
        thumbs = out / "files" / "thumbnails"
        items = sorted((cat / kind).glob("*.json"), key=lambda p: int(p.stem))
        if limit:
            items = items[:limit]
        print(f"[download:{kind}] {len(items)} items, "
              f"{'ALL versions' if all_versions else 'latest version each'}")
        for i, f in enumerate(items, 1):
            d = json.loads(f.read_text(encoding="utf-8"))
            slug = sanitize(d.get("slug") or d.get("name") or f.stem)
            versions = d.get("all_versions", [])
            targets = versions if all_versions else ([latest_version(versions)] if versions else [])
            for v in targets:
                if not v or not v.get("link"):
                    continue
                vdir = files_root / f"{d['id']}-{slug}" / sanitize(v.get("version") or str(v["id"]))
                written += download_file(api, v["link"], vdir, v.get("content_length"),
                                         f"{kind[:-1]} {d['id']} {slug} v{v.get('version')}",
                                         manifest, failures, github_links)
                if written > budget:
                    print(f"[download] size cap reached ({max_gb} GB) - stopping. "
                          f"Re-run with a higher --max-gb to continue (resumable).")
                    break
            thumb = d.get("thumbnail")
            if thumb:
                # download_file names by Content-Disposition/URL basename; mirror that here
                tdest = thumbs / sanitize(thumb.split("?")[0].rstrip("/").rsplit("/", 1)[-1])
                if not tdest.exists():
                    download_file(api, thumb, thumbs, None, f"thumb {d['id']}", [], [])
            if i % 25 == 0 or i == len(items):
                print(f"[download:{kind}] {i}/{len(items)} | {written/1e9:.2f} GB this run")
            if written > budget:
                break
        if written > budget:
            break

    (out / "files-manifest.jsonl").open("a", encoding="utf-8").write(
        "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in manifest))
    if failures:
        (out / "failures.jsonl").open("a", encoding="utf-8").write(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in failures))
    if github_links:
        (out / "github-links.jsonl").open("a", encoding="utf-8").write(
            "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in github_links))
    print(f"[download] done: {written/1e9:.2f} GB this run, "
          f"{len(manifest)} files, {len(github_links or [])} github-hosted (links recorded), "
          f"{len(failures)} failures/external (see failures.jsonl)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("phase", choices=["metadata", "download", "all"])
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--delay", type=float, default=0.7, help="seconds between request starts")
    ap.add_argument("--all-versions", action="store_true", help="download every version, not just latest")
    ap.add_argument("--max-gb", type=float, default=250.0, help="stop downloads past this many GB (resumable)")
    ap.add_argument("--limit", type=int, default=None, help="process only the first N mods (smoke test)")
    ap.add_argument("--no-addons", action="store_true")
    ap.add_argument("--with-github-files", action="store_true",
                    help="also download files hosted on GitHub (default: record the link only)")
    args = ap.parse_args()

    out = Path(args.out)
    api = Api(args.delay)
    t0 = time.monotonic()
    if args.phase in ("metadata", "all"):
        phase_metadata(api, out, args.limit, addons=not args.no_addons)
    if args.phase in ("download", "all"):
        phase_download(api, out, args.all_versions, args.max_gb, args.limit,
                       addons=not args.no_addons,
                       skip_github=not args.with_github_files)
    print(f"[done] {time.monotonic() - t0:.0f} s elapsed")


if __name__ == "__main__":
    main()
