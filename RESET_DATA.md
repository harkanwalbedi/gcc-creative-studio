# Resetting Creative Studio Data Without Breaking the Setup (`main`)

This runbook and companion script ([scripts/reset_data.sh](scripts/reset_data.sh)) clear **all user and generated data** from a Creative Studio deployment (either **Cloud GCP** or **Local Docker Compose**) while keeping the platform **100% usable** immediately afterward.

---

## Why Naive Wipes Break Creative Studio (and How This Avoids Them)

Simply running `DROP SCHEMA public CASCADE` or emptying every table and bucket leaves Creative Studio broken. To keep the app usable, **five invariants** must hold:

1. **Preserve `alembic_version` and `unified_gallery_view`:**
   - Truncate only the 12 application data tables with `TRUNCATE TABLE ... RESTART IDENTITY CASCADE` — **never** drop tables, views, or `alembic_version`.
2. **Preserve Bucket Resource & IAM Bindings:**
   - Delete only **objects** inside `gs://<GENMEDIA_BUCKET>/**` (`gcloud storage rm "gs://<BUCKET>/**"`), **never** the bucket itself (`gs://<BUCKET>`), which would destroy Terraform-managed IAM bindings for `SIGNING_SA_EMAIL` and Cloud Run.
   - Never touch `*-tfstate` or `*_cloudbuild` buckets, Secret Manager secrets, OAuth clients, or Firebase Auth configuration.
3. **Never Run `bootstrap.bootstrap` with `ADMIN_USER_EMAIL="system"`:**
   - In `backend/src/config/config_service.py`, `ADMIN_USER_EMAIL` defaults to `"system"`. When `ADMIN_USER_EMAIL == "system"`, `backend/bootstrap/bootstrap.py` **exits 0 while silently skipping**:
     - Admin user creation
     - Default **`PUBLIC` workspace** creation (without a public workspace, users cannot generate images/videos)
     - **VTO system garments & models** (`system_assets/vto/*`)
     - **Gallery templates** (`media_template_assets/*`)
   - Always pass a real admin email (`ADMIN_USER_EMAIL="you@domain.com"`).
4. **Always Pass the Live `GENMEDIA_BUCKET` to `bootstrap.bootstrap`:**
   - If `GENMEDIA_BUCKET` is omitted when running `bootstrap.py` from a shell, `ConfigService` falls back to `<PROJECT_ID>-assets` instead of Terraform's `<PROJECT_ID>-cs-development-bucket`, causing 404 signed URLs on all templates and VTO garments.
5. **Clean Up GCP Cloud Workflows (`id-<uuid>`):**
   - Creating a workflow in Creative Studio also deploys a managed workflow in **Google Cloud Workflows**. Query `SELECT id FROM workflows` *before* truncating PostgreSQL so only Creative Studio's `id-<uuid>` workflows are deleted.

---

## Option 1: Automated Script (`scripts/reset_data.sh`)

If your checkout already includes `scripts/reset_data.sh`, make sure it is executable (or download it directly into any existing `main` checkout):

```bash
# If pulling into an existing clone created by bootstrap.sh, disable sparse-checkout first:
git sparse-checkout disable 2>/dev/null || true

# Download scripts/reset_data.sh if your fork does not have it yet:
curl -fsSL --create-dirs -o scripts/reset_data.sh \
  https://raw.githubusercontent.com/harkanwalbedi/gcc-creative-studio/refs/heads/main/scripts/reset_data.sh

chmod +x scripts/reset_data.sh
```

### A. Cloud GCP Deployment (Cloud SQL + GCS + Cloud Workflows)

1. **Dry Run first** (inspects Cloud Run, row counts, workflows, and custom buckets with zero changes):
   ```bash
   ./scripts/reset_data.sh \
     --mode cloud \
     --project <GCP_PROJECT_ID> \
     --region us-central1 \
     --admin-email <ADMIN_EMAIL> \
     --dry-run
   ```

2. **Execute Full Reset + Re-Seed** (takes an on-demand Cloud SQL backup first, wipes DB + GCS objects + Creative Studio Cloud Workflows, re-seeds baseline data, and verifies usability invariants):
   ```bash
   ./scripts/reset_data.sh \
     --mode cloud \
     --project <GCP_PROJECT_ID> \
     --region us-central1 \
     --admin-email <ADMIN_EMAIL>
   ```

### B. Local Docker Compose Setup

1. **Dry Run:**
   ```bash
   ./scripts/reset_data.sh --mode local --dry-run
   ```

2. **Reset Local Postgres Only (Recommended if sharing a team dev GCS bucket):**
   ```bash
   ./scripts/reset_data.sh \
     --mode local \
     --skip-gcs \
     --skip-workflows \
     --admin-email <ADMIN_EMAIL>
   ```

3. **Reset Local Postgres + Configured GCS Bucket + Workflows:**
   ```bash
   ./scripts/reset_data.sh \
     --mode local \
     --admin-email <ADMIN_EMAIL>
   ```

---

## Option 2: Manual Step-by-Step Commands

If you prefer running each step manually without the wrapper script:

### Manual Reset — Local Docker Compose (`main`)

```bash
# 1. Optional backup of local Postgres
docker compose exec -T postgres pg_dump -U studio_user -d creative_studio \
  > /tmp/creative-studio-backup-$(date -u +%Y%m%dT%H%M%SZ).sql

# 2. Truncate ONLY the 12 data tables (keeps alembic_version & unified_gallery_view)
docker compose exec -T postgres psql -U studio_user -d creative_studio -c '
TRUNCATE TABLE
  media_item_tags,
  source_asset_tags,
  tags,
  workflow_runs,
  workflows,
  brand_guidelines,
  media_items,
  source_assets,
  media_templates,
  workspace_members,
  workspaces,
  users
RESTART IDENTITY CASCADE;'

# 3. (Optional) Empty ONLY objects inside your dedicated local GENMEDIA_BUCKET
# WARNING: Skip if sharing this bucket with teammates or another environment!
gcloud storage rm "gs://<YOUR_LOCAL_GENMEDIA_BUCKET>/**" --quiet || true

# 4. Re-seed admin user, default PUBLIC workspace, VTO system assets & templates
# IMPORTANT: ADMIN_USER_EMAIL must NOT be "system"
docker compose exec -T \
  -e ADMIN_USER_EMAIL="<YOUR_ADMIN_EMAIL>" \
  -e PYTHONPATH=/app \
  backend /app/.venv/bin/python -m bootstrap.bootstrap
```

### Manual Reset — Cloud GCP Deployment (`main`)

Run from Cloud Shell or an authenticated workstation at the root of `gcc-creative-studio` (`main` branch):

```bash
export PROJECT_ID="<GCP_PROJECT_ID>"
export REGION="us-central1"
export BACKEND_SERVICE="cstudio-be"
export ADMIN_USER_EMAIL="<YOUR_ADMIN_EMAIL>" # Must NOT be "system"

# 1. Read exact GENMEDIA_BUCKET and Cloud SQL connection name from Cloud Run
export GENMEDIA_BUCKET=$(gcloud run services describe "$BACKEND_SERVICE" \
  --region="$REGION" --project="$PROJECT_ID" --format=json \
  | jq -r '.spec.template.spec.containers[0].env[] | select(.name=="GENMEDIA_BUCKET") | .value')

export INSTANCE_CONNECTION_NAME=$(gcloud sql instances list \
  --project="$PROJECT_ID" --filter="name:creative-studio-db*" \
  --format="value(connectionName)" | head -n 1)
export SQL_INSTANCE_SHORT="${INSTANCE_CONNECTION_NAME##*:}"

# 2. Create a pre-reset Cloud SQL backup
gcloud sql backups create \
  --instance="$SQL_INSTANCE_SHORT" \
  --project="$PROJECT_ID" \
  --description="pre-reset-$(date -u +%Y%m%dT%H%M%SZ)"

# 3. Start Cloud SQL Auth Proxy on port 15432 (avoids colliding with local 5432)
curl -fsSL -o /tmp/cloud-sql-proxy \
  https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.14.1/cloud-sql-proxy.linux.amd64
chmod +x /tmp/cloud-sql-proxy
/tmp/cloud-sql-proxy --address 127.0.0.1 --port 15432 "$INSTANCE_CONNECTION_NAME" &
PROXY_PID=$!
sleep 3

# 4. Fetch DB password
export DB_PASS=$(gcloud secrets versions access latest \
  --secret="creative-studio-db-password" --project="$PROJECT_ID")

# 5. Delete Creative Studio GCP Cloud Workflows recorded in DB, then truncate data tables
PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -p 15432 -U studio_user -d creative_studio -Atc \
  "SELECT id FROM workflows;" | while read -r wf_id; do
    [ -n "$wf_id" ] && gcloud workflows delete "$wf_id" \
      --location="$REGION" --project="$PROJECT_ID" --quiet || true
done

PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -p 15432 -U studio_user -d creative_studio -c '
TRUNCATE TABLE
  media_item_tags,
  source_asset_tags,
  tags,
  workflow_runs,
  workflows,
  brand_guidelines,
  media_items,
  source_assets,
  media_templates,
  workspace_members,
  workspaces,
  users
RESTART IDENTITY CASCADE;'

# 6. Delete objects inside GENMEDIA_BUCKET (never delete the bucket itself!)
if [ -n "$(gcloud storage ls "gs://${GENMEDIA_BUCKET}/" --project="$PROJECT_ID" 2>/dev/null | head -n 1)" ]; then
  gcloud storage rm "gs://${GENMEDIA_BUCKET}/**" --project="$PROJECT_ID" --quiet
fi

# 7. Re-seed admin user, default PUBLIC workspace, VTO assets & media templates
uv venv backend/.venv --python python3
uv pip install --python backend/.venv/bin/python -e backend

(
  cd backend && \
  GOOGLE_CLOUD_PROJECT="$PROJECT_ID" \
  PROJECT_ID="$PROJECT_ID" \
  GENMEDIA_BUCKET="$GENMEDIA_BUCKET" \
  ADMIN_USER_EMAIL="$ADMIN_USER_EMAIL" \
  DB_USER="studio_user" \
  DB_PASS="$DB_PASS" \
  DB_NAME="creative_studio" \
  DB_HOST="127.0.0.1" \
  DB_PORT="15432" \
  USE_CLOUD_SQL_AUTH_PROXY="true" \
  INSTANCE_CONNECTION_NAME="" \
  ./.venv/bin/python -m bootstrap.bootstrap
)

# 8. Stop Cloud SQL Auth Proxy
kill "$PROXY_PID"
```

---

## Post-Reset Verification Checklist

1. **Database Usability Invariants:**
   - `alembic_version` still has `1` row (`d891e2b4f5a7` on `main`).
   - `workspaces` has `1` `public` workspace (`id = 1`).
   - `users` has `1` admin user (`id = 1`, roles `user, admin`).
   - `media_templates` and `source_assets` (`scope = 'system'`) are populated.
   - `media_items`, `workflows`, `workflow_runs`, `brand_guidelines`, and `tags` have `0` rows.
2. **Browser Session Refresh (Important for Active Users):**
   - Have open browser sessions **Sign Out and Sign In** once (or run `localStorage.clear()` in DevTools).
   - Why: The Angular app caches `USER_DETAILS`, `activeWorkspaceId`, `video_state`, and `image_state` in `localStorage`. Signing out clears stale asset references from before the reset, while Just-In-Time (JIT) provisioning in `auth_guard.py` automatically re-creates standard user rows on next sign-in.
