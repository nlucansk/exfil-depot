#!/usr/bin/env python3
"""Import the scraped Forge catalogue (JSON files) into Postgres.

Idempotent: upserts everything, safe to re-run. Skips entirely when the DB
already holds as many mods as the catalogue directory (set FORCE_REIMPORT=1
to override, e.g. after the scraper has fetched more data).

Data layout expected under $FORGE_DATA (produced by forge_scraper.py):
  catalogue/{mods,addons}/{id}.json     one file per mod, incl. all_versions
  catalogue/{mods,addons}-index.json    listing incl. license/category/links
  catalogue/categories.json, spt-versions.json
  files/{mods,addons}/{id}-{slug}/{version}/<archive>   downloaded files
  files/thumbnails/<basename>
  github-links.jsonl, failures.jsonl    resolved external hosting
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

DB_URL = os.environ.get("DATABASE_URL", "postgresql://exfil:exfil@localhost:5432/exfil")
DATA = Path(os.environ.get("FORGE_DATA", "/data/forge"))
FORCE = os.environ.get("FORCE_REIMPORT", "0") == "1"

SCRIPTY = re.compile(r"(?is)<(script|iframe|object|embed|form)\b.*?</\1>|<(script|iframe|object|embed|form)\b[^>]*/?>")
ON_ATTR = re.compile(r'(?i)\son\w+\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)')


def clean_html(html):
    if not html:
        return None
    return ON_ATTR.sub("", SCRIPTY.sub("", str(html)))


def connect_with_retry(retries: int = 30) -> psycopg.Connection:
    for i in range(retries):
        try:
            return psycopg.connect(DB_URL)
        except psycopg.OperationalError:
            print(f"[importer] waiting for database ({i + 1}/{retries})...")
            time.sleep(2)
    raise SystemExit("database never became ready")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def parse_label(label: str):
    """'mod 123 some-slug v1.2.0' -> ('mod', 123, '1.2.0')"""
    m = re.match(r"^(mod|addon) (\d+) .+ v(.+)$", label or "")
    return (m.group(1), int(m.group(2)), m.group(3)) if m else (None, None, None)


def scan_local_files(kind: str) -> dict:
    """{(mod_id, version_dirname): web_path} for downloaded archives."""
    out = {}
    root = DATA / "files" / f"{kind}s"
    if not root.is_dir():
        return out
    for mod_dir in root.iterdir():
        m = re.match(r"^(\d+)-", mod_dir.name)
        if not m or not mod_dir.is_dir():
            continue
        mid = int(m.group(1))
        for vdir in mod_dir.iterdir():
            if not vdir.is_dir():
                continue
            files = [f for f in vdir.iterdir() if f.is_file() and not f.name.endswith(".part")]
            if files:
                f = max(files, key=lambda x: x.stat().st_size)
                out[(mid, vdir.name)] = f"/files/{kind}s/{mod_dir.name}/{vdir.name}/{f.name}"
    return out


def load_link_log(name: str) -> dict:
    """{(kind, mod_id, version): url} from github-links.jsonl / failures.jsonl."""
    out = {}
    p = DATA / name
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


def ts(v):
    return v or None


def import_kind(cur, kind: str, index_by_id: dict, thumbs: dict,
                github: dict, external: dict) -> tuple[int, int]:
    cat_dir = DATA / "catalogue" / f"{kind}s"
    if not cat_dir.is_dir():
        return 0, 0
    local_files = scan_local_files(kind)
    n_mods = n_vers = 0

    for f in sorted(cat_dir.glob("*.json"), key=lambda p: int(p.stem)):
        d = load_json(f)
        idx = index_by_id.get(d["id"], {})
        license_ = d.get("license") or idx.get("license") or {}
        category = d.get("category") or idx.get("category") or {}
        owner = d.get("owner") or {}
        thumb = d.get("thumbnail") or ""
        thumb_local = thumbs.get(thumb.split("?")[0].rstrip("/").rsplit("/", 1)[-1]) if thumb else None
        fika = d.get("fika_compatibility")

        cur.execute("""
            INSERT INTO mods (id, kind, hub_id, guid, name, slug, teaser, description_html,
                              thumbnail_url, thumbnail_local, downloads, favourites, detail_url,
                              featured, contains_ads, contains_ai_content, cheat_notice,
                              fika_compatibility, category_id, license_name, license_link,
                              owner_id, owner_name, published_at, created_at, updated_at, raw)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (kind, id) DO UPDATE SET
                name = EXCLUDED.name, slug = EXCLUDED.slug, teaser = EXCLUDED.teaser,
                description_html = EXCLUDED.description_html,
                thumbnail_url = EXCLUDED.thumbnail_url, thumbnail_local = EXCLUDED.thumbnail_local,
                downloads = EXCLUDED.downloads, favourites = EXCLUDED.favourites,
                featured = EXCLUDED.featured, category_id = EXCLUDED.category_id,
                license_name = EXCLUDED.license_name, license_link = EXCLUDED.license_link,
                owner_id = EXCLUDED.owner_id, owner_name = EXCLUDED.owner_name,
                fika_compatibility = EXCLUDED.fika_compatibility,
                published_at = EXCLUDED.published_at, updated_at = EXCLUDED.updated_at,
                raw = EXCLUDED.raw
        """, (
            d["id"], kind, d.get("hub_id"), d.get("guid"), d.get("name") or d.get("slug") or str(d["id"]),
            d.get("slug"), d.get("teaser"), clean_html(d.get("description")),
            thumb or None, thumb_local, d.get("downloads") or 0, d.get("favourites_count") or 0,
            d.get("detail_url"), bool(d.get("featured")), bool(d.get("contains_ads")),
            bool(d.get("contains_ai_content")), bool(d.get("cheat_notice")),
            str(fika) if fika is not None else None,
            (category or {}).get("id") or d.get("category_id"),
            (license_ or {}).get("name"), (license_ or {}).get("link"),
            owner.get("id"), owner.get("name"),
            ts(d.get("published_at")), ts(d.get("created_at")), ts(d.get("updated_at")),
            Jsonb({k: v for k, v in d.items() if k != "all_versions"}),
        ))
        n_mods += 1

        for link in (d.get("source_code_links") or idx.get("source_code_links") or []):
            url = link.get("url") if isinstance(link, dict) else str(link)
            if url:
                cur.execute("""
                    INSERT INTO source_code_links (kind, mod_id, url, label)
                    VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING
                """, (kind, d["id"], url, (link.get("label") if isinstance(link, dict) else None)))

        for v in d.get("all_versions") or []:
            ver = v.get("version") or str(v.get("id"))
            ver_dir = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", ver).strip(" .")[:150]
            key = (kind, d["id"], ver)
            cur.execute("""
                INSERT INTO mod_versions (id, kind, mod_id, version, description_html, forge_link,
                                          github_url, external_url, local_path, content_length,
                                          spt_version_constraint, downloads, fika_compatibility,
                                          published_at, created_at, raw)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (kind, id) DO UPDATE SET
                    version = EXCLUDED.version, description_html = EXCLUDED.description_html,
                    forge_link = EXCLUDED.forge_link, github_url = EXCLUDED.github_url,
                    external_url = EXCLUDED.external_url, local_path = EXCLUDED.local_path,
                    content_length = EXCLUDED.content_length,
                    spt_version_constraint = EXCLUDED.spt_version_constraint,
                    downloads = EXCLUDED.downloads, published_at = EXCLUDED.published_at,
                    raw = EXCLUDED.raw
            """, (
                v["id"], kind, d["id"], ver, clean_html(v.get("description")), v.get("link"),
                github.get(key), external.get(key), local_files.get((d["id"], ver_dir)),
                v.get("content_length"), v.get("spt_version_constraint"),
                v.get("downloads") or 0,
                str(v.get("fika_compatibility")) if v.get("fika_compatibility") is not None else None,
                ts(v.get("published_at")), ts(v.get("created_at")),
                Jsonb({k: x for k, x in v.items() if k != "dependencies"}),
            ))
            n_vers += 1

            for dep in v.get("dependencies") or []:
                if isinstance(dep, dict) and dep.get("id") is not None:
                    cur.execute("""
                        INSERT INTO version_dependencies (kind, version_id, dep_mod_id, dep_name, dep_slug, dep_guid)
                        VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                    """, (kind, v["id"], dep["id"], dep.get("name"), dep.get("slug"), dep.get("guid")))
    return n_mods, n_vers


def main():
    cat = DATA / "catalogue"
    if not cat.is_dir():
        print(f"[importer] no catalogue at {cat} - nothing to import (app still starts)")
        return

    n_disk = len(list((cat / "mods").glob("*.json"))) + len(list((cat / "addons").glob("*.json")))
    with connect_with_retry() as conn:
        conn.execute(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        conn.commit()
        n_db = conn.execute("SELECT count(*) FROM mods").fetchone()[0]
        if n_db == n_disk and n_db > 0 and not FORCE:
            print(f"[importer] up to date ({n_db} mods+addons) - skipping (FORCE_REIMPORT=1 to override)")
            return

        with conn.cursor() as cur:
            for c in load_json(cat / "categories.json"):
                cur.execute("""
                    INSERT INTO categories (id, hub_id, title, slug, description)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, slug = EXCLUDED.slug
                """, (c["id"], c.get("hub_id"), c["title"], c.get("slug"), c.get("description")))
            for v in load_json(cat / "spt-versions.json"):
                cur.execute("""
                    INSERT INTO spt_versions (id, version, version_major, version_minor, version_patch,
                                              mod_count, link, color_class)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET mod_count = EXCLUDED.mod_count
                """, (v["id"], v["version"], v.get("version_major"), v.get("version_minor"),
                      v.get("version_patch"), v.get("mod_count"), v.get("link"), v.get("color_class")))

            thumbs = {}
            tdir = DATA / "files" / "thumbnails"
            if tdir.is_dir():
                thumbs = {f.name: f"/thumbnails/{f.name}" for f in tdir.iterdir() if f.is_file()}

            github = load_link_log("github-links.jsonl")
            external = load_link_log("failures.jsonl")

            totals = {}
            for kind in ("mod", "addon"):
                idx_file = cat / f"{kind}s-index.json"
                index_by_id = {m["id"]: m for m in load_json(idx_file)} if idx_file.exists() else {}
                totals[kind] = import_kind(cur, kind, index_by_id, thumbs, github, external)

            cur.execute("""
                INSERT INTO meta (key, value) VALUES ('imported_at', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (datetime.now(timezone.utc).isoformat(),))
            newest = max((f.stat().st_mtime for f in (cat / "mods").glob("*.json")), default=0)
            cur.execute("""
                INSERT INTO meta (key, value) VALUES ('scraped_at', %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (datetime.fromtimestamp(newest, timezone.utc).isoformat() if newest else "",))
        conn.commit()
    print(f"[importer] done: mods {totals['mod']}, addons {totals['addon']} (items, versions)")


if __name__ == "__main__":
    sys.exit(main())
