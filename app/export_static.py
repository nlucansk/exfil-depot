#!/usr/bin/env python3
"""Export the scraped catalogue as a static site for GitHub Pages.

Reads the archive directly (no database needed) and emits:

  site/index.html                the EXFIL UI with the static-mode flag injected
  site/data/summary.json         stats + categories + audit counts
  site/data/mods.json            all mod/addon summary rows (client-side search)
  site/data/detail/{kind}-{id}.json   full detail incl. versions + dependencies
  site/data/audit.json           validity report (see below)
  site/thumbs/*                  archived thumbnails (small; optional)

Audit pass (computed over the whole catalogue):
  - no_repo      mod has no recoverable source repository
  - obtainable   at least one version is acquirable post-shutdown
                 (local archive file, GitHub release, or external link)
  - dependency health: every dependency edge checked - target missing from
    the catalogue, target unobtainable, and cycle detection
  - required_by  reverse-dependency count (load-bearing mods)

Mod FILES are deliberately NOT exported: a public static site must not
redistribute other authors' archives. GitHub/external links are kept.

Usage:  python app/export_static.py [--data <archive>] [--out site]
"""

import argparse
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

SCRIPTY = re.compile(r"(?is)<(script|iframe|object|embed|form)\b.*?</\1>|<(script|iframe|object|embed|form)\b[^>]*/?>")
ON_ATTR = re.compile(r'(?i)\son\w+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)')


def clean_html(html):
    if not html:
        return None
    return ON_ATTR.sub("", SCRIPTY.sub("", str(html)))


def load_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def parse_label(label):
    m = re.match(r"^(mod|addon) (\d+) .+ v(.+)$", label or "")
    return (m.group(1), int(m.group(2)), m.group(3)) if m else (None, None, None)


def parse_repo(url):
    try:
        u = urlparse(url or "")
    except ValueError:
        return None
    if u.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2 or parts[0].lower() in ("orgs", "topics", "search"):
        return None
    return f"{parts[0]}/{re.sub(r'[.]git$', '', parts[1])}"


def load_link_log(data: Path, name: str) -> dict:
    out = {}
    p = data / name
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind, mid, ver = parse_label(rec.get("label", ""))
        url = rec.get("github_url") or rec.get("final_url")
        if kind and url:
            out[(kind, mid, ver)] = url
    return out


def latest(versions):
    if not versions:
        return None
    return max(versions, key=lambda v: (v.get("published_at") or v.get("created_at") or ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=r"C:\Users\rdp_local\test\spt-archive\forge")
    ap.add_argument("--out", default="site")
    ap.add_argument("--no-thumbs", action="store_true")
    args = ap.parse_args()

    data = Path(args.data)
    cat = data / "catalogue"
    out = Path(args.out)
    (out / "data" / "detail").mkdir(parents=True, exist_ok=True)

    github = load_link_log(data, "github-links.jsonl")
    external = load_link_log(data, "failures.jsonl")
    categories = {c["id"]: c for c in load_json(cat / "categories.json")}

    # thumbnails (small images only); keep any already exported (CI runs
    # scrape metadata only and would otherwise drop committed thumbs)
    thumbs = {}
    if (out / "thumbs").is_dir() and not args.no_thumbs:
        thumbs = {f.name: f"thumbs/{f.name}" for f in (out / "thumbs").iterdir() if f.is_file()}
    tdir = data / "files" / "thumbnails"
    if tdir.is_dir() and not args.no_thumbs:
        (out / "thumbs").mkdir(exist_ok=True)
        for f in tdir.iterdir():
            if f.is_file() and f.stat().st_size < 2_000_000:
                shutil.copy2(f, out / "thumbs" / f.name)
                thumbs[f.name] = f"thumbs/{f.name}"

    # ------------------------------------------------------------------ pass 1
    rows, details = [], {}
    by_cat, activity = {}, {}
    total_versions = total_downloads = n_github = 0

    for kind in ("mod", "addon"):
        d = cat / f"{kind}s"
        if not d.is_dir():
            continue
        # license / category / source_code_links only ride on the index
        idx_file = cat / f"{kind}s-index.json"
        index_by_id = {x["id"]: x for x in load_json(idx_file)} if idx_file.exists() else {}
        for f in sorted(d.glob("*.json"), key=lambda p: int(p.stem)):
            m = load_json(f)
            idx = index_by_id.get(m["id"], {})
            for key_ in ("license", "category", "source_code_links"):
                if not m.get(key_) and idx.get(key_):
                    m[key_] = idx[key_]

            versions = m.get("all_versions") or []
            lv = latest(versions)
            thumb = (m.get("thumbnail") or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            cat_title = (categories.get(m.get("category_id")) or {}).get("title")

            det_versions = []
            for v in versions:
                key = (kind, m["id"], v.get("version"))
                gh_url = github.get(key)
                if gh_url:
                    n_github += 1
                det_versions.append({
                    "version": v.get("version"), "spt_version_constraint": v.get("spt_version_constraint"),
                    "content_length": v.get("content_length"), "downloads": v.get("downloads") or 0,
                    "published_at": v.get("published_at"), "forge_link": v.get("link"),
                    "github_url": gh_url, "external_url": external.get(key), "local_path": None,
                    "fika_compatibility": v.get("fika_compatibility"),
                    "dependencies": [
                        {"id": dep.get("id"), "name": dep.get("name"), "slug": dep.get("slug")}
                        for dep in (v.get("dependencies") or []) if isinstance(dep, dict)
                    ],
                })
            total_versions += len(det_versions)
            total_downloads += m.get("downloads") or 0
            if kind == "mod":
                by_cat[cat_title or "Uncategorized"] = by_cat.get(cat_title or "Uncategorized", 0) + 1
                year = str(m.get("updated_at") or "")[:4]
                if year:
                    activity[year] = activity.get(year, 0) + 1

            repos = sorted({r for r in (
                [parse_repo((l.get("url") if isinstance(l, dict) else str(l)))
                 for l in (m.get("source_code_links") or [])]
                + [parse_repo(v["github_url"]) for v in det_versions]
            ) if r})

            row = {
                "kind": kind, "id": m["id"], "name": m.get("name") or str(m["id"]),
                "slug": m.get("slug"), "teaser": m.get("teaser"), "owner_name": (m.get("owner") or {}).get("name"),
                "downloads": m.get("downloads") or 0, "favourites": m.get("favourites_count") or 0,
                "featured": bool(m.get("featured")), "contains_ads": bool(m.get("contains_ads")),
                "contains_ai_content": bool(m.get("contains_ai_content")), "cheat_notice": bool(m.get("cheat_notice")),
                "license_name": (m.get("license") or {}).get("name"), "guid": m.get("guid"),
                "thumbnail_local": thumbs.get(thumb), "category": cat_title, "category_id": m.get("category_id"),
                "updated_at": m.get("updated_at"), "created_at": m.get("created_at"),
                "published_at": m.get("published_at"),
                "latest_constraint": (lv or {}).get("spt_version_constraint"),
                "latest_version": (lv or {}).get("version"),
                "has_local": False,
                "has_github": any(v["github_url"] for v in det_versions),
            }
            detail = dict(row)
            detail.update({
                "license_link": (m.get("license") or {}).get("link"),
                "description_html": clean_html(m.get("description")),
                "versions": det_versions,
                "repos": repos,
                "source_code_links": [
                    {"url": (l.get("url") if isinstance(l, dict) else str(l)),
                     "label": (l.get("label") if isinstance(l, dict) else None)}
                    for l in (m.get("source_code_links") or [])
                ],
            })
            rows.append(row)
            details[(kind, m["id"])] = detail

    # ------------------------------------------------------------------ pass 2: audit
    mod_ids = {d["id"] for (k, _), d in details.items() if k == "mod"}

    def obtainable(detail):
        # acquirable after the hub's shutdown: an archived file, a resolved
        # GitHub/external link, or a live source repository (its releases
        # page). Mods failing ALL of these exist only in the archive.
        return bool(detail["repos"]) or any(
            v["github_url"] or v["external_url"] or v["local_path"] for v in detail["versions"])

    obtain = {key: obtainable(d) for key, d in details.items()}

    # dependency edges from each mod's LATEST version (current relevance)
    required_by = {}
    edges = {}
    for key, d in details.items():
        lv = latest(d["versions"])
        deps = (lv or {}).get("dependencies") or []
        edges[key] = [dep["id"] for dep in deps if dep.get("id") is not None]
        for dep in deps:
            if dep.get("id") is not None:
                required_by[dep["id"]] = required_by.get(dep["id"], 0) + 1

    # cycle detection over mod-kind edges
    cycles, state = [], {}

    def dfs(node, path):
        state[node] = 1
        for nxt in edges.get(("mod", node), []):
            if nxt not in mod_ids:
                continue
            if state.get(nxt) == 1:
                cycles.append(path[path.index(nxt):] + [nxt] if nxt in path else [node, nxt])
            elif state.get(nxt) is None:
                dfs(nxt, path + [nxt])
        state[node] = 2

    for (k, mid) in details:
        if k == "mod" and state.get(mid) is None:
            dfs(mid, [mid])

    audit = {"no_repo": [], "unobtainable": [], "no_versions": [],
             "missing_deps": [], "at_risk_deps": [], "cycles": cycles}

    for key, d in details.items():
        kind, mid = key
        d["required_by"] = required_by.get(mid, 0) if kind == "mod" else 0
        no_repo = not d["repos"]
        ok = obtain[key]
        for v in d["versions"]:
            for dep in v["dependencies"]:
                if dep.get("id") is None:
                    continue
                if dep["id"] not in mod_ids:
                    dep["status"] = "missing"
                    audit["missing_deps"].append({"kind": kind, "id": mid, "mod": d["name"],
                                                  "version": v["version"], "dep_id": dep["id"],
                                                  "dep_name": dep.get("name")})
                elif not obtain.get(("mod", dep["id"])):
                    dep["status"] = "at-risk"
                    audit["at_risk_deps"].append({"kind": kind, "id": mid, "mod": d["name"],
                                                  "version": v["version"], "dep_id": dep["id"],
                                                  "dep_name": dep.get("name")})
                else:
                    dep["status"] = "ok"
        lv = latest(d["versions"])
        dep_bad = sum(1 for dep in ((lv or {}).get("dependencies") or []) if dep.get("status") in ("missing", "at-risk"))
        for target in (d,):
            target["no_repo"] = no_repo
            target["obtainable"] = ok
            target["dep_problems"] = dep_bad
        for r in rows:
            if r["kind"] == kind and r["id"] == mid:
                r["no_repo"] = no_repo
                r["obtainable"] = ok
                r["dep_problems"] = dep_bad
                r["required_by"] = d["required_by"]
                break
        if no_repo:
            audit["no_repo"].append({"kind": kind, "id": mid, "mod": d["name"]})
        if not ok:
            audit["unobtainable"].append({"kind": kind, "id": mid, "mod": d["name"]})
        if not d["versions"]:
            audit["no_versions"].append({"kind": kind, "id": mid, "mod": d["name"]})

    audit["top_required"] = sorted(
        ({"id": mid, "name": details[("mod", mid)]["name"], "required_by": n,
          "obtainable": obtain.get(("mod", mid), False)}
         for mid, n in required_by.items() if ("mod", mid) in details),
        key=lambda x: -x["required_by"])[:25]

    # ------------------------------------------------------------------ pass 3: write
    for (kind, mid), d in details.items():
        (out / "data" / "detail" / f"{kind}-{mid}.json").write_text(
            json.dumps(d, ensure_ascii=False), encoding="utf-8")

    n_mods = sum(1 for r in rows if r["kind"] == "mod")
    n_addons = sum(1 for r in rows if r["kind"] == "addon")
    summary = {
        "mods": n_mods, "addons": n_addons, "versions": total_versions,
        "total_downloads": total_downloads, "local_files": 0, "github_hosted": n_github,
        "by_category": [{"title": k, "n": v} for k, v in sorted(by_cat.items(), key=lambda x: -x[1])],
        "activity": [{"year": int(y), "n": n} for y, n in sorted(activity.items()) if y.isdigit()],
        "categories": [
            {"id": c["id"], "title": c["title"],
             "mod_count": sum(1 for r in rows if r["kind"] == "mod" and r["category_id"] == c["id"])}
            for c in sorted(categories.values(), key=lambda c: c["title"])
        ],
        "audit": {
            "no_repo": len(audit["no_repo"]),
            "unobtainable": len(audit["unobtainable"]),
            "no_versions": len(audit["no_versions"]),
            "missing_deps": len(audit["missing_deps"]),
            "at_risk_deps": len(audit["at_risk_deps"]),
            "cycles": len(cycles),
        },
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "static": True,
    }
    (out / "data" / "summary.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    (out / "data" / "mods.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    audit["generated_at"] = summary["scraped_at"]
    (out / "data" / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=1), encoding="utf-8")

    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace("<script>\n\"use strict\";", "<script>window.EXFIL_STATIC=true;</script>\n<script>\n\"use strict\";", 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"[export] {n_mods} mods + {n_addons} addons, {total_versions} versions, "
          f"{len(thumbs)} thumbs -> {out} ({size / 1e6:.1f} MB)")
    print(f"[audit ] no_repo={summary['audit']['no_repo']} unobtainable={summary['audit']['unobtainable']} "
          f"no_versions={summary['audit']['no_versions']} missing_deps={summary['audit']['missing_deps']} "
          f"at_risk_deps={summary['audit']['at_risk_deps']} cycles={len(cycles)}")


if __name__ == "__main__":
    main()
