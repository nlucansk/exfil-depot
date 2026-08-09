#!/usr/bin/env python3
"""Export the scraped catalogue as a static site for GitHub Pages.

Reads the archive directly (no database needed) and emits:

  site/index.html                the EXFIL UI with the static-mode flag injected
  site/data/summary.json         stats + categories (mirrors /api/stats)
  site/data/mods.json            all mod/addon summary rows (client-side search)
  site/data/detail/{kind}-{id}.json   full detail incl. versions + dependencies
  site/thumbs/*                  archived thumbnails (small; optional)

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

    # thumbnails (small images only)
    thumbs = {}
    tdir = data / "files" / "thumbnails"
    if tdir.is_dir() and not args.no_thumbs:
        (out / "thumbs").mkdir(exist_ok=True)
        for f in tdir.iterdir():
            if f.is_file() and f.stat().st_size < 2_000_000:
                shutil.copy2(f, out / "thumbs" / f.name)
                thumbs[f.name] = f"thumbs/{f.name}"

    rows, by_cat, activity = [], {}, {}
    total_versions = total_downloads = n_github = 0

    for kind in ("mod", "addon"):
        d = cat / f"{kind}s"
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json"), key=lambda p: int(p.stem)):
            m = load_json(f)
            versions = m.get("all_versions") or []
            lv = latest(versions)
            thumb = (m.get("thumbnail") or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            thumb_rel = thumbs.get(thumb)
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

            row = {
                "kind": kind, "id": m["id"], "name": m.get("name") or str(m["id"]),
                "slug": m.get("slug"), "teaser": m.get("teaser"), "owner_name": (m.get("owner") or {}).get("name"),
                "downloads": m.get("downloads") or 0, "favourites": m.get("favourites_count") or 0,
                "featured": bool(m.get("featured")), "contains_ads": bool(m.get("contains_ads")),
                "contains_ai_content": bool(m.get("contains_ai_content")), "cheat_notice": bool(m.get("cheat_notice")),
                "license_name": (m.get("license") or {}).get("name"), "guid": m.get("guid"),
                "thumbnail_local": thumb_rel, "category": cat_title, "category_id": m.get("category_id"),
                "updated_at": m.get("updated_at"), "created_at": m.get("created_at"),
                "published_at": m.get("published_at"),
                "latest_constraint": (lv or {}).get("spt_version_constraint"),
                "latest_version": (lv or {}).get("version"),
                "has_local": False,
                "has_github": any(v["github_url"] for v in det_versions),
            }
            rows.append(row)

            detail = dict(row)
            detail.update({
                "license_link": (m.get("license") or {}).get("link"),
                "description_html": clean_html(m.get("description")),
                "versions": det_versions,
                "source_code_links": [
                    {"url": (l.get("url") if isinstance(l, dict) else str(l)), "label": (l.get("label") if isinstance(l, dict) else None)}
                    for l in (m.get("source_code_links") or [])
                ],
            })
            (out / "data" / "detail" / f"{kind}-{m['id']}.json").write_text(
                json.dumps(detail, ensure_ascii=False), encoding="utf-8")

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
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "static": True,
    }
    (out / "data" / "summary.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    (out / "data" / "mods.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    # frontend with static flag injected
    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    html = html.replace("<script>\n\"use strict\";", "<script>window.EXFIL_STATIC=true;</script>\n<script>\n\"use strict\";", 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"[export] {n_mods} mods + {n_addons} addons, {total_versions} versions, "
          f"{len(thumbs)} thumbs -> {out} ({size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
