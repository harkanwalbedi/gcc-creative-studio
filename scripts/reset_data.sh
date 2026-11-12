#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# ==============================================================================
# Creative Studio Data Reset & Re-Seed Script (main branch)
#
# Clears all user-generated data (media items, uploaded assets, custom
# workspaces, tags, brand guidelines, workflows, and GCS objects) while
# strictly preserving infrastructure, database schema/views, IAM bindings,
# secrets, and re-seeding the default public workspace, admin user, VTO
# system assets, and media templates so the deployment remains 100% usable.
# ==============================================================================

set -euo pipefail

# --- Styling ---
C_RESET='\033[0m'
C_RED='\033[1;31m'
C_GREEN='\033[1;32m'
C_YELLOW='\033[1;33m'
C_BLUE='\033[1;34m'
C_CYAN='\033[1;36m'

info()    { echo -e "${C_CYAN}➡️  $1${C_RESET}"; }
warn()    { echo -e "${C_YELLOW}⚠️  $1${C_RESET}"; }
success() { echo -e "${C_GREEN}✅  $1${C_RESET}"; }
fail()    { echo -e "${C_RED}❌  $1${C_RESET}" >&2; exit 1; }
step()    { echo -e "\n${C_BLUE}=== $1 ===${C_RESET}"; }

# --- Defaults ---
MODE=""
PROJECT_ID=""
REGION="us-central1"
BACKEND_SERVICE="cstudio-be"
BUCKET_OVERRIDE=""
SQL_INSTANCE_OVERRIDE=""
ADMIN_EMAIL_OVERRIDE=""
WORKFLOWS_LOCATION=""
PROXY_PORT="15432"
SKIP_GCS=0
SKIP_WORKFLOWS=0
INCLUDE_CUSTOM_BUCKETS=0
NO_BACKUP=0
DRY_RUN=0
ASSUME_YES=0

PROXY_PID=""
PROXY_BIN="/tmp/creative-studio-cloud-sql-proxy"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/backend/bootstrap/bootstrap.py" ]]; then
  REPO_ROOT="${SCRIPT_DIR}"
elif [[ -f "${SCRIPT_DIR}/../backend/bootstrap/bootstrap.py" ]]; then
  REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
elif [[ -f "${PWD}/backend/bootstrap/bootstrap.py" ]]; then
  REPO_ROOT="${PWD}"
else
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "${PWD}")"
fi

# Application data tables on `main` (excludes `alembic_version` and views).
APP_DATA_TABLES=(
  "media_item_tags"
  "source_asset_tags"
  "tags"
  "workflow_runs"
  "workflows"
  "brand_guidelines"
  "media_items"
  "source_assets"
  "media_templates"
  "workspace_members"
  "workspaces"
  "users"
)

usage() {
  cat <<'EOF'
Usage:
  ./scripts/reset_data.sh --mode <cloud|local> [options]

Required:
  --mode <cloud|local>        Target environment: 'cloud' (Cloud SQL + GCS) or
                              'local' (Docker Compose Postgres + configured GCS).

Options:
  --project <PROJECT_ID>      GCP Project ID (required for --mode cloud; read
                              from backend/.env in --mode local if omitted).
  --region <REGION>           GCP region of Cloud Run backend (default: us-central1).
  --backend-service <NAME>    Cloud Run backend service name (default: cstudio-be).
  --bucket <BUCKET_NAME>      Override GENMEDIA_BUCKET name.
  --sql-instance <CONN_NAME>  Override Cloud SQL connection name (proj:region:inst).
  --admin-email <EMAIL>       Admin email to own the re-seeded default workspace,
                              VTO assets, and templates (must not be 'system').
  --proxy-port <PORT>         Local port for Cloud SQL Auth Proxy (default: 15432).
  --skip-gcs                  Keep GCS bucket objects; only reset & re-seed DB.
  --skip-workflows            Skip deleting Creative Studio GCP Cloud Workflows.
  --include-custom-workspace-buckets
                              Also wipe objects inside custom per-workspace GCS
                              buckets recorded in workspaces.gcs_bucket_name.
  --no-backup                 Skip pre-reset database backup/snapshot.
  --dry-run                   Inspect state and print planned actions without
                              modifying any database rows, buckets, or workflows.
  --yes                       Skip interactive confirmation prompt.
  -h, --help                  Show this help message.

Examples:
  # Preview a Cloud reset without changing anything:
  ./scripts/reset_data.sh --mode cloud --project my-gcp-project --dry-run

  # Execute a full Cloud reset + re-seed:
  ./scripts/reset_data.sh --mode cloud --project my-gcp-project --admin-email admin@company.com

  # Reset Local Docker Compose DB only (preserving shared dev GCS bucket):
  ./scripts/reset_data.sh --mode local --skip-gcs --skip-workflows
EOF
}

cleanup() {
  if [[ -n "${PROXY_PID}" ]]; then
    kill "${PROXY_PID}" >/dev/null 2>&1 || true
    wait "${PROXY_PID}" 2>/dev/null || true
    PROXY_PID=""
  fi
}
trap cleanup EXIT INT TERM

# --- Parse Arguments ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="${2:-}"; shift 2 ;;
    --project) PROJECT_ID="${2:-}"; shift 2 ;;
    --region) REGION="${2:-}"; shift 2 ;;
    --backend-service) BACKEND_SERVICE="${2:-}"; shift 2 ;;
    --bucket) BUCKET_OVERRIDE="${2:-}"; shift 2 ;;
    --sql-instance) SQL_INSTANCE_OVERRIDE="${2:-}"; shift 2 ;;
    --admin-email) ADMIN_EMAIL_OVERRIDE="${2:-}"; shift 2 ;;
    --proxy-port) PROXY_PORT="${2:-}"; shift 2 ;;
    --skip-gcs) SKIP_GCS=1; shift ;;
    --skip-workflows) SKIP_WORKFLOWS=1; shift ;;
    --include-custom-workspace-buckets) INCLUDE_CUSTOM_BUCKETS=1; shift ;;
    --no-backup) NO_BACKUP=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "Unknown argument: $1 (run with --help for usage)" ;;
  esac
done

if [[ "${MODE}" != "cloud" && "${MODE}" != "local" ]]; then
  usage
  fail "You must specify --mode cloud or --mode local."
fi

# --- Guardrail: Verify Repo & Seed Assets Before Touching Anything ---
verify_repo_seed_assets() {
  step "Step 1: Verifying Repository & Bootstrap Assets"
  if [[ ! -f "${REPO_ROOT}/backend/bootstrap/bootstrap.py" ]]; then
    fail "Missing backend/bootstrap/bootstrap.py under ${REPO_ROOT}. If this is a sparse checkout, run 'git sparse-checkout disable' first."
  fi
  for asset_dir in \
    "${REPO_ROOT}/backend/bootstrap/assets/vto/garments" \
    "${REPO_ROOT}/backend/bootstrap/assets/vto/models" \
    "${REPO_ROOT}/backend/bootstrap/assets/media-template"; do
    if [[ ! -d "${asset_dir}" ]] || [[ -z "$(ls -A "${asset_dir}" 2>/dev/null)" ]]; then
      fail "Missing or empty seed asset folder: ${asset_dir}. Refusing to wipe data because re-seeding would leave the app incomplete."
    fi
  done
  success "Bootstrap script and VTO/template seed assets verified."
}

# --- Guardrail: Bucket Safety Check ---
validate_bucket_safe_to_clear() {
  local bucket="$1"
  if [[ -z "${bucket}" ]]; then
    fail "Resolved GCS bucket name is empty."
  fi
  if [[ "${bucket}" == *-tfstate* || "${bucket}" == *_cloudbuild* ]]; then
    fail "Refusing to clear protected infrastructure bucket: gs://${bucket}"
  fi
}

# --- Helper: Read Key from backend/.env ---
read_dotenv_var() {
  local key="$1"
  local env_file="${REPO_ROOT}/backend/.env"
  if [[ ! -f "${env_file}" ]]; then
    echo ""
    return
  fi
  grep -E "^[[:space:]]*${key}=" "${env_file}" | tail -n 1 | cut -d'=' -f2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" || true
}

# --- Environment Discovery ---
discover_local_config() {
  step "Step 2: Discovering Local Docker Compose Configuration"
  command -v docker >/dev/null 2>&1 || fail "docker CLI not found."

  if [[ ! -f "${REPO_ROOT}/backend/.env" ]]; then
    fail "backend/.env not found at ${REPO_ROOT}/backend/.env."
  fi

  pushd "${REPO_ROOT}" >/dev/null
  if ! docker compose ps --status running --services 2>/dev/null | grep -qx "postgres"; then
    popd >/dev/null
    fail "Docker Compose 'postgres' service is not running. Start stack first with 'docker compose up -d'."
  fi
  if ! docker compose ps --status running --services 2>/dev/null | grep -qx "backend"; then
    popd >/dev/null
    fail "Docker Compose 'backend' service is not running. Start stack first with 'docker compose up -d'."
  fi
  popd >/dev/null

  PROJECT_ID="${PROJECT_ID:-$(read_dotenv_var PROJECT_ID)}"
  if [[ -z "${PROJECT_ID}" ]]; then
    PROJECT_ID="$(read_dotenv_var GOOGLE_CLOUD_PROJECT)"
  fi
  GENMEDIA_BUCKET="${BUCKET_OVERRIDE:-$(read_dotenv_var GENMEDIA_BUCKET)}"
  DB_USER="$(read_dotenv_var DB_USER)"
  DB_USER="${DB_USER:-studio_user}"
  DB_PASS="$(read_dotenv_var DB_PASS)"
  DB_PASS="${DB_PASS:-studio_pass}"
  DB_NAME="$(read_dotenv_var DB_NAME)"
  DB_NAME="${DB_NAME:-creative_studio}"
  WORKFLOWS_LOCATION="$(read_dotenv_var WORKFLOWS_LOCATION)"
  WORKFLOWS_LOCATION="${WORKFLOWS_LOCATION:-us-central1}"

  ADMIN_USER_EMAIL="${ADMIN_EMAIL_OVERRIDE:-$(read_dotenv_var ADMIN_USER_EMAIL)}"
  if [[ -z "${ADMIN_USER_EMAIL}" || "${ADMIN_USER_EMAIL}" == "system" ]]; then
    if command -v gcloud >/dev/null 2>&1; then
      ADMIN_USER_EMAIL="$(gcloud config get-value account 2>/dev/null || true)"
    fi
  fi
  if [[ -z "${ADMIN_USER_EMAIL}" || "${ADMIN_USER_EMAIL}" == "system" || "${ADMIN_USER_EMAIL}" != *"@"* ]]; then
    fail "ADMIN_USER_EMAIL must be a valid email (not 'system') so bootstrap.py can create the default public workspace and seed templates. Pass --admin-email user@domain.com."
  fi

  if [[ "${SKIP_GCS}" -eq 0 ]]; then
    validate_bucket_safe_to_clear "${GENMEDIA_BUCKET}"
  fi

  info "Mode:              local (Docker Compose)"
  info "GCP Project ID:    ${PROJECT_ID:-<unset>}"
  info "Media Bucket:      ${GENMEDIA_BUCKET:-<skipped>} $( [[ ${SKIP_GCS} -eq 1 ]] && echo '(SKIPPED via --skip-gcs)' || true )"
  info "Database:          ${DB_NAME} (Docker service: postgres, user: ${DB_USER})"
  info "Seed Admin Email:  ${ADMIN_USER_EMAIL}"
}

start_cloud_sql_proxy() {
  local conn_name="$1"
  info "Preparing Cloud SQL Auth Proxy for ${conn_name} on 127.0.0.1:${PROXY_PORT}..."

  if (echo >/dev/tcp/127.0.0.1/"${PROXY_PORT}") >/dev/null 2>&1; then
    fail "Local TCP port ${PROXY_PORT} is already in use. Pass a free port via --proxy-port <PORT>."
  fi

  if [[ ! -x "${PROXY_BIN}" ]]; then
    local os arch
    os="$(uname -s | tr '[:upper:]' '[:lower:]')"
    arch="$(uname -m)"
    case "${arch}" in
      x86_64) arch="amd64" ;;
      aarch64|arm64) arch="arm64" ;;
      *) fail "Unsupported CPU architecture for cloud-sql-proxy: ${arch}" ;;
    esac
    local proxy_url="https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.14.1/cloud-sql-proxy.${os}.${arch}"
    info "Downloading cloud-sql-proxy (${os}.${arch}) to ${PROXY_BIN}..."
    curl -fsSL -o "${PROXY_BIN}" "${proxy_url}"
    chmod +x "${PROXY_BIN}"
  fi

  "${PROXY_BIN}" --address 127.0.0.1 --port "${PROXY_PORT}" "${conn_name}" >/dev/null 2>&1 &
  PROXY_PID=$!

  for _ in {1..30}; do
    if (echo >/dev/tcp/127.0.0.1/"${PROXY_PORT}") >/dev/null 2>&1; then
      success "Cloud SQL Auth Proxy connected on 127.0.0.1:${PROXY_PORT}."
      return 0
    fi
    if ! kill -0 "${PROXY_PID}" 2>/dev/null; then
      fail "Cloud SQL Auth Proxy exited unexpectedly. Check IAM permissions (roles/cloudsql.client) and ADC credentials."
    fi
    sleep 1
  done
  fail "Timed out waiting for Cloud SQL Auth Proxy on 127.0.0.1:${PROXY_PORT}."
}

discover_cloud_config() {
  step "Step 2: Discovering Cloud Deployment Configuration"
  command -v gcloud >/dev/null 2>&1 || fail "gcloud CLI not found."
  command -v jq >/dev/null 2>&1 || fail "jq CLI not found."
  command -v uv >/dev/null 2>&1 || fail "uv CLI not found (install from https://astral.sh/uv)."

  if [[ -z "${PROJECT_ID}" ]]; then
    fail "--project <PROJECT_ID> is required when running with --mode cloud."
  fi

  info "Inspecting Cloud Run service '${BACKEND_SERVICE}' in ${REGION} (project: ${PROJECT_ID})..."
  local svc_json=""
  svc_json="$(gcloud run services describe "${BACKEND_SERVICE}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}" \
    --format=json 2>/dev/null || echo "{}")"

  local discovered_bucket discovered_conn discovered_wf_loc
  discovered_bucket="$(echo "${svc_json}" | jq -r '(.spec.template.spec.containers[0].env // []) | map(select(.name == "GENMEDIA_BUCKET"))[0].value // ""')"
  discovered_conn="$(echo "${svc_json}" | jq -r '
    ((.spec.template.spec.containers[0].env // []) | map(select(.name == "INSTANCE_CONNECTION_NAME"))[0].value) //
    (.spec.template.metadata.annotations["run.googleapis.com/cloudsql-instances"]) // ""
  ' | cut -d',' -f1)"
  discovered_wf_loc="$(echo "${svc_json}" | jq -r '(.spec.template.spec.containers[0].env // []) | map(select(.name == "WORKFLOWS_LOCATION"))[0].value // ""')"

  GENMEDIA_BUCKET="${BUCKET_OVERRIDE:-${discovered_bucket}}"
  if [[ -z "${GENMEDIA_BUCKET}" ]]; then
    fail "Could not auto-discover GENMEDIA_BUCKET from Cloud Run service '${BACKEND_SERVICE}'. Pass --bucket <BUCKET_NAME>."
  fi
  if [[ "${SKIP_GCS}" -eq 0 ]]; then
    validate_bucket_safe_to_clear "${GENMEDIA_BUCKET}"
    gcloud storage buckets describe "gs://${GENMEDIA_BUCKET}" --project="${PROJECT_ID}" >/dev/null \
      || fail "Cannot access bucket gs://${GENMEDIA_BUCKET} in project ${PROJECT_ID}."
  fi

  INSTANCE_CONNECTION_NAME="${SQL_INSTANCE_OVERRIDE:-${discovered_conn}}"
  if [[ -z "${INSTANCE_CONNECTION_NAME}" ]]; then
    INSTANCE_CONNECTION_NAME="$(gcloud sql instances list \
      --project="${PROJECT_ID}" \
      --filter="name:creative-studio-db*" \
      --format="value(connectionName)" | head -n 1)"
  fi
  if [[ -z "${INSTANCE_CONNECTION_NAME}" ]]; then
    fail "Could not discover Cloud SQL connection name. Pass --sql-instance <PROJECT:REGION:INSTANCE>."
  fi
  SQL_INSTANCE_SHORT="${INSTANCE_CONNECTION_NAME##*:}"

  WORKFLOWS_LOCATION="${discovered_wf_loc:-us-central1}"

  ADMIN_USER_EMAIL="${ADMIN_EMAIL_OVERRIDE:-}"
  if [[ -z "${ADMIN_USER_EMAIL}" || "${ADMIN_USER_EMAIL}" == "system" ]]; then
    ADMIN_USER_EMAIL="$(gcloud config get-value account 2>/dev/null || true)"
  fi
  if [[ -z "${ADMIN_USER_EMAIL}" || "${ADMIN_USER_EMAIL}" == "system" || "${ADMIN_USER_EMAIL}" != *"@"* ]]; then
    fail "Could not determine a valid admin user email (got '${ADMIN_USER_EMAIL}'). Pass --admin-email user@domain.com."
  fi

  info "Fetching database password from Secret Manager ('creative-studio-db-password')..."
  DB_PASS="$(gcloud secrets versions access latest \
    --secret="creative-studio-db-password" \
    --project="${PROJECT_ID}")"
  [[ -n "${DB_PASS}" ]] || fail "Secret 'creative-studio-db-password' returned an empty value."
  DB_USER="studio_user"
  DB_NAME="creative_studio"
  DB_HOST="127.0.0.1"
  DB_PORT="${PROXY_PORT}"

  info "Mode:              cloud (GCP Project: ${PROJECT_ID})"
  info "Cloud Run Service: ${BACKEND_SERVICE} (${REGION})"
  info "Cloud SQL:         ${INSTANCE_CONNECTION_NAME} (db: ${DB_NAME}, user: ${DB_USER})"
  info "Media Bucket:      gs://${GENMEDIA_BUCKET} $( [[ ${SKIP_GCS} -eq 1 ]] && echo '(SKIPPED via --skip-gcs)' || true )"
  info "Workflows Region:  ${WORKFLOWS_LOCATION}"
  info "Seed Admin Email:  ${ADMIN_USER_EMAIL}"

  # Ensure local Python venv for running DB inspection & bootstrap.py
  VENV_DIR="${REPO_ROOT}/backend/.venv"
  if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    info "Creating Python virtual environment at ${VENV_DIR}..."
    uv venv "${VENV_DIR}" --python python3
  fi
  info "Syncing backend Python dependencies into ${VENV_DIR}..."
  uv pip install --python "${VENV_DIR}/bin/python" -q -e "${REPO_ROOT}/backend"

  start_cloud_sql_proxy "${INSTANCE_CONNECTION_NAME}"
}

# --- Helper: Run Python Against Target Database ---
run_backend_python() {
  local py_code="$1"
  if [[ "${MODE}" == "local" ]]; then
    pushd "${REPO_ROOT}" >/dev/null
    docker compose exec -T \
      -e PROJECT_ID="${PROJECT_ID}" \
      -e GOOGLE_CLOUD_PROJECT="${PROJECT_ID}" \
      -e GENMEDIA_BUCKET="${GENMEDIA_BUCKET}" \
      -e ADMIN_USER_EMAIL="${ADMIN_USER_EMAIL}" \
      -e PYTHONPATH="/app" \
      backend /app/.venv/bin/python -c "${py_code}"
    popd >/dev/null
  else
    (
      cd "${REPO_ROOT}/backend"
      PROJECT_ID="${PROJECT_ID}" \
      GOOGLE_CLOUD_PROJECT="${PROJECT_ID}" \
      GENMEDIA_BUCKET="${GENMEDIA_BUCKET}" \
      ADMIN_USER_EMAIL="${ADMIN_USER_EMAIL}" \
      DB_USER="${DB_USER}" \
      DB_PASS="${DB_PASS}" \
      DB_NAME="${DB_NAME}" \
      DB_HOST="${DB_HOST}" \
      DB_PORT="${DB_PORT}" \
      USE_CLOUD_SQL_AUTH_PROXY="true" \
      INSTANCE_CONNECTION_NAME="" \
      PYTHONPATH="${REPO_ROOT}/backend" \
      "${REPO_ROOT}/backend/.venv/bin/python" -c "${py_code}"
    )
  fi
}

# --- Step 3: Inspect Current State Before Reset ---
INSPECT_JSON=""
inspect_current_state() {
  step "Step 3: Inspecting Current Database State"
  INSPECT_JSON="$(run_backend_python '
import asyncio, json
from sqlalchemy import text
from src.database import async_session_local, cleanup_connector

TABLES = [
    "media_item_tags", "source_asset_tags", "tags",
    "workflow_runs", "workflows", "brand_guidelines",
    "media_items", "source_assets", "media_templates",
    "workspace_members", "workspaces", "users",
]

async def inspect():
    try:
        async with async_session_local() as db:
            res = await db.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = \x27public\x27 AND table_type = \x27BASE TABLE\x27"
            ))
            existing = {r[0] for r in res.fetchall()}

            alembic_rev = None
            if "alembic_version" in existing:
                r = await db.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
                row = r.fetchone()
                alembic_rev = row[0] if row else None

            counts = {}
            for t in TABLES:
                if t in existing:
                    c = (await db.execute(text(f"SELECT COUNT(*) FROM \"{t}\""))).scalar_one()
                    counts[t] = c

            workflows = []
            if "workflows" in existing:
                r = await db.execute(text("SELECT id FROM workflows"))
                workflows = [row[0] for row in r.fetchall() if row[0]]

            custom_buckets = []
            if "workspaces" in existing:
                cols_res = await db.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = \x27public\x27 AND table_name = \x27workspaces\x27"
                ))
                cols = {r[0] for r in cols_res.fetchall()}
                if "gcs_bucket_name" in cols:
                    r = await db.execute(text(
                        "SELECT DISTINCT gcs_bucket_name FROM workspaces "
                        "WHERE gcs_bucket_name IS NOT NULL AND btrim(gcs_bucket_name) <> \x27\x27"
                    ))
                    custom_buckets = [row[0] for row in r.fetchall()]

            print("JSON_RESULT:" + json.dumps({
                "alembic_revision": alembic_rev,
                "counts": counts,
                "workflow_ids": workflows,
                "custom_buckets": custom_buckets,
            }))
    finally:
        await cleanup_connector()

asyncio.run(inspect())
' | grep '^JSON_RESULT:' | sed 's/^JSON_RESULT://')"

  [[ -n "${INSPECT_JSON}" ]] || fail "Failed to inspect database tables."

  echo "${INSPECT_JSON}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
rev = data.get("alembic_revision") or "<none>"
print(f"  Alembic revision (preserved): {rev}")
print("  Current row counts to clear:")
for table, count in data["counts"].items():
    print(f"    - {table:<20} {count}")
wf = data.get("workflow_ids") or []
print(f"  Creative Studio GCP Workflows found: {len(wf)}")
cb = data.get("custom_buckets") or []
if cb:
    print(f"  Custom workspace buckets referenced: {cb}")
'
}

# --- Step 4: Dry-Run Exit or Interactive Confirmation ---
confirm_execution() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    step "Dry Run Summary (No Changes Made)"
    info "Would truncate 12 application tables (preserving alembic_version and unified_gallery_view)."
    if [[ "${SKIP_GCS}" -eq 0 ]]; then
      info "Would remove all objects inside gs://${GENMEDIA_BUCKET}/** (preserving bucket & IAM)."
    else
      info "Would skip GCS object removal (--skip-gcs)."
    fi
    if [[ "${SKIP_WORKFLOWS}" -eq 0 ]]; then
      info "Would delete Creative Studio GCP Cloud Workflows recorded in the workflows table."
    fi
    info "Would re-run backend/bootstrap/bootstrap.py as '${ADMIN_USER_EMAIL}' to recreate:"
    echo "    1. Admin user (${ADMIN_USER_EMAIL})"
    echo "    2. Default PUBLIC workspace"
    echo "    3. VTO system garments & models (system_assets/vto/*)"
    echo "    4. Default gallery templates (media_template_assets/*)"
    success "Dry run complete. Nothing was modified."
    exit 0
  fi

  if [[ "${ASSUME_YES}" -eq 1 ]]; then
    return 0
  fi

  local expected_token
  if [[ "${MODE}" == "cloud" ]]; then
    expected_token="${PROJECT_ID}"
  else
    expected_token="local"
  fi

  echo
  warn "DESTRUCTIVE DATA RESET: All user/generated DB records $( [[ ${SKIP_GCS} -eq 0 ]] && echo "and objects in gs://${GENMEDIA_BUCKET}" || true ) will be permanently deleted, then default templates/system assets will be re-seeded."
  read -r -p "   Type '${expected_token}' to confirm and continue: " reply </dev/tty
  if [[ "${reply}" != "${expected_token}" ]]; then
    fail "Confirmation did not match '${expected_token}'. Aborted with zero changes."
  fi
}

# --- Step 5: Pre-Reset Database Backup ---
create_backup() {
  if [[ "${NO_BACKUP}" -eq 1 ]]; then
    warn "Skipping pre-reset database backup (--no-backup)."
    return 0
  fi

  step "Step 4: Creating Pre-Reset Database Backup"
  local ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  if [[ "${MODE}" == "cloud" ]]; then
    info "Creating on-demand Cloud SQL backup on instance '${SQL_INSTANCE_SHORT}'..."
    gcloud sql backups create \
      --instance="${SQL_INSTANCE_SHORT}" \
      --project="${PROJECT_ID}" \
      --description="pre-data-reset-${ts}"
    success "Cloud SQL backup created (pre-data-reset-${ts})."
  else
    local dump_path="/tmp/creative-studio-local-backup-${ts}.sql"
    info "Dumping local Postgres database to ${dump_path}..."
    pushd "${REPO_ROOT}" >/dev/null
    docker compose exec -T postgres pg_dump -U "${DB_USER}" -d "${DB_NAME}" > "${dump_path}"
    popd >/dev/null
    success "Local database dump saved to ${dump_path}."
  fi
}

# --- Step 6: Delete Creative Studio GCP Cloud Workflows ---
clear_gcp_workflows() {
  if [[ "${SKIP_WORKFLOWS}" -eq 1 ]]; then
    info "Skipping GCP Cloud Workflows cleanup (--skip-workflows)."
    return 0
  fi

  local wf_ids
  wf_ids="$(echo "${INSPECT_JSON}" | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin).get("workflow_ids", [])))')"
  if [[ -z "${wf_ids}" ]]; then
    info "No Creative Studio workflows recorded in database; skipping GCP Cloud Workflows cleanup."
    return 0
  fi

  if ! command -v gcloud >/dev/null 2>&1 || [[ -z "${PROJECT_ID}" ]]; then
    warn "gcloud or PROJECT_ID unavailable; skipping deletion of GCP Cloud Workflows."
    return 0
  fi

  step "Step 5: Deleting Creative Studio GCP Cloud Workflows"
  while IFS= read -r wf_id; do
    [[ -z "${wf_id}" ]] && continue
    info "Deleting GCP Cloud Workflow '${wf_id}' in ${WORKFLOWS_LOCATION}..."
    gcloud workflows delete "${wf_id}" \
      --location="${WORKFLOWS_LOCATION}" \
      --project="${PROJECT_ID}" \
      --quiet >/dev/null 2>&1 || warn "Workflow '${wf_id}' was already absent in GCP."
  done <<< "${wf_ids}"
  success "Creative Studio GCP Cloud Workflows cleaned up."
}

# --- Step 7: Truncate Application Data Tables (Schema & Views Preserved) ---
truncate_database_tables() {
  step "Step 6: Truncating Application Data Tables (Preserving Schema & Views)"
  run_backend_python '
import asyncio
from sqlalchemy import text
from src.database import async_session_local, cleanup_connector

APP_TABLES = [
    "media_item_tags", "source_asset_tags", "tags",
    "workflow_runs", "workflows", "brand_guidelines",
    "media_items", "source_assets", "media_templates",
    "workspace_members", "workspaces", "users",
]

async def truncate_all():
    try:
        async with async_session_local() as db:
            res = await db.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = \x27public\x27 AND table_type = \x27BASE TABLE\x27"
            ))
            existing = {r[0] for r in res.fetchall()}
            targets = [f"\"{t}\"" for t in APP_TABLES if t in existing]
            if targets:
                stmt = f"TRUNCATE TABLE {', '.join(targets)} RESTART IDENTITY CASCADE"
                await db.execute(text(stmt))
                await db.commit()
                print(f"Truncated {len(targets)} tables and reset sequences.")
    finally:
        await cleanup_connector()

asyncio.run(truncate_all())
'
  success "All application data tables truncated; alembic_version and unified_gallery_view preserved."
}

# --- Step 8: Clear GCS Media Bucket Objects (Preserve Bucket & IAM) ---
wipe_bucket_objects() {
  local bucket="$1"
  validate_bucket_safe_to_clear "${bucket}"

  info "Checking objects inside gs://${bucket}/ ..."
  local has_objects
  has_objects="$(gcloud storage ls "gs://${bucket}/" --project="${PROJECT_ID}" 2>/dev/null | head -n 1 || true)"
  if [[ -z "${has_objects}" ]]; then
    info "Bucket gs://${bucket} is already empty."
    return 0
  fi

  info "Deleting all objects inside gs://${bucket}/** (bucket resource & IAM bindings stay intact)..."
  gcloud storage rm "gs://${bucket}/**" --project="${PROJECT_ID}" --quiet
  success "Cleared all objects inside gs://${bucket}."
}

clear_gcs_storage() {
  if [[ "${SKIP_GCS}" -eq 1 ]]; then
    info "Skipping GCS bucket cleanup (--skip-gcs)."
    return 0
  fi

  step "Step 7: Clearing GCS Media Bucket Objects"
  command -v gcloud >/dev/null 2>&1 || fail "gcloud CLI is required to clear GCS buckets (or pass --skip-gcs)."

  wipe_bucket_objects "${GENMEDIA_BUCKET}"

  local custom_buckets
  custom_buckets="$(echo "${INSPECT_JSON}" | python3 -c 'import json,sys; print("\n".join(json.load(sys.stdin).get("custom_buckets", [])))')"
  if [[ -n "${custom_buckets}" ]]; then
    if [[ "${INCLUDE_CUSTOM_BUCKETS}" -eq 1 ]]; then
      while IFS= read -r cb; do
        [[ -z "${cb}" || "${cb}" == "${GENMEDIA_BUCKET}" ]] && continue
        wipe_bucket_objects "${cb}"
      done <<< "${custom_buckets}"
    else
      warn "Custom workspace bucket(s) were referenced in workspaces.gcs_bucket_name and left untouched for safety:"
      while IFS= read -r cb; do
        [[ -n "${cb}" ]] && echo "    - gs://${cb}"
      done <<< "${custom_buckets}"
      warn "Re-run with --include-custom-workspace-buckets if you also want those buckets emptied."
    fi
  fi
}

# --- Step 9: Re-Seed Baseline Data so App Remains Usable ---
reseed_baseline_data() {
  step "Step 8: Re-Seeding Admin User, Default Public Workspace, VTO Assets & Templates"
  if [[ "${MODE}" == "local" ]]; then
    pushd "${REPO_ROOT}" >/dev/null
    docker compose exec -T \
      -e PROJECT_ID="${PROJECT_ID}" \
      -e GOOGLE_CLOUD_PROJECT="${PROJECT_ID}" \
      -e GENMEDIA_BUCKET="${GENMEDIA_BUCKET}" \
      -e ADMIN_USER_EMAIL="${ADMIN_USER_EMAIL}" \
      -e PYTHONPATH="/app" \
      backend /app/.venv/bin/python -m bootstrap.bootstrap
    popd >/dev/null
  else
    (
      cd "${REPO_ROOT}/backend"
      PROJECT_ID="${PROJECT_ID}" \
      GOOGLE_CLOUD_PROJECT="${PROJECT_ID}" \
      GENMEDIA_BUCKET="${GENMEDIA_BUCKET}" \
      ADMIN_USER_EMAIL="${ADMIN_USER_EMAIL}" \
      DB_USER="${DB_USER}" \
      DB_PASS="${DB_PASS}" \
      DB_NAME="${DB_NAME}" \
      DB_HOST="${DB_HOST}" \
      DB_PORT="${DB_PORT}" \
      USE_CLOUD_SQL_AUTH_PROXY="true" \
      INSTANCE_CONNECTION_NAME="" \
      PYTHONPATH="${REPO_ROOT}/backend" \
      "${REPO_ROOT}/backend/.venv/bin/python" -m bootstrap.bootstrap
    )
  fi
  success "Baseline bootstrap seeding completed."
}

# --- Step 10: Verify Post-Reset Usable State ---
verify_usable_state() {
  step "Step 9: Verifying Post-Reset Application Usability Invariants"
  run_backend_python '
import asyncio, sys
from sqlalchemy import text
from src.database import async_session_local, cleanup_connector

async def verify():
    errors = []
    try:
        async with async_session_local() as db:
            rev = (await db.execute(text("SELECT COUNT(*) FROM alembic_version"))).scalar_one()
            if rev != 1:
                errors.append(f"alembic_version expected 1 row, found {rev}")

            # Ensure unified_gallery_view was not broken
            await db.execute(text("SELECT COUNT(*) FROM unified_gallery_view"))

            admin_cnt = (await db.execute(text(
                "SELECT COUNT(*) FROM users WHERE \x27admin\x27 = ANY(roles) OR \x27ADMIN\x27 = ANY(roles)"
            ))).scalar_one()
            if admin_cnt < 1:
                errors.append("No ADMIN user exists in users table")

            pub_ws = (await db.execute(text(
                "SELECT COUNT(*) FROM workspaces WHERE LOWER(scope::text) = \x27public\x27"
            ))).scalar_one()
            if pub_ws < 1:
                errors.append("No PUBLIC default workspace exists in workspaces table")

            tpl_cnt = (await db.execute(text("SELECT COUNT(*) FROM media_templates"))).scalar_one()
            if tpl_cnt < 1:
                errors.append("media_templates is empty (templates failed to re-seed)")

            sys_assets = (await db.execute(text(
                "SELECT COUNT(*) FROM source_assets WHERE LOWER(scope::text) = \x27system\x27"
            ))).scalar_one()
            if sys_assets < 1:
                errors.append("No SYSTEM source_assets found (VTO/template assets failed to re-seed)")

            for empty_tbl in ("media_items", "workflows", "workflow_runs", "brand_guidelines", "tags"):
                cnt = (await db.execute(text(f"SELECT COUNT(*) FROM \"{empty_tbl}\""))).scalar_one()
                if cnt != 0:
                    errors.append(f"Expected {empty_tbl} to be 0 after reset, found {cnt}")

            print(
                f"  Verified usable state: admin_users={admin_cnt}, public_workspaces={pub_ws}, "
                f"templates={tpl_cnt}, system_assets={sys_assets}, user_media_items=0"
            )
    finally:
        await cleanup_connector()

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

asyncio.run(verify())
'
  success "All usability invariants passed — Creative Studio is clean and ready to use!"
  echo
  info "Browser Tip: Ask currently signed-in users to Sign Out & Sign In once (or clear site localStorage)"
  info "so cached 'USER_DETAILS', 'activeWorkspaceId', 'video_state', and 'image_state' refresh cleanly."
}

main() {
  verify_repo_seed_assets
  if [[ "${MODE}" == "local" ]]; then
    discover_local_config
  else
    discover_cloud_config
  fi
  inspect_current_state
  confirm_execution
  create_backup
  clear_gcp_workflows
  truncate_database_tables
  clear_gcs_storage
  reseed_baseline_data
  verify_usable_state
}

main
