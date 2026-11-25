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
# test_bootstrap_offline.sh
# Automated offline unit test suite for Creative Studio bootstrap.sh.
# Simulates Google Cloud CLI, Firebase CLI, and Terraform commands using mocks
# to verify multi-profile workflows, self-healing parameter generation,
# pre-plan auto-imports, Cloud Run Job flags, and non-interactive execution.
#

set -eo pipefail

# --- Color Formatting ---
C_GREEN='\033[0;32m'
C_RED='\033[0;31m'
C_YELLOW='\033[0;33m'
C_BLUE='\033[0;34m'
C_RESET='\033[0m'

info()    { echo -e "${C_BLUE}[TEST-INFO]${C_RESET} $*"; }
success() { echo -e "${C_GREEN}[TEST-PASS]${C_RESET} $*"; }
fail()    { echo -e "${C_RED}[TEST-FAIL]${C_RESET} $*" >&2; exit 1; }

# Locate repository root and target script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BOOTSTRAP_SCRIPT="${REPO_ROOT}/bootstrap.sh"

if [ ! -f "${BOOTSTRAP_SCRIPT}" ]; then
    fail "Could not find bootstrap.sh at ${BOOTSTRAP_SCRIPT}."
fi

# Static sanity checks: verify Izumi agent and legacy secrets are absent from bootstrap.sh
if grep -qE 'deploy_izumi_agent|agent_engine_resource_name|agent_engine_user_auth_token_key|workflow_token_encryption_key|generate_fernet_key' "${BOOTSTRAP_SCRIPT}"; then
    fail "bootstrap.sh still contains Izumi agent or removed secret references!"
fi

# --- Initialize Temporary Test Workspace ---
BASE_TMP="${TMPDIR:-/tmp}"
BASE_TMP="${BASE_TMP%/}"
TEST_WORKSPACE=$(mktemp -d "${BASE_TMP}/cstudio_bootstrap_test_XXXXXX")
TEST_WORKSPACE="$(cd "${TEST_WORKSPACE}" && pwd)"
info "Initialized temporary test workspace at: ${TEST_WORKSPACE}"

# Ensure cleanup on exit
cleanup() {
    info "Cleaning up temporary test workspace: ${TEST_WORKSPACE}"
    rm -rf "${TEST_WORKSPACE}"
}
trap cleanup EXIT INT TERM

# Isolate HOME inside TEST_WORKSPACE so user profiles are never touched
export HOME="${TEST_WORKSPACE}/mock_home"
mkdir -p "${HOME}"

export MOCK_CALL_LOG="${TEST_WORKSPACE}/mock_calls.log"
touch "${MOCK_CALL_LOG}"
export MOCK_TF_STATE_POPULATED="false"

# Create required folder structure inside test workspace
mkdir -p "${TEST_WORKSPACE}/gcc-creative-studio/infrastructure/modules/artifact"
mkdir -p "${TEST_WORKSPACE}/gcc-creative-studio/infrastructure/modules/compute"
mkdir -p "${TEST_WORKSPACE}/gcc-creative-studio/infrastructure/modules/database"
mkdir -p "${TEST_WORKSPACE}/gcc-creative-studio/infrastructure/modules/network"
mkdir -p "${TEST_WORKSPACE}/gcc-creative-studio/backend"
mkdir -p "${TEST_WORKSPACE}/gcc-creative-studio/frontend"
cp "${BOOTSTRAP_SCRIPT}" "${TEST_WORKSPACE}/gcc-creative-studio/bootstrap.sh"
chmod +x "${TEST_WORKSPACE}/gcc-creative-studio/bootstrap.sh"

# --- Create Mock Executables ---
MOCK_BIN="${TEST_WORKSPACE}/mock_bin"
mkdir -p "${MOCK_BIN}"

cat << 'EOF' > "${MOCK_BIN}/gcloud"
#!/usr/bin/env bash
echo "gcloud $*" >> "${MOCK_CALL_LOG}"
if [[ "$*" == *"auth print-access-token"* ]]; then
    echo "mock-access-token-9999"
    exit 0
elif [[ "$*" == *"config get-value project"* ]]; then
    echo "creative-studio-mock-project"
    exit 0
elif [[ "$*" == *"config get-value account"* ]]; then
    echo "admin@example.com"
    exit 0
elif [[ "$*" == *"config set project"* ]]; then
    exit 0
elif [[ "$*" == *"services enable"* ]]; then
    echo "Operation finished successfully."
    exit 0
elif [[ "$*" == *"storage buckets list"* || "$*" == *"storage ls"* || "$*" == *"sql instances list"* ]]; then
    exit 0
elif [[ "$*" == *"storage buckets describe"* ]]; then
    echo "gs://mock-bucket"
    exit 0
elif [[ "$*" == *"secrets describe"* || "$*" == *"secrets versions"* || "$*" == *"secrets create"* ]]; then
    echo "Mock secret operation successful."
    exit 0
elif [[ "$*" == *"builds triggers run"* ]]; then
    echo "mock-build-id-1234"
    exit 0
elif [[ "$*" == *"builds describe"* ]]; then
    echo "SUCCESS"
    exit 0
elif [[ "$*" == *"run services describe"* ]]; then
    if [[ "$*" == *"value(image)"* ]]; then
        echo "us-central1-docker.pkg.dev/creative-studio-mock-project/cs-dev-repo/backend:mocksha"
    elif [[ "$*" == *"value(template.serviceAccount)"* ]]; then
        echo "cs-dev-run@creative-studio-mock-project.iam.gserviceaccount.com"
    else
        echo "https://cs-dev-backend-xyz.a.run.app"
    fi
    exit 0
elif [[ "$*" == *"run jobs execute"* ]]; then
    echo "temp-db-bootstrap-job-exec-001"
    exit 0
elif [[ "$*" == *"run jobs executions describe"* ]]; then
    if [[ "$*" == *"completionTime"* ]]; then
        echo "2026-01-01T00:00:05Z"
    elif [[ "$*" == *"succeededCount"* ]]; then
        echo "1"
    fi
    exit 0
elif [[ "$*" == *"run jobs"* || "$*" == *"run deploy"* ]]; then
    echo "Mock cloud run execution successful."
    exit 0
elif [[ "$*" == *"iap oauth-clients list"* ]]; then
    echo "[]"
    exit 0
fi
echo "gcloud mock executed: $*" >&2
exit 0
EOF
chmod +x "${MOCK_BIN}/gcloud"

cat << 'EOF' > "${MOCK_BIN}/firebase"
#!/usr/bin/env bash
echo "firebase $*" >> "${MOCK_CALL_LOG}"
if [[ "$*" == *"apps:list"* ]]; then
    echo '{"result":[{"displayName":"cs-dev-test-frontend","appId":"1:999:web:mock123"}]}'
    exit 0
elif [[ "$*" == *"apps:sdkconfig"* ]]; then
    echo '{"result":{"sdkConfig":{"apiKey":"mock-api-key-xyz","authDomain":"mock.firebaseapp.com","projectId":"creative-studio-mock-project","storageBucket":"mock.appspot.com","messagingSenderId":"123456","appId":"1:999:web:mock123","measurementId":"G-MOCK"}}}'
    exit 0
elif [[ "$*" == *"hosting:sites:list"* ]]; then
    echo '{"result":{"sites":[{"name":"projects/creative-studio-mock-project/sites/creative-studio-mock-f9d5d","type":"DEFAULT_SITE"}]}}'
    exit 0
elif [[ "$*" == *"apps:create"* ]]; then
    echo "Firebase app created successfully."
    exit 0
fi
echo "firebase mock executed: $*" >&2
exit 0
EOF
chmod +x "${MOCK_BIN}/firebase"

cat << 'EOF' > "${MOCK_BIN}/terraform"
#!/usr/bin/env bash
echo "terraform $*" >> "${MOCK_CALL_LOG}"
if [[ "$1" == "version" ]]; then
    if [[ "$*" == *"-json"* ]]; then
        echo '{"terraform_version":"1.14.1"}'
    else
        echo "Terraform v1.14.1"
    fi
    exit 0
elif [[ "$1" == "init" ]]; then
    echo "Terraform initialized successfully."
    exit 0
elif [[ "$1" == "state" && "$2" == "list" ]]; then
    if [[ "${MOCK_TF_STATE_POPULATED}" == "true" ]]; then
        echo "google_firebase_hosting_site.frontend"
        echo "google_cloudbuildv2_repository.source_repo"
    fi
    exit 0
elif [[ "$1" == "import" ]]; then
    echo "Import successful!"
    exit 0
elif [[ "$1" == "plan" ]]; then
    echo "Terraform plan encountered zero changes."
    exit 0
elif [[ "$1" == "apply" ]]; then
    echo "Terraform applied successfully."
    exit 0
elif [[ "$1" == "output" ]]; then
    if [[ "$*" == *"-json"* ]]; then
        echo '{"frontend_secrets":{"value":["FIREBASE_API_KEY","GOOGLE_CLIENT_ID","GOOGLE_TOKEN_AUDIENCE"]},"backend_secrets":{"value":["GOOGLE_CLIENT_ID","GOOGLE_TOKEN_AUDIENCE"]}}'
    elif [[ "$*" == *"frontend_service_url"* ]]; then
        echo "https://creative-studio-mock-f9d5d.web.app"
    elif [[ "$*" == *"backend_service_url"* ]]; then
        echo "https://cstudio-be-xyz-uc.a.run.app"
    elif [[ "$*" == *"cloud_sql_connection_name"* ]]; then
        echo "creative-studio-mock-project:us-central1:cs-dev-db-abcd"
    elif [[ "$*" == *"db_name"* ]]; then
        echo "creative_studio"
    elif [[ "$*" == *"db_user"* ]]; then
        echo "app_user"
    elif [[ "$*" == *"db_secret_id"* ]]; then
        echo "cs-dev-db-password"
    elif [[ "$*" == *"cloud_run_subnet_name"* ]]; then
        echo "cs-dev-run-subnet"
    else
        echo "mock-output-value"
    fi
    exit 0
fi
echo "terraform mock executed: $*" >&2
exit 0
EOF
chmod +x "${MOCK_BIN}/terraform"

cat << 'EOF' > "${MOCK_BIN}/gsutil"
#!/usr/bin/env bash
echo "gsutil $*" >> "${MOCK_CALL_LOG}"
echo "gsutil mock executed successfully."
exit 0
EOF
chmod +x "${MOCK_BIN}/gsutil"

cat << 'EOF' > "${MOCK_BIN}/jq"
#!/usr/bin/env bash
if [[ "$*" == *".oauthClientId"* ]]; then
    echo "999999999999-mockedclientid.apps.googleusercontent.com"
    exit 0
elif [[ "$*" == *"DEFAULT_SITE"* ]]; then
    echo "projects/creative-studio-mock-project/sites/creative-studio-mock-f9d5d"
    exit 0
elif [[ "$*" == *".sdkConfig.apiKey"* ]]; then
    echo "mock-api-key-xyz"
    exit 0
elif [[ "$*" == *".sdkConfig."* ]]; then
    echo "mock-firebase-sdk-val"
    exit 0
elif [[ "$*" == *".terraform_version"* ]]; then
    echo "1.14.1"
    exit 0
elif [[ "$*" == *".frontend_secrets"* ]]; then
    printf "FIREBASE_API_KEY\nGOOGLE_CLIENT_ID\nGOOGLE_TOKEN_AUDIENCE\n"
    exit 0
elif [[ "$*" == *".backend_secrets"* ]]; then
    printf "GOOGLE_CLIENT_ID\nGOOGLE_TOKEN_AUDIENCE\n"
    exit 0
elif [[ "$*" == *"select(.name | contains"* ]]; then
    echo ""
    exit 0
fi
echo "mock-jq-result"
exit 0
EOF
chmod +x "${MOCK_BIN}/jq"

cat << 'EOF' > "${MOCK_BIN}/git"
#!/usr/bin/env bash
echo "git $*" >> "${MOCK_CALL_LOG}"
if [[ "$*" == *"ls-remote"* ]]; then
    exit 0
elif [[ "$*" == *"clone"* ]]; then
    eval "last_arg=\${$#}"
    mkdir -p "$last_arg/infrastructure/modules"
    mkdir -p "$last_arg/backend"
    mkdir -p "$last_arg/frontend"
    cp "${BOOTSTRAP_SCRIPT_FOR_MOCK}" "$last_arg/bootstrap.sh"
    chmod +x "$last_arg/bootstrap.sh"
    exit 0
elif [[ "$*" == *"branch --show-current"* || "$*" == *"rev-parse"* || "$*" == *"show-ref"* ]]; then
    echo "feature/infra-changes"
    exit 0
elif [[ "$*" == *"remote get-url"* ]]; then
    echo "https://github.com/GoogleCloudPlatform/gcc-creative-studio.git"
    exit 0
fi
exit 0
EOF
chmod +x "${MOCK_BIN}/git"
export BOOTSTRAP_SCRIPT_FOR_MOCK="${BOOTSTRAP_SCRIPT}"

cat << 'EOF' > "${MOCK_BIN}/curl"
#!/usr/bin/env bash
echo '{"oauthClientId":"999999999999-mockedclientid.apps.googleusercontent.com"}'
exit 0
EOF
chmod +x "${MOCK_BIN}/curl"

cat << 'EOF' > "${MOCK_BIN}/uv"
#!/usr/bin/env bash
echo "uv mock executed successfully."
exit 0
EOF
chmod +x "${MOCK_BIN}/uv"

# Inject MOCK_BIN at head of PATH
export PATH="${MOCK_BIN}:${PATH}"
info "Mock cloud CLI environment activated on PATH."

# --- Configure Test Profile in Isolated Home Directory ---
MOCK_PROFILE_DIR="${HOME}/.cstudio/profiles"
mkdir -p "${MOCK_PROFILE_DIR}"
TEST_PROFILE_PATH="${MOCK_PROFILE_DIR}/test_automated.cstudio_bootstrap.conf"

cat <<EOF > "${TEST_PROFILE_PATH}"
GCP_PROJECT_ID="creative-studio-mock-project"
GITHUB_REPO_URL="https://github.com/GoogleCloudPlatform/gcc-creative-studio.git"
GITHUB_BRANCH="feature/infra-changes"
ENV_NAME="dev-test"
GITHUB_CONN_NAME="gh-mock-conn-test"
AUTO_OAUTH_CLIENT_ID="999999999999-mockedclientid.apps.googleusercontent.com"
REPO_ROOT="${TEST_WORKSPACE}/gcc-creative-studio"
EOF
info "Initialized simulated deployment profile at: ${TEST_PROFILE_PATH}"

# --- TEST CASE 1: Verify Non-Interactive In-Repo Execution & Parameter Seeding ---
info "Executing TEST CASE 1: Running bootstrap.sh inside checkout against test profile via CLI flags..."
cd "${TEST_WORKSPACE}/gcc-creative-studio"
: > "${MOCK_CALL_LOG}"

if ! bash ./bootstrap.sh --profile "test_automated.cstudio_bootstrap.conf" --auto-approve > "${TEST_WORKSPACE}/test_output.log" 2>&1; then
    cat "${TEST_WORKSPACE}/test_output.log" >&2
    fail "bootstrap.sh failed during automated execution!"
fi

if [ -d "${TEST_WORKSPACE}/gcc-creative-studio/gcc-creative-studio" ]; then
    fail "bootstrap.sh erroneously created a nested gcc-creative-studio clone inside an existing checkout!"
fi
success "TEST CASE 1 PASSED: Script completed uninterrupted in-place without nested cloning!"

# --- TEST CASE 2: Verify .tfvars, custom_audiences, Pre-Plan Auto-Import & Cloud Run Job Flags ---
info "Executing TEST CASE 2: Verifying .tfvars generation, custom_audiences, pre-plan imports, and job flags..."

TFVARS_FILE="${TEST_WORKSPACE}/gcc-creative-studio/infrastructure/dev-test.tfvars"
if [ ! -f "${TFVARS_FILE}" ]; then
    cat "${TEST_WORKSPACE}/test_output.log" >&2
    fail "Expected file ${TFVARS_FILE} was not generated!"
fi

grep -q 'project_id[[:space:]]*=[[:space:]]*"creative-studio-mock-project"' "${TFVARS_FILE}" || { cat "${TFVARS_FILE}" >&2; fail "project_id missing from .tfvars!"; }
grep -q 'region[[:space:]]*=[[:space:]]*"us-central1"' "${TFVARS_FILE}" || { cat "${TFVARS_FILE}" >&2; fail "region missing from .tfvars!"; }
grep -q 'resource_prefix[[:space:]]*=[[:space:]]*"cs"' "${TFVARS_FILE}" || { cat "${TFVARS_FILE}" >&2; fail "resource_prefix missing from .tfvars!"; }
grep -q 'firebase_site_id[[:space:]]*=[[:space:]]*"creative-studio-mock-f9d5d"' "${TFVARS_FILE}" || { cat "${TFVARS_FILE}" >&2; fail "firebase_site_id (mocked domain) missing from .tfvars!"; }
grep -q 'custom_audiences[[:space:]]*=[[:space:]]*\["999999999999-mockedclientid.apps.googleusercontent.com", "creative-studio-mock-project"\]' "${TFVARS_FILE}" || { cat "${TFVARS_FILE}" >&2; fail "custom_audiences missing or malformed in .tfvars!"; }

if ! grep -q 'AUTO_FIREBASE_SITE_ID=.*creative-studio-mock-f9d5d' "${TEST_PROFILE_PATH}"; then
    cat "${TEST_PROFILE_PATH}" >&2
    fail "AUTO_FIREBASE_SITE_ID was not saved into persistent profile!"
fi

# Assert pre-plan auto-imports ran when resources were absent from terraform state list
grep -q "terraform import .* google_firebase_hosting_site.frontend projects/creative-studio-mock-project/sites/creative-studio-mock-f9d5d" "${MOCK_CALL_LOG}" || { cat "${MOCK_CALL_LOG}" >&2; fail "Pre-plan auto-import for google_firebase_hosting_site.frontend did not run!"; }
grep -q "terraform import .* google_cloudbuildv2_repository.source_repo projects/creative-studio-mock-project/locations/us-central1/connections/gh-mock-conn-test/repositories/gcc-creative-studio" "${MOCK_CALL_LOG}" || { cat "${MOCK_CALL_LOG}" >&2; fail "Pre-plan auto-import for google_cloudbuildv2_repository.source_repo did not run!"; }

# Assert Cloud Run seeding job creation flags
grep -q "gcloud run jobs create temp-db-bootstrap-job .*--vpc-egress=private-ranges-only .*--memory=2Gi .*--cpu=2 .*--max-retries=0 .*--task-timeout=900s" "${MOCK_CALL_LOG}" || { cat "${MOCK_CALL_LOG}" >&2; fail "temp-db-bootstrap-job missing required memory/cpu/vpc-egress/timeout flags!"; }

# Assert no Izumi or removed secret calls occurred
if grep -qE "izumi|agent_engine|workflow_token_encryption_key" "${MOCK_CALL_LOG}"; then
    cat "${MOCK_CALL_LOG}" >&2
    fail "Unexpected Izumi or legacy secret command detected in mock call log!"
fi

success "TEST CASE 2 PASSED: .tfvars, custom_audiences, pre-plan imports, and Cloud Run Job flags verified!"

# --- TEST CASE 3: Verify Idempotent Re-Run Skips Auto-Import When State Already Populated ---
info "Executing TEST CASE 3: Verifying idempotent re-run when Terraform state is already populated..."
export MOCK_TF_STATE_POPULATED="true"
: > "${MOCK_CALL_LOG}"

if ! bash ./bootstrap.sh --profile "test_automated.cstudio_bootstrap.conf" --auto-approve > "${TEST_WORKSPACE}/test_resumption.log" 2>&1; then
    cat "${TEST_WORKSPACE}/test_resumption.log" >&2
    fail "bootstrap.sh failed during idempotent re-run!"
fi

if grep -q "^terraform import " "${MOCK_CALL_LOG}"; then
    cat "${MOCK_CALL_LOG}" >&2
    fail "terraform import should be skipped when resources are already in terraform state list!"
fi
export MOCK_TF_STATE_POPULATED="false"
success "TEST CASE 3 PASSED: Idempotent re-run skipped duplicate imports and completed cleanly!"

# --- TEST CASE 4: Verify Stdin ('curl | bash') Invocation from External Directory ---
info "Executing TEST CASE 4: Simulating stdin ('curl | bash') invocation from external directory..."
CURL_TEST_DIR="${TEST_WORKSPACE}/curl_simulation_workspace"
mkdir -p "${CURL_TEST_DIR}"
cd "${CURL_TEST_DIR}"

CURL_PROFILE_PATH="${MOCK_PROFILE_DIR}/test_curl_simulation.cstudio_bootstrap.conf"
cat <<EOF > "${CURL_PROFILE_PATH}"
GCP_PROJECT_ID="creative-studio-mock-project"
GITHUB_REPO_URL="https://github.com/GoogleCloudPlatform/gcc-creative-studio.git"
GITHUB_BRANCH="feature/infra-changes"
ENV_NAME="dev-curl-test"
GITHUB_CONN_NAME="gh-mock-conn-test"
AUTO_OAUTH_CLIENT_ID="999999999999-mockedclientid.apps.googleusercontent.com"
EOF

if ! cat "${BOOTSTRAP_SCRIPT}" | bash -s -- --profile "test_curl_simulation.cstudio_bootstrap.conf" --auto-approve > "${CURL_TEST_DIR}/test_curl.log" 2>&1; then
    cat "${CURL_TEST_DIR}/test_curl.log" >&2
    fail "bootstrap.sh failed during simulated stdin ('curl | bash') invocation!"
fi

grep -q "REPO_ROOT=.*${CURL_TEST_DIR}/gcc-creative-studio" "${CURL_PROFILE_PATH}" || { cat "${CURL_TEST_DIR}/test_curl.log" >&2; fail "REPO_ROOT was not correctly resolved during stdin invocation!"; }
success "TEST CASE 4 PASSED: Stdin ('curl | bash') style invocation correctly resolved path and completed cleanly!"

# --- TEST CASE 5: Verify Parent Directory Invocation & Arbitrary Repo Folder Name ---
info "Executing TEST CASE 5: Simulating parent directory CLI invocation with arbitrary repo folder name..."
PARENT_TEST_DIR="${TEST_WORKSPACE}/parent_simulation_workspace"
mkdir -p "${PARENT_TEST_DIR}/custom-creative-studio/infrastructure/modules"
mkdir -p "${PARENT_TEST_DIR}/custom-creative-studio/backend"
mkdir -p "${PARENT_TEST_DIR}/custom-creative-studio/frontend"
cp "${BOOTSTRAP_SCRIPT}" "${PARENT_TEST_DIR}/custom-creative-studio/bootstrap.sh"
chmod +x "${PARENT_TEST_DIR}/custom-creative-studio/bootstrap.sh"
cd "${PARENT_TEST_DIR}"

PARENT_PROFILE_PATH="${MOCK_PROFILE_DIR}/test_parent_simulation.cstudio_bootstrap.conf"
cat <<EOF > "${PARENT_PROFILE_PATH}"
GCP_PROJECT_ID="creative-studio-mock-project"
REPO_URL="https://github.com/example-org/custom-creative-studio.git"
REPO_BRANCH="feature/infra-changes"
ENV_NAME="dev-parent-test"
REPO_CONN_NAME="gh-mock-conn-test"
AUTO_OAUTH_CLIENT_ID="999999999999-mockedclientid.apps.googleusercontent.com"
EOF

if ! bash custom-creative-studio/bootstrap.sh --profile "test_parent_simulation.cstudio_bootstrap.conf" --auto-approve > "${PARENT_TEST_DIR}/test_parent.log" 2>&1; then
    cat "${PARENT_TEST_DIR}/test_parent.log" >&2
    fail "bootstrap.sh failed during parent directory invocation ('bash custom-creative-studio/bootstrap.sh')!"
fi

grep -q "REPO_ROOT=.*${PARENT_TEST_DIR}/custom-creative-studio" "${PARENT_PROFILE_PATH}" || { cat "${PARENT_TEST_DIR}/test_parent.log" >&2; fail "REPO_ROOT was not correctly resolved during parent directory invocation!"; }
grep -q "REPO_NAME=custom-creative-studio" "${PARENT_PROFILE_PATH}" || { cat "${PARENT_PROFILE_PATH}" >&2; fail "REPO_NAME was not correctly saved for custom repo!"; }
success "TEST CASE 5 PASSED: Parent directory invocation with arbitrary repo name resolved path and completed cleanly!"

echo -e "\n${C_GREEN}============================================================${C_RESET}"
echo -e "${C_GREEN} 🎉 ALL OFFLINE BOOTSTRAP TESTS PASSED SUCCESSFULLY! 🎉 ${C_RESET}"
echo -e "${C_GREEN}============================================================${C_RESET}"
exit 0
