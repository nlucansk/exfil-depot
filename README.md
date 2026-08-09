# EXFIL — Mod Depot

Fully containerized, self-hosted archive browser for a community game-mod hub
that shut down on 2026-08-12. Browse the preserved catalogue — every mod,
every version, resolved dependency graphs, categories, licenses — backed by
your own database, with locally archived files served alongside.

- **db** — Postgres 17: `mods`, `mod_versions`, `version_dependencies`,
  `categories`, `spt_versions`, original JSON kept in `raw` JSONB columns.
- **app** — FastAPI: JSON API + archived files/thumbnails + the EXFIL web UI
  (single-file, dependency-free, dark industrial design). Imports the scraped
  catalogue automatically and idempotently at startup.
- **scraper** — the Python scraper that produced the archive while the hub was
  alive (kept for reference; the hub is gone).

## Quickstart

```bash
cp .env.example .env   # point FORGE_DATA_PATH at your scraped archive
docker compose up -d --build
```

Open <http://localhost:8484>.

Kubernetes? `helm install depot charts/exfil-depot ...` — see
**[docs/SELF-HOSTING.md](docs/SELF-HOSTING.md)** for the full guide (compose,
Helm, getting data into the cluster, re-imports, backups, access control).

## Static / GitHub Pages mode

The same UI also runs with **no backend at all**: `app/export_static.py` reads
the archive and emits `site/` — pre-computed JSON (summary, searchable index,
per-mod detail with versions + dependencies) plus thumbnails, with search,
filters and sorting done client-side. `.github/workflows/pages.yml` deploys
`site/` to GitHub Pages on push.

```bash
python app/export_static.py            # regenerate site/ from the archive
git add site && git commit -m "update static export" && git push
```

Deliberate limitation: the static export contains **catalogue metadata and
links only** — never the archived mod files. A public page must not
redistribute other authors' work; per-version GitHub/external links point to
the authors' own hosting instead.

## API

- `GET /api/stats` — totals, category breakdown, activity histogram
- `GET /api/mods?q=&kind=mod|addon|all&category=&sort=downloads|updated|created|name&page=&per_page=&featured=&no_license=&local_only=`
- `GET /api/mods/{kind}/{id}` — full detail: versions, dependencies, source links
- `GET /api/categories` · `GET /api/spt-versions`
- `GET /files/...` · `GET /thumbnails/...` — the preserved archive

## Data

The depot renders a scraped archive directory (`catalogue/` + optional
`files/`). It ships **without data** — the archive contains third-party mod
files and stays out of git. See the self-hosting guide for the expected layout.

## Naming

The project is deliberately branded **EXFIL** and its UI, metadata and docs
carry no game-publisher trademarks — the upstream hub was shut down over
exactly that. Mod names inside your scraped data render as-is (factual data);
keep the product surfaces clean in forks.

## License / intent

Personal-use preservation tooling. The archived mod files belong to their
authors — see [docs/SELF-HOSTING.md](docs/SELF-HOSTING.md) §4 before exposing
an instance beyond yourself.
