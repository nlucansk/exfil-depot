# Self-hosting EXFIL Mod Depot

EXFIL serves a **scraped archive** of a community mod hub: catalogue metadata
(mods, versions, resolved dependencies) in Postgres, plus locally preserved
files and thumbnails. It ships as containers only — no bare-metal setup.

## 0. What you need first: the archive data

The depot renders whatever archive you mount. The upstream hub shut down on
**2026-08-12**; scraping it is no longer possible after that date. You need a
data directory (referred to below as *the archive*) with this layout, produced
by [`scraper/forge_scraper.py`](../scraper/forge_scraper.py) while the hub was
alive:

```
forge/
  catalogue/            # mods/, addons/, categories.json, spt-versions.json
  files/                # downloaded mod files + thumbnails (optional but recommended)
  github-links.jsonl    # resolved GitHub release URLs (optional)
  failures.jsonl        # author-hosted externals (optional)
```

Only `catalogue/` is required; everything else enriches the UI (LOCAL FILE /
GITHUB links per version).

## 1. Docker Compose (simplest)

```bash
git clone <this repo> && cd <repo>
cp .env.example .env          # point FORGE_DATA_PATH at your archive
docker compose up -d --build
```

Open <http://localhost:8484>. The importer runs automatically on startup and
skips work when the database already matches the archive.

**After updating the archive** (new scrape data copied in):

```bash
docker compose exec -e FORCE_REIMPORT=1 app python importer.py
```

## 2. Kubernetes (Helm)

The chart lives in [`charts/exfil-depot`](../charts/exfil-depot). It deploys
the app plus a single-replica Postgres StatefulSet with a PVC.

### Image

Pushes to `main` build `ghcr.io/<owner>/exfil-depot:latest` via GitHub Actions.
To build your own instead:

```bash
docker build -t registry.example.com/exfil-depot:1.0 ./app
docker push registry.example.com/exfil-depot:1.0
```

### Getting the archive into the cluster

Option A — hostPath (single-node clusters like k3s):

```bash
helm install depot charts/exfil-depot \
  --set forgeData.hostPath=/srv/forge \
  --set postgres.password="$(openssl rand -hex 16)"
```

Option B — PVC: create a PVC, copy the archive into it, then reference it:

```bash
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata: { name: forge-archive }
spec:
  accessModes: [ReadWriteOnce]
  resources: { requests: { storage: 30Gi } }
EOF
# temporary pod to fill it:
kubectl run copier --image=busybox --restart=Never \
  --overrides='{"spec":{"volumes":[{"name":"v","persistentVolumeClaim":{"claimName":"forge-archive"}}],"containers":[{"name":"copier","image":"busybox","command":["sleep","3600"],"volumeMounts":[{"name":"v","mountPath":"/data"}]}]}}'
kubectl cp ./forge copier:/data/
kubectl delete pod copier

helm install depot charts/exfil-depot --set forgeData.existingClaim=forge-archive
```

### Exposing it

```bash
helm upgrade depot charts/exfil-depot --reuse-values \
  --set ingress.enabled=true \
  --set ingress.className=traefik \
  --set ingress.host=depot.example.internal
```

Without ingress: `kubectl port-forward svc/depot-exfil-depot 8484:80`.

The startup probe allows up to 15 minutes for the first import of a large
catalogue before the pod is considered failed.

## 3. Operations

- **Re-import**: see NOTES printed by `helm install`, or the compose command above.
- **Backup**: dump the DB (`pg_dump -U exfil exfil`) and back up the archive
  directory itself — the archive is the source of truth; the DB can always be
  rebuilt from it.
- **Direct SQL**: `docker compose exec db psql -U exfil exfil` (tables: `mods`,
  `mod_versions`, `version_dependencies`, `categories`, `spt_versions`; original
  scraped JSON is kept in `raw` JSONB columns).

## 4. Access control & sharing — read this

The app has **no authentication**. It is designed for personal/LAN use.

- Keep it on a private network, or put it behind an authenticating reverse
  proxy (basic auth, Authelia, Tailscale, ...).
- The archived files under `/files/...` are **other authors' work**, preserved
  for personal continuity after the hub's shutdown. Exposing the depot — and
  especially the file downloads — to the public internet redistributes those
  files without their authors' permission. Don't do that; if you must share
  beyond yourself, share the catalogue only (unmount `files/`) or obtain
  permission per mod.
- The UI and this project deliberately carry no game-publisher trademarks;
  keep it that way in your fork (see the repo README).
