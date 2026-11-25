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
#
# Prepares an allowlisted project so VPE jobs can actually run.
#
# This exists because of one non-obvious Vertex behaviour. VPE needs three
# service agents to hold roles/storage.admin on its I/O bucket, and two of
# them - gcp-sa-vertex-bp and gcp-sa-vertex-tune - DO NOT EXIST until a
# batch-prediction job and a tuning job have each been started once in the
# project. Granting a role to an agent that has not been provisioned fails
# with "Principal does not exist", and the documented way to provision them
# is to start and immediately cancel a throwaway job of each type.
#
# That is infrastructure setup, not a request-path concern, which is why it
# lives here rather than in the app. It is separate from bootstrap.sh
# deliberately: bootstrap.sh prepares the project Creative Studio is
# deployed into, and the allowlisted VPE project is frequently a different
# one.
#
# Everything here is idempotent - re-running it on a prepared project makes
# no changes and reports what it found.
#
#   ./vpe_bootstrap_project.sh --project VPE_PROJECT --bucket vpe-io-bucket \
#       --runtime-sa backend@app-project.iam.gserviceaccount.com
#
# The allowlisted project may also be the project the app itself runs in -
# that works, and --project simply names it.
#
# --lifecycle-days N adds an age-based delete rule for VPE's scratch data,
# which the worker does not clean up. --dry-run prints every command it
# would run and changes nothing.

set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'
BLUE=$'\033[0;34m'; BOLD=$'\033[1m'; NC=$'\033[0m'

info()    { echo "${BLUE}==>${NC} $*"; }
success() { echo "${GREEN} ok ${NC} $*"; }
warn()    { echo "${YELLOW}warn${NC} $*"; }
fail()    { echo "${RED}fail${NC} $*" >&2; exit 1; }

PROJECT=""
BUCKET=""
RUNTIME_SA=""
LOCATION="us-central1"
LIFECYCLE_DAYS=""
DRY_RUN=0

# Everything VPE reads or writes lives under this prefix, which is what
# makes an age-based rule safe to apply even to a shared bucket.
VPE_PREFIX="vpe/upscale/"

usage() {
    sed -n '16,43p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project)    PROJECT="$2"; shift 2 ;;
        --bucket)     BUCKET="${2#gs://}"; shift 2 ;;
        --runtime-sa) RUNTIME_SA="$2"; shift 2 ;;
        --location)   LOCATION="$2"; shift 2 ;;
        --lifecycle-days) LIFECYCLE_DAYS="$2"; shift 2 ;;
        --dry-run)    DRY_RUN=1; shift ;;
        -h|--help)    usage ;;
        *)            fail "unknown argument: $1" ;;
    esac
done

[[ -n "$PROJECT" ]] || usage
[[ -n "$BUCKET"  ]] || usage

if [[ -n "$LIFECYCLE_DAYS" && ! "$LIFECYCLE_DAYS" =~ ^[1-9][0-9]*$ ]]; then
    fail "--lifecycle-days takes a positive whole number of days"
fi

# Runs a mutating command, or prints it under --dry-run. It swallows the
# command's own stdout rather than leaving that to call sites: a call site
# that redirects would also silence the "would run" line, and the dry run
# would then claim to have done something it only planned.
run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "    would run: $*"
        return 0
    fi
    "$@" >/dev/null
}

command -v gcloud >/dev/null || fail "gcloud is not on PATH"

info "Project: ${BOLD}${PROJECT}${NC}   bucket: gs://${BUCKET}   region: ${LOCATION}"
[[ $DRY_RUN -eq 1 ]] && warn "dry run - nothing will be changed"

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" \
    --format='value(projectNumber)' 2>/dev/null)" \
    || fail "cannot read project ${PROJECT}. Wrong name, or no access."
success "project number ${PROJECT_NUMBER}"

# --- 1. APIs -----------------------------------------------------------------
info "Enabling required APIs"
for api in aiplatform.googleapis.com storage.googleapis.com; do
    if gcloud services list --enabled --project "$PROJECT" \
            --filter="config.name=${api}" --format='value(config.name)' \
            2>/dev/null | grep -q .; then
        success "${api} already enabled"
    else
        run gcloud services enable "$api" --project "$PROJECT"
        success "${api} enabled"
    fi
done

# --- 2. The I/O bucket -------------------------------------------------------
# VPE reads and writes Cloud Storage only, and BOTH buckets must sit inside
# the allowlisted project - a bucket in the deployment project is a blocking
# infra problem rather than a permissions one.
info "Checking the I/O bucket"
if gcloud storage buckets describe "gs://${BUCKET}" --project "$PROJECT" \
        --format='value(name)' >/dev/null 2>&1; then
    OWNER="$(gcloud storage buckets describe "gs://${BUCKET}" \
        --format='value(project_number)' 2>/dev/null || echo '')"
    if [[ -n "$OWNER" && "$OWNER" != "$PROJECT_NUMBER" ]]; then
        fail "gs://${BUCKET} lives in project ${OWNER}, not ${PROJECT_NUMBER}.
      VPE requires its input and output buckets inside the allowlisted
      project. Use a different bucket."
    fi
    success "gs://${BUCKET} exists in this project"
else
    run gcloud storage buckets create "gs://${BUCKET}" \
        --project "$PROJECT" --location "$LOCATION" \
        --uniform-bucket-level-access
    success "gs://${BUCKET} created"
fi

# --- 3. Provision the service agents -----------------------------------------
# The whole reason this script exists. gcp-sa-aiplatform is created when the
# API is enabled; the other two are not created until a job of the matching
# type has been started once, so a throwaway job is started and immediately
# cancelled purely as a side effect.
AIPLATFORM_SA="service-${PROJECT_NUMBER}@gcp-sa-aiplatform.iam.gserviceaccount.com"
BP_SA="service-${PROJECT_NUMBER}@gcp-sa-vertex-bp.iam.gserviceaccount.com"
TUNE_SA="service-${PROJECT_NUMBER}@gcp-sa-vertex-tune.iam.gserviceaccount.com"

# Returns 0 if the agent can already be named in an IAM policy.
agent_exists() {
    local member="serviceAccount:$1"
    gcloud projects get-iam-policy "$PROJECT" \
        --flatten='bindings[].members' \
        --format='value(bindings.members)' 2>/dev/null \
        | grep -qx "$member"
}

info "Provisioning the batch-prediction service agent"
if agent_exists "$BP_SA"; then
    success "gcp-sa-vertex-bp already present"
else
    warn "gcp-sa-vertex-bp does not exist yet - starting and cancelling a"
    warn "throwaway batch-prediction job to provoke Google into creating it"
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "    would run: gcloud ai batch-prediction-jobs create ... then cancel"
    else
        BP_JOB="$(gcloud ai batch-prediction-jobs create \
            --project "$PROJECT" --region "$LOCATION" \
            --display-name="vpe-bootstrap-throwaway" \
            --model="publishers/google/models/gemini-2.0-flash-001" \
            --input-paths="gs://${BUCKET}/vpe-bootstrap/nonexistent.jsonl" \
            --input-format=jsonl \
            --output-uri-prefix="gs://${BUCKET}/vpe-bootstrap/out" \
            --format='value(name)' 2>/dev/null || echo '')"
        if [[ -n "$BP_JOB" ]]; then
            gcloud ai batch-prediction-jobs cancel "$BP_JOB" \
                --project "$PROJECT" --region "$LOCATION" >/dev/null 2>&1 || true
            success "throwaway batch job started and cancelled"
        else
            # The agent is created on job *submission*, so even a job that is
            # rejected downstream usually suffices. Report rather than abort:
            # the grant below will say plainly if it did not.
            warn "could not start a batch job; the grant below will show"
            warn "whether the agent was created anyway"
        fi
    fi
fi

info "Provisioning the tuning service agent"
if agent_exists "$TUNE_SA"; then
    success "gcp-sa-vertex-tune already present"
else
    warn "gcp-sa-vertex-tune does not exist yet."
    warn "There is no non-interactive gcloud surface for starting a tuning"
    warn "job, so this one has to be provoked by hand, once:"
    echo
    echo "    Console -> Vertex AI -> Tuning -> Create tuned model"
    echo "    Start it, then cancel it immediately. The job does not need to"
    echo "    succeed; submitting it is what creates the agent."
    echo
    warn "Re-run this script afterwards to complete the grants."
fi

# --- 4. Grant storage admin on the bucket ------------------------------------
info "Granting roles/storage.admin on gs://${BUCKET}"
GRANTED_ALL=1
for sa in "$AIPLATFORM_SA" "$BP_SA" "$TUNE_SA"; do
    # All three share a service-<number>@ prefix, so the agent's own name -
    # the part that differs, and the part a reader needs - is in the domain.
    agent="${sa#*@}"; agent="${agent%%.*}"
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "    would grant storage.admin to ${agent}"
        continue
    fi
    if gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
            --member="serviceAccount:${sa}" \
            --role="roles/storage.admin" \
            --project "$PROJECT" >/dev/null 2>&1; then
        success "${agent} granted"
    else
        GRANTED_ALL=0
        warn "could not grant to ${agent}"
        warn "  -> this is the 'Principal does not exist' case: that agent"
        warn "     has not been provisioned yet. See step 3 above."
    fi
done

# --- 5. The deployed backend's own identity ----------------------------------
# Distinct from the agents above. They let Vertex touch the bucket; this lets
# the app call the allowlist-gated endpoint and stage its own inputs. This is
# the identity that has to appear on the VPE allowlist.
if [[ -n "$RUNTIME_SA" ]]; then
    info "Granting the deployed backend's identity"
    for role in roles/aiplatform.user roles/storage.objectAdmin; do
        if [[ "$role" == "roles/storage.objectAdmin" ]]; then
            run gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
                --member="serviceAccount:${RUNTIME_SA}" \
                --role="$role" --project "$PROJECT"
        else
            run gcloud projects add-iam-policy-binding "$PROJECT" \
                --member="serviceAccount:${RUNTIME_SA}" \
                --role="$role" --condition=None
        fi
        [[ $DRY_RUN -eq 1 ]] || success "${RUNTIME_SA} granted ${role}"
    done
    echo
    warn "${BOLD}IAM is necessary but not sufficient.${NC}"
    warn "The veo-experimental endpoint is allowlisted ${BOLD}per calling"
    warn "project${NC}. Confirm with the VPE programme that ${PROJECT} is on"
    warn "the allowlist - no amount of IAM substitutes for that, and the"
    warn "failure looks like an ordinary permission error."
else
    warn "no --runtime-sa given, so the deployed backend's identity was not"
    warn "granted. Re-run with it before deploying."
fi

# --- 6. Lifecycle for VPE's scratch data -------------------------------------
# The worker deletes the input segments it uploaded once a job succeeds, but
# the upscaled output VPE wrote is left behind - and that is the 4K half. It
# accumulates for as long as the deployment runs, so an age-based rule is the
# only thing that bounds it.
#
# The rule is scoped to the VPE prefix rather than the whole bucket. That is
# what makes it safe on a bucket shared with app media, and it costs nothing
# on a dedicated one.
echo
if [[ -z "$LIFECYCLE_DAYS" ]]; then
    info "No --lifecycle-days given"
    warn "VPE's upscaled segments under gs://${BUCKET}/${VPE_PREFIX} are"
    warn "never cleaned up by the app and are 4K video. Consider re-running"
    warn "with --lifecycle-days 30."
else
    info "Adding a ${LIFECYCLE_DAYS}-day delete rule for ${VPE_PREFIX}"
    # Setting a lifecycle config REPLACES whatever is there, so an existing
    # policy is left alone rather than silently discarded.
    EXISTING="$(gcloud storage buckets describe "gs://${BUCKET}" \
        --format='value(lifecycle_config.rule)' 2>/dev/null || echo '')"
    if [[ -n "$EXISTING" && "$EXISTING" != "[]" && "$EXISTING" != "None" ]]; then
        warn "this bucket already has a lifecycle policy, and applying one"
        warn "would replace it. Left untouched - add this rule by hand:"
        echo
        echo "    delete objects older than ${LIFECYCLE_DAYS} days"
        echo "    where the prefix matches ${VPE_PREFIX}"
        echo
    else
        LIFECYCLE_FILE="$(mktemp)"
        trap 'rm -f "$LIFECYCLE_FILE"' EXIT
        cat > "$LIFECYCLE_FILE" <<RULE
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {"age": ${LIFECYCLE_DAYS}, "matchesPrefix": ["${VPE_PREFIX}"]}
    }
  ]
}
RULE
        run gcloud storage buckets update "gs://${BUCKET}" \
            --lifecycle-file="$LIFECYCLE_FILE" --project "$PROJECT"
        [[ $DRY_RUN -eq 1 ]] || success "lifecycle rule applied"
    fi
fi

# --- 7. Report the settings the deployment needs -----------------------------
echo
info "${BOLD}Settings for the deployed backend${NC}"
cat <<SETTINGS

    VPE_ENABLED=true
    VPE_PROJECT_ID=${PROJECT}
    VPE_BUCKET=${BUCKET}
    VPE_LOCATION=${LOCATION}
    VPE_DRY_RUN=false

SETTINGS

if [[ $DRY_RUN -eq 1 ]]; then
    warn "${BOLD}dry run complete - nothing was changed${NC}"
elif [[ $GRANTED_ALL -eq 1 ]]; then
    success "${BOLD}project prepared${NC}"
else
    warn "finished with gaps - re-run once the missing agents exist"
fi
