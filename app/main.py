#!/usr/bin/env python3
"""EXFIL Mod Depot - API + frontend for the self-hosted Forge archive."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DB_URL = os.environ.get("DATABASE_URL", "postgresql://exfil:exfil@localhost:5432/exfil")
DATA = Path(os.environ.get("FORGE_DATA", "/data/forge"))

pool: ConnectionPool | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global pool
    pool = ConnectionPool(DB_URL, min_size=1, max_size=8, kwargs={"row_factory": dict_row})
    pool.wait()
    yield
    pool.close()


app = FastAPI(title="EXFIL Mod Depot", lifespan=lifespan)

SORTS = {
    "downloads": "m.downloads DESC NULLS LAST",
    "updated": "m.updated_at DESC NULLS LAST",
    "created": "m.created_at DESC NULLS LAST",
    "name": "lower(m.name) ASC",
}


def q_all(sql: str, params=()):
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()


def q_one(sql: str, params=()):
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchone()


@app.get("/api/stats")
def stats():
    counts = q_one("""
        SELECT
          count(*) FILTER (WHERE kind = 'mod')                          AS mods,
          count(*) FILTER (WHERE kind = 'addon')                        AS addons,
          coalesce(sum(downloads), 0)                                   AS total_downloads
        FROM mods
    """)
    versions = q_one("SELECT count(*) AS n FROM mod_versions")["n"]
    local = q_one("SELECT count(*) AS n FROM mod_versions WHERE local_path IS NOT NULL")["n"]
    ghosted = q_one("SELECT count(*) AS n FROM mod_versions WHERE github_url IS NOT NULL")["n"]
    by_cat = q_all("""
        SELECT coalesce(c.title, 'Uncategorized') AS title, count(*) AS n
        FROM mods m LEFT JOIN categories c ON c.id = m.category_id
        WHERE m.kind = 'mod'
        GROUP BY 1 ORDER BY n DESC
    """)
    activity = q_all("""
        SELECT extract(year FROM updated_at)::int AS year, count(*) AS n
        FROM mods WHERE updated_at IS NOT NULL
        GROUP BY 1 ORDER BY 1
    """)
    meta = {r["key"]: r["value"] for r in q_all("SELECT key, value FROM meta")}
    return {
        "mods": counts["mods"], "addons": counts["addons"], "versions": versions,
        "total_downloads": int(counts["total_downloads"]),
        "local_files": local, "github_hosted": ghosted,
        "by_category": by_cat, "activity": activity,
        "imported_at": meta.get("imported_at"), "scraped_at": meta.get("scraped_at"),
    }


@app.get("/api/categories")
def categories():
    return q_all("""
        SELECT c.id, c.title, c.slug, count(m.id) AS mod_count
        FROM categories c LEFT JOIN mods m ON m.category_id = c.id AND m.kind = 'mod'
        GROUP BY c.id ORDER BY c.title
    """)


@app.get("/api/spt-versions")
def spt_versions():
    return q_all("SELECT * FROM spt_versions ORDER BY version_major DESC, version_minor DESC, version_patch DESC")


@app.get("/api/mods")
def mods(
    q: str = "",
    kind: str = Query("mod", pattern="^(mod|addon|all)$"),
    category: int | None = None,
    sort: str = Query("downloads", pattern="^(downloads|updated|created|name)$"),
    page: int = Query(0, ge=0),
    per_page: int = Query(25, ge=1, le=200),
    featured: bool = False,
    no_license: bool = False,
    local_only: bool = False,
):
    where, params = ["TRUE"], []
    if kind != "all":
        where.append("m.kind = %s")
        params.append(kind)
    if q.strip():
        where.append("(m.name ILIKE %s OR m.teaser ILIKE %s OR m.owner_name ILIKE %s OR m.slug ILIKE %s OR m.guid ILIKE %s)")
        like = f"%{q.strip()}%"
        params += [like, like, like, like, like]
    if category is not None:
        where.append("m.category_id = %s")
        params.append(category)
    if featured:
        where.append("m.featured")
    if no_license:
        where.append("(m.license_name IS NULL OR m.license_name = '')")
    if local_only:
        where.append("EXISTS (SELECT 1 FROM mod_versions v WHERE v.kind = m.kind AND v.mod_id = m.id AND v.local_path IS NOT NULL)")
    w = " AND ".join(where)

    total = q_one(f"SELECT count(*) AS n FROM mods m WHERE {w}", params)["n"]
    rows = q_all(f"""
        SELECT m.kind, m.id, m.name, m.slug, m.teaser, m.owner_name, m.downloads,
               m.favourites, m.featured, m.contains_ads, m.contains_ai_content,
               m.cheat_notice, m.license_name, m.thumbnail_local, m.thumbnail_url,
               m.updated_at, m.created_at, m.published_at,
               c.title AS category,
               lv.spt_version_constraint AS latest_constraint,
               lv.version AS latest_version,
               (lv.local_path IS NOT NULL) AS has_local,
               (lv.github_url IS NOT NULL) AS has_github
        FROM mods m
        LEFT JOIN categories c ON c.id = m.category_id
        LEFT JOIN LATERAL (
            SELECT v.* FROM mod_versions v
            WHERE v.kind = m.kind AND v.mod_id = m.id
            ORDER BY v.published_at DESC NULLS LAST, v.created_at DESC NULLS LAST
            LIMIT 1
        ) lv ON TRUE
        WHERE {w}
        ORDER BY {SORTS[sort]}, m.id
        LIMIT %s OFFSET %s
    """, params + [per_page, page * per_page])
    return {"total": total, "page": page, "per_page": per_page, "items": rows}


@app.get("/api/mods/{kind}/{mod_id}")
def mod_detail(kind: str, mod_id: int):
    if kind not in ("mod", "addon"):
        raise HTTPException(404)
    mod = q_one("""
        SELECT m.*, c.title AS category
        FROM mods m LEFT JOIN categories c ON c.id = m.category_id
        WHERE m.kind = %s AND m.id = %s
    """, (kind, mod_id))
    if not mod:
        raise HTTPException(404, "not found")
    versions = q_all("""
        SELECT v.id, v.version, v.description_html, v.forge_link, v.github_url,
               v.external_url, v.local_path, v.content_length, v.spt_version_constraint,
               v.downloads, v.fika_compatibility, v.published_at
        FROM mod_versions v
        WHERE v.kind = %s AND v.mod_id = %s
        ORDER BY v.published_at DESC NULLS LAST, v.created_at DESC NULLS LAST
    """, (kind, mod_id))
    for v in versions:
        v["dependencies"] = q_all("""
            SELECT d.dep_mod_id AS id, d.dep_name AS name, d.dep_slug AS slug
            FROM version_dependencies d
            WHERE d.kind = %s AND d.version_id = %s
        """, (kind, v["id"]))
    mod["versions"] = versions
    mod["source_code_links"] = q_all(
        "SELECT url, label FROM source_code_links WHERE kind = %s AND mod_id = %s", (kind, mod_id))
    mod.pop("raw", None)
    return mod


# --- static: archived files + thumbnails + frontend --------------------------

if (DATA / "files").is_dir():
    app.mount("/files", StaticFiles(directory=DATA / "files"), name="files")
if (DATA / "files" / "thumbnails").is_dir():
    app.mount("/thumbnails", StaticFiles(directory=DATA / "files" / "thumbnails"), name="thumbnails")

STATIC = Path(__file__).parent / "static"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
