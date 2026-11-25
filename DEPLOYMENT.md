# 🚀 Creative Studio — Deployment Guide (Terraform + `bootstrap.sh`)

This guide explains, step by step, how Creative Studio is deployed into a Google Cloud project with the Terraform code in [`infrastructure/`](./infrastructure) orchestrated by [`bootstrap.sh`](./bootstrap.sh), what each step asks you for, how to re-run / resume / upgrade a deployment, and — most importantly — the **errors we have actually hit in real deployments and how to get out of them** (see [§6 Troubleshooting](#6-troubleshooting-known-errors-and-fixes)).

Related documents:

- [`README.md`](./README.md) — product overview and quick start.
- [`DEVELOPMENT.md`](./DEVELOPMENT.md) — running Creative Studio locally with Docker Compose.
- [`UPGRADING.md`](./UPGRADING.md) — upgrading an existing deployment, Gemini Omni & VPE configuration.
- [`infrastructure/README.md`](./infrastructure/README.md) — the **in-depth infrastructure diagram** (every GCP resource and how traffic, data and CI/CD flow between them), VPE / `TF_VAR_*` overrides, and the database **migration flow** (legacy public‑IP → private‑IP) in detail.

---

## Table of contents

1. [How a deployment works](#1-how-a-deployment-works)
2. [Prerequisites](#2-prerequisites)
3. [Running the deployment, step by step](#3-running-the-deployment-step-by-step)
4. [Flags, profiles and non-interactive runs](#4-flags-profiles-and-non-interactive-runs)
5. [Redeploying, upgrading and migrating](#5-redeploying-upgrading-and-migrating)
6. [Troubleshooting (known errors and fixes)](#6-troubleshooting-known-errors-and-fixes)
7. [Running Terraform by hand & optional variables (VPE)](#7-running-terraform-by-hand--optional-variables-vpe)
8. [Tearing a deployment down](#8-tearing-a-deployment-down)
9. [Reference: files the script generates](#9-reference-files-the-script-generates)

---

## 1. How a deployment works

`bootstrap.sh` is a **resumable, interactive Bash script** (`set -e`: it stops at the first error and can simply be re-run). It does not replace Terraform — it wraps it:

1. Collects configuration interactively and stores it in a **deployment profile** (`~/.cstudio/profiles/<name>.cstudio_bootstrap.conf`) so later runs are mostly non-interactive.
2. Walks you through the few things that cannot be automated (Cloud Build host connection, accepting Firebase terms, the OAuth Web Client ID).
3. Generates `infrastructure/<env>.tfvars` and `infrastructure/<env>.backend.tfvars` and runs `terraform init` / `plan` / `apply` against a GCS state bucket (auto-importing pre-existing Firebase Hosting site and Cloud Build repository resources if not yet in state).
4. Populates Secret Manager, triggers Cloud Build (backend → Cloud Run, frontend → Firebase Hosting).
5. Runs database migrations + seeding through a temporary Cloud Run Job inside the VPC.

```mermaid
flowchart LR
    A["bootstrap.sh"] --> B["Profile + prompts"]
    B --> C["Manual steps: Cloud Build connection, Firebase terms, OAuth client"]
    C --> D["terraform apply (infrastructure/)"]
    D --> E["Secret Manager values"]
    E --> F["Cloud Build triggers"]
    F --> G["Cloud Run backend (cs-env-backend)"]
    F --> H["Firebase Hosting frontend"]
    G --> I["Cloud Run Job: Alembic migrations + seeding"]
```

### 1.1 What gets created

`<env>` is the environment name you choose (default `dev-infra`); `<project>` is the GCP project ID.

| Component | Name(s) | Created by |
| --- | --- | --- |
| Terraform state bucket | `<project>-cstudio-<env>-tfstate`, prefix `infra/<env>/state` | `bootstrap.sh` (Step 5) |
| VPC, subnet, Private Service Access range, firewall | `cs-<env>-vpc`, `cs-<env>-run-subnet` (`10.0.0.0/26`), `cs-<env>-psa-range`, `cs-<env>-allow-internal` | Terraform `modules/network` |
| Cloud SQL — PostgreSQL 18, **private IP only**, automated backups + PITR | instance `cs-<env>-db-<hex>`, database `creative_studio`, user `app_user`, password in secret `cs-<env>-db-password` | Terraform `modules/database` |
| Media/asset bucket | `<project>-cs-<env>-bucket` (an existing legacy `<project>-cs-*-bucket` is reused automatically) | Terraform `storage.tf` |
| Artifact Registry (Docker) | `cs-<env>-repo` | Terraform `modules/artifact` |
| Backend Cloud Run service | `cs-<env>-backend` — 2 vCPU / 8 GiB, min 1 instance, 3600s request timeout, Direct VPC egress | Terraform `modules/compute` (the real image is deployed by Cloud Build; Terraform ignores image changes) |
| Service accounts | `cs-<env>-run` (backend runtime), `cs-<env>-bkt-reader` (signed URLs), `cs-trig-sa` (Cloud Build triggers) | Terraform |
| Secret Manager "shells" | `FIREBASE_*` (7), `GOOGLE_CLIENT_ID`, `GOOGLE_TOKEN_AUDIENCE` — created with a placeholder version | Terraform creates them; `bootstrap.sh` writes the values |
| Firebase Hosting site + Web app | site = the project's default Hosting site (usually `<project>`), web app `cs-<env>-frontend` | Terraform (site) / `bootstrap.sh` (web app) |
| Cloud Build (2nd gen) | repository link to **your fork** + triggers `cs-<env>-backend-trigger` and `cs-<env>-frontend-trigger` (push to your branch, path-filtered to `backend/**` / `frontend/**`) | Terraform `ci_cd.tf` — **the host connection itself is created manually by you** |
| Temporary Cloud Run Job | `temp-db-bootstrap-job` (2 vCPU / 2 GiB, private-ranges-only VPC egress; deleted automatically after a successful run) | `bootstrap.sh` (Step 14) |

Terraform also enables the required APIs (`infrastructure/variables.tf` → `apis_to_enable`) and never disables them on destroy.

---

## 2. Prerequisites

### 2.1 Accounts and permissions

- A Google Cloud project with **billing enabled** (the script can also create one for you if you provide a Billing Account ID).
- Your Google account needs **Owner** on the project (or an equivalent set of admin roles: Service Usage, IAM, Cloud SQL, Cloud Run, Cloud Build, Secret Manager, Storage, Firebase, Artifact Registry, Vertex AI).
- A **fork** of this repository on GitHub (or GitLab): <https://github.com/GoogleCloudPlatform/gcc-creative-studio/fork>. Cloud Build needs to install its app on that fork, so you must be allowed to install GitHub Apps on it.
- Google sign-in for the application is Firebase Authentication **with Identity Platform** and the **Google provider**. You enable the provider and click **Upgrade** (to Identity Platform) in the Firebase console during Step 6 (see below); the upgrade also creates the OAuth Web Client the script asks for, so there is nothing to create by hand in *APIs & Services → Credentials*.

### 2.2 Tools

The easiest environment is **Google Cloud Shell** — everything is pre-installed and pre-authenticated. On a workstation you need:

| Tool | Notes |
| --- | --- |
| `gcloud` | `gcloud auth login` **and** `gcloud auth application-default login` (Terraform uses Application Default Credentials). Cloud Shell has both already. |
| `git` | — |
| `jq` | The script offers to install it (`apt-get` / `brew` / `yum`). |
| `firebase-tools` | `npm install -g firebase-tools` (the script offers to do it) and `firebase login` on a workstation. |
| `uv` | Installed automatically if missing. |
| `terraform` | **1.14.1** is installed into `~/bin` automatically if missing or older (`REQUIRED_TERRAFORM_VERSION` in the script). The code requires ≥ 1.13.0 and the `google` / `google-beta` providers ≥ 6.50.0. |

### 2.3 Decisions to make before you start

| Decision | Default | Notes |
| --- | --- | --- |
| GCP project ID | active `gcloud` project | — |
| **Region** | `us-central1` | Pick a region allowed by your organisation policies (`constraints/gcp.resourceLocations`). Everything — state bucket, Cloud SQL, Cloud Run, secrets replicas — is created in this region. |
| Environment name | `dev-infra` | Appears in every resource name (`cs-<env>-…`). One profile ⇔ one environment. |
| Branch to deploy | `main` | The Cloud Build triggers fire on pushes to **exactly** this branch of your fork. |
| Cloud SQL tier | `db-custom-2-7680` (Enterprise) | Option 2 `db-perf-optimized-N-2` switches the instance to Enterprise Plus; option 3 is any custom tier. |
| Cloud SQL availability | `ZONAL` | `REGIONAL` = multi-zone HA, roughly 2× the cost. |
| Cloud Build host connection name | — | You create it in the console (e.g. `gh-yourname-con`). **Create it in the deploy region and do not link the repository in the wizard** (see Step 6 and [§6.1](#61-terraform-fails-the-cloud-build-repository--connection-already-exists-409)). |

---

## 3. Running the deployment, step by step

### 3.1 Start the script

You can start `bootstrap.sh` either **directly inside an existing git clone** or from an **external parent directory** (including `curl | bash`):

- **From inside an existing clone** (`./bootstrap.sh`): the script detects the local git checkout and offers to use it directly (defaulting the repository URL from `origin` and branch from `git branch --show-current`) without cloning a nested subdirectory.
- **From a parent directory or `curl | bash`**: the script sparse-clones your fork into `./<repo-name>` (`infrastructure`, `backend`, `frontend`, `bootstrap.sh`) and, on later runs, `git pull`s it.

```bash
# Option A: inside an existing clone of your fork
./bootstrap.sh

# Option B: first run from an empty/parent directory (Cloud Shell or workstation)
curl https://raw.githubusercontent.com/GoogleCloudPlatform/gcc-creative-studio/refs/heads/main/bootstrap.sh | bash

# Later runs (resume after a failure, upgrade, infra changes)
./bootstrap.sh --profile <profile-name>
```

When piping from `curl`, pass flags with `bash -s --`, e.g. `… | bash -s -- --profile default --auto-approve`.

Plan for roughly **15–30 minutes** of wall-clock time: the first `terraform apply` (Cloud SQL + VPC peering) takes 10–15 minutes, and the two Cloud Build pipelines plus seeding take about 5–10 minutes.

### 3.2 What each step does and what you answer

> [!NOTE]
> The banner numbers the script prints (`--- Step N: … ---`) are labels, not a strict sequence: "Step 2", "Step 8" and "Step 10" each appear twice. The table below is in **execution order**.

| # | Banner | What happens | What you must do / know |
| --- | --- | --- | --- |
| 1 | Step 1 — Checking Prerequisites | Verifies `gcloud`, `git`; offers to install `jq`, `firebase-tools`, `uv`. | Answer `y` to the install prompts if needed. |
| 2 | Step 2 — Selecting Deployment Configuration Profile | Lists `~/.cstudio/profiles/*.cstudio_bootstrap.conf`. With no profiles it creates `default`. With a loaded profile it shows the 11 stored parameters. | `1..n` to load, `N` to create, `D` to delete. On a loaded profile: `Y` accept, `e` edit one property, `n` reject. `--profile <name>` skips the menu. |
| 3 | Step 2 — Checking Terraform Installation | Installs Terraform 1.14.1 into `~/bin` if missing/older. | If it says "open a new terminal", do so and re-run. |
| 4 | Step 3 — Configuring Google Cloud Project | Uses the stored project, else offers the active `gcloud` project, else asks for an existing/new project ID (+ billing account for a new one). | — |
| 5 | Step 4 — Configuring Git Repository | Uses your current git clone if invoked inside one (defaulting URL & branch from git), or asks for **your fork URL** (validated with `git ls-remote`) and **branch** (default `main`) and sparse-clones/pulls it. | Use the HTTPS URL of your fork, e.g. `https://github.com/<you>/gcc-creative-studio.git`. |
| 6 | Step 5 — Configuring Terraform Environment | Scans the project for an existing `<project>-cstudio-*-tfstate` bucket (auto-fills env name + state bucket so an existing deployment is updated, not duplicated) and for a legacy `<project>-cs-*-bucket` (reused as asset bucket). Asks environment name, region, DB tier, DB availability. Creates the state bucket and writes `<env>.backend.tfvars` + `<env>.tfvars` (including `custom_audiences`). | Enter the decisions from §2.3. The region must match `^[a-z]+-[a-z]+[0-9]+$` (e.g. `europe-west1`). |
| 7 | Step 6 — Manual Steps Required | Enables APIs. Then three manual items: **(a)** Cloud Build host connection, **(b)** Firebase terms + Authentication (Google provider, **Upgrade** to Identity Platform), **(c)** OAuth Web Client ID. | See the detailed instructions right below this table. |
| 8 | Step 7 — Automating Firebase Web App Configuration | Creates the Firebase web app `cs-<env>-frontend` if missing, reads its SDK config, resolves the Hosting site ID (the project's default site, else the first site, else the project ID) and writes it into the tfvars. | Fails with *"Could not automatically fetch Firebase API Key"* if Firebase is not linked to the project yet → finish Step 6 (b) and re-run. |
| 9 | Step 8 — Database Backup & Export | **No-op on a fresh project.** If a legacy V1 instance (`creative-studio-db[-<hex>]`) exists it offers to export it to `gs://<state-bucket>/migration_backup.sql.gz`. `--migrate-db` forces an export of whatever DB exists; `--use-existing-backup` skips the export and reuses the file. | Answer `y` only if you are upgrading a legacy V1 deployment. Details in [§5.4](#54-migrating-a-legacy-v1-public-ip-database). |
| 10 | Step 9 — Checking Migration Intent & Preparing Safe State | If a migration is planned (or an orphaned `migration_backup.sql.gz` is found and you accept), deploys the dummy image `us-docker.pkg.dev/cloudrun/container/hello` to the existing backend service(s) so Terraform's network changes cannot deadlock Cloud Run. | On the happy path it prints "No migration intent detected" and moves on. |
| 11 | Step 10 — Deploying Infrastructure with Terraform | Runs `terraform init`, idempotently auto-imports `google_firebase_hosting_site.frontend` and `google_cloudbuildv2_repository.source_repo` if they already exist in GCP but are not yet in state, then runs `terraform plan -detailed-exitcode -out=tfplan`. Exit 0 → "zero modifications", apply skipped. Exit 2 → asks for confirmation and runs `terraform apply -parallelism=30 tfplan`. | Review the plan and answer `y` (or pass `--auto-approve`). The first apply takes 10–15 min. |
| 12 | Step 10 — Importing Legacy Database (if applicable) | Only when a migration is in flight: grants the new instance's SA read access to the backup, **drops and recreates** `creative_studio` on the new instance, imports the dump, restarts the backend with its current image. | Nothing on the happy path. |
| 13 | Step 8 — Automating OAuth Secret Population | Writes the OAuth Web Client ID into `GOOGLE_CLIENT_ID` and `GOOGLE_TOKEN_AUDIENCE`, and updates `custom_audiences` in `<env>.tfvars`. | — |
| 14 | Step 11 — Configuring OAuth Client URIs | Best-effort: tries to add the `*.web.app` / `*.firebaseapp.com` origins and `/__/auth/handler` redirect URIs to the OAuth client via `gcloud iap oauth-clients`. Only IAP-managed clients can be updated that way, so it frequently prints *"Could not resolve the full name for the OAuth client. Skipping URI update."*. | Harmless. If sign-in later fails, see [§6.8](#68-google-sign-in-fails-authunauthorized-domain-redirect_uri_mismatch-origin-not-allowed). |
| 15 | Step 12 — Updating Remaining Secrets | Writes the seven `FIREBASE_*` secrets from the SDK config into Secret Manager. | Any unexpected custom secret in `application_secrets` is asked for interactively (input is hidden). |
| 16 | Step 13 — Triggering Initial Builds | Runs `cs-<env>-backend-trigger` and `cs-<env>-frontend-trigger` on the **current branch of the clone**. | Answer `y`. `--force-builds` skips the question, `--skip-builds` skips the builds. Monitor at `https://console.cloud.google.com/cloud-build/builds?project=<project>`. |
| 17 | Step 14 — Executing Database Migrations & Initial Seeding | Waits for the backend build (≤ 30 min) or for a non-dummy image on the service (≤ 15 min). Creates `temp-db-bootstrap-job` (2 vCPU / 2 GiB, `--vpc-egress=private-ranges-only`) from the live backend image inside the VPC subnet and runs `python -m bootstrap.bootstrap`: Alembic migrations, admin user, default public workspace, VTO assets and media templates. Deletes the job on success. | **The `gcloud` account you run the script with becomes the first ADMIN user** (`ADMIN_USER_EMAIL`). A failed job is **not fatal**: the job is left in place for inspection ([§6.6](#66-database-migrations--seeding-job-failed)). |
| 18 | Step 15 — Deployment Complete | Prints the frontend (`https://<site>.web.app`) and backend URLs. | Builds may still be finishing; wait a few minutes. |

#### Step 6 in detail — the manual items

**(a) Cloud Build host connection.** The script asks *"Do you already have a Cloud Build Host Connection for GitHub in this project? (y/n)"*.

- If you do, type its name. If not, it prints the wizard URL `https://console.cloud.google.com/cloud-build/connections/create?project=<project>`.
- In the wizard: pick **GitHub** (or GitLab), choose the **same region as your deploy region**, name it (e.g. `gh-yourname-con`), authorize the Cloud Build GitHub App and grant it access to your fork.
- **Stop when the wizard offers to "Link repositories".** Terraform manages the repository link itself (`google_cloudbuildv2_repository.source_repo` in [`infrastructure/ci_cd.tf`](./infrastructure/ci_cd.tf)); `bootstrap.sh` will auto-import an existing link if found, but leaving it unlinked is cleanest — see [§6.1](#61-terraform-fails-the-cloud-build-repository--connection-already-exists-409).
- Paste the connection name back into the script; it is stored in the profile (`REPO_CONN_NAME`) and written to `repo_conn_name` in the tfvars. Terraform looks for `projects/<project>/locations/<region>/connections/<name>` — a connection created in another region is "not found" ([§6.2](#62-terraform-fails-cloud-build-connection-not-found-404)).

**(b) Firebase terms and Authentication.** Open `https://console.firebase.google.com/?project=<project>`, click **Add Firebase** to the existing project and accept the terms. Then set up the sign-in the app uses, in this order:

1. **Build → Authentication → Get started → Sign-in method → Google → Enable** (provide the support email it asks for) → **Save**.
2. Scroll to the bottom of the Authentication page and click **Upgrade** (*"Upgrade to Firebase Authentication with Identity Platform"*), then confirm. This switches the project to **Identity Platform** (the `identitytoolkit.googleapis.com` API the script already enabled) and — the reason it matters here — **automatically creates the OAuth 2.0 Web client** in *APIs & Services → Credentials*, named *"Web client (auto created by Google Service)"*. You do not have to create or configure an OAuth client yourself.

Press Enter in the script when done; it remembers this (`FIREBASE_TERMS_ACCEPTED`).

**(c) OAuth Web Client ID.** Open `https://console.cloud.google.com/apis/credentials?project=<project>`: after the upgrade in (b) the **OAuth 2.0 Client ID of type "Web application"** is already there — copy its Client ID into the script. Only if the list is empty (the upgrade was skipped) create a Web application client manually. The script stores it as `AUTO_OAUTH_CLIENT_ID`, wires it into `custom_audiences`, and later writes it to the `GOOGLE_CLIENT_ID` / `GOOGLE_TOKEN_AUDIENCE` secrets. The authorized origins of this client are configured **after** the deployment, in [§3.3](#33-open-the-app-to-your-users-before-verifying).

### 3.3 Open the app to your users (before verifying)

Step 15 prints the frontend URL, `https://<site>.web.app` (`<site>` is the Hosting site ID, usually the project ID). Three settings decide who can actually use it; do them right after the script finishes, otherwise sign-in fails with `redirect_uri_mismatch` / *"The given origin is not allowed"*, or the app loads and every API call returns **403**.

**1. Authorized JavaScript origins of the OAuth Web client.** Open `https://console.cloud.google.com/apis/credentials?project=<project>` → the *Web client (auto created by Google Service)* from Step 6 (c) and add:

| Field | Values |
| --- | --- |
| Authorized JavaScript origins | `https://<site>.web.app`, `https://<project>.firebaseapp.com` (and `https://<custom-domain>` if you set `custom_domain`) |
| Authorized redirect URIs | `https://<project>.firebaseapp.com/__/auth/handler` |

Step 11 tries to add these automatically, but it can only edit IAP-managed clients, so in practice this is manual. Changes take a few minutes to propagate.

**2. Audience — who is allowed to sign in.** Open `https://console.cloud.google.com/auth/audience?project=<project>` (*Google Auth Platform → Audience*). Set **User type = Internal** to restrict sign-in to the accounts of your own Google Workspace / Cloud Identity organisation (the normal choice for an internal deployment). **External** lets any Google account sign in and requires test users while the app is unverified. Two further, application-side gates exist as backend environment variables if you need them: `IDENTITY_PLATFORM_ALLOWED_ORGS` (comma-separated hosted domains, checked against the token's `hd` claim) and `ALLOWED_EMAILS`; they are read by [`backend/src/auth/auth_guard.py`](./backend/src/auth/auth_guard.py) and can be passed via `TF_VAR_extra_env_vars` (see [§7](#7-running-terraform-by-hand--optional-variables-vpe)).

**3. Cloud Run Invoker for the people who will use the app.** The backend service is **private** by default (Cloud Build deploys without `--allow-unauthenticated`). Browser requests reach it through the Hosting `/api/**` rewrite carrying the signed-in user's Google ID token, and Cloud Run's IAM check requires that user to hold an invoker role — without it every request fails with **403** right after sign-in. Grant it to a **Google Group** (recommended; add/remove people in the group afterwards) or to individual users, on the service itself:

```bash
gcloud run services add-iam-policy-binding cs-<env>-backend --region=<DEPLOY_REGION> --project=<project> \
  --member="group:<creative-studio-users@your-domain.com>" --role=roles/run.invoker
# individual user instead of a group: --member="user:<someone@your-domain.com>"
```

The same can be done in **IAM & Admin → IAM → Grant access** at project level with the roles **Cloud Run Invoker** (`roles/run.invoker`) and/or **Cloud Run Service Invoker** (`roles/run.servicesInvoker`, the services-only variant) — either is enough to invoke the service. The account that ran the script already has access through its Owner role.

### 3.4 Verify the deployment

1. Open the frontend URL and sign in **with the same Google account you ran the script with** — it is the seeded admin.
2. Cloud Build → History shows both pipelines green; Cloud Run → `cs-<env>-backend` serves a revision whose image comes from `cs-<env>-repo` (not `cloudrun/container/hello`).
3. Sign in with a second account that is a member of the invoker group from §3.3 — it must get past the login screen and load the gallery.

---

## 4. Flags, profiles and non-interactive runs

### 4.1 Flags

| Flag | Effect |
| --- | --- |
| `--profile <name>`, `-p` | Load `~/.cstudio/profiles/<name>.cstudio_bootstrap.conf` and skip the profile menu (headless mode also auto-accepts the detected gcloud project and auto-names the state bucket). |
| `--auto-approve`, `-a` | Skip interactive `terraform apply` and build confirmation prompts. |
| `--skip-builds` | Do not trigger Cloud Build and do not wait for it (infra-only runs). |
| `--force-builds` | Trigger the builds without asking. |
| `--skip-migrations` | Skip the Alembic migrations/seeding job (Step 14). |
| `--skip-seeding` | Same step, intended for quick test loops. |
| `--migrate-db` | Force the safe migration flow: export the current DB (prefers a legacy V1 instance, otherwise the first `cs-*-db-*`), deploy the dummy image, apply, restore into the new instance. |
| `--skip-db-import` | Never prompt about a `migration_backup.sql.gz` found in the state bucket. |
| `--use-existing-backup` | Run the migration flow **without exporting**, reusing the `migration_backup.sql.gz` already in the state bucket (implies `--migrate-db`). See [§6.3](#63-migration-terraform-failed-half-way-and-the-source-database-can-no-longer-be-exported). |
| `--help`, `-h` | Usage. |

Example of a fully non-interactive infra-only run:

```bash
./bootstrap.sh --profile prod --auto-approve --skip-builds --skip-seeding
```

### 4.2 Profiles

A profile is a plain `KEY=VALUE` file in `~/.cstudio/profiles/`. It stores: `GCP_PROJECT_ID`, `REPO_URL`, `REPO_BRANCH`, `REPO_HOST/OWNER/NAME`, `REPO_ROOT`, `ENV_NAME`, `DEPLOY_REGION`, `DB_TIER`, `DB_AVAILABILITY`, `TF_BUCKET_NAME`, `BE_SERVICE_NAME`, `FE_SERVICE_NAME`, `REPO_CONN_NAME`, `FIREBASE_TERMS_ACCEPTED`, `AUTO_OAUTH_CLIENT_ID`, `AUTO_FIREBASE_SITE_ID`.

- One profile per environment (`dev`, `staging`, `prod`, …). Create a new one with `N` in the profile menu.
- Edit a single value with `e` at the *"Use these stored deployment parameters?"* prompt (e.g. `[6] Cloud Build Conn Name` after re-creating a connection, `[9] Deploy Region`).
- Deleting a profile does **not** touch any cloud resource.

### 4.3 Resuming after a failure

Re-run the same command with the same profile. Every step is designed to be re-entrant:

- The project, repository, tfvars, APIs and Firebase app are re-checked, not re-created.
- Terraform reconciles: pre-plan auto-imports adopt pre-existing Hosting/Repository resources if missing from state, and a plan with no changes skips the apply entirely.
- Secret values are written as new versions (harmless).
- The seeding job is re-created and re-run (migrations and seeders are idempotent).

Use `--skip-builds` / `--skip-seeding` to make infra-only re-runs fast.

---

## 5. Redeploying, upgrading and migrating

### 5.1 Application changes only

Push (or sync your fork) to the deployed branch. The Cloud Build triggers rebuild and redeploy whatever changed under `backend/**` or `frontend/**` in ~5 minutes. No Terraform involved.

### 5.2 Infrastructure changes

Re-run `./bootstrap.sh --profile <name>` (add `--auto-approve`, `--skip-builds` as appropriate). Terraform shows the plan before applying. Release notes will say when a version needs this.

### 5.3 Database schema migrations

Schema changes ship as Alembic migrations and are applied by Step 14's Cloud Run Job (which runs `python -m bootstrap.bootstrap` with the freshly built backend image, and also on backend container startup). PITR is enabled on the instance, so no manual dump is required for normal upgrades. A failed job never aborts the deployment ([§6.6](#66-database-migrations--seeding-job-failed)).

### 5.4 Migrating a legacy (V1, public-IP) database

Old deployments used a public-IP instance named `creative-studio-db-<hex>`. The script detects it automatically and offers the **safe migration flow**: export → dummy image → `terraform apply` → drop/recreate `creative_studio` on the new private instance → import → restart the backend. Everything about that flow (triggers, flags, caveats) is documented in [`infrastructure/README.md`](./infrastructure/README.md). The failure mode that bites in practice is covered in [§6.3](#63-migration-terraform-failed-half-way-and-the-source-database-can-no-longer-be-exported).

---

## 6. Troubleshooting (known errors and fixes)

> [!IMPORTANT]
> `bootstrap.sh` is resumable. Unless stated otherwise, the fix is "repair the cause, then re-run the script with the same profile".

### 6.1 Terraform fails: the Cloud Build repository / connection "already exists" (409)

**Symptom** — Step 10 (`terraform apply`) aborts with an error like:

```
Error: Error creating Repository: googleapi: Error 409: Resource
'projects/<project>/locations/<region>/connections/<connection>/repositories/<repo>' already exists
  with google_cloudbuildv2_repository.source_repo
```

(Note: `bootstrap.sh` automatically attempts a pre-plan `terraform import` for both `google_firebase_hosting_site.frontend` and `google_cloudbuildv2_repository.source_repo` when they are absent from state, preventing most `409` errors automatically. You will only see this when running Terraform manually or when a different repository link name collides.)

**Fix** — adopt the existing link into Terraform state (or delete only the link and re-run):

```bash
cd <repo>/infrastructure
terraform init -reconfigure -backend-config=<env>.backend.tfvars
terraform import -var-file=<env>.tfvars google_cloudbuildv2_repository.source_repo \
  projects/<PROJECT_ID>/locations/<DEPLOY_REGION>/connections/<CONNECTION_NAME>/repositories/<REPO_NAME>
```

Or delete only the repository link and re-run `./bootstrap.sh --profile <name>`:

```bash
gcloud builds repositories delete <REPO_NAME> --connection=<CONNECTION_NAME> --region=<DEPLOY_REGION> --project=<PROJECT_ID>
```

### 6.2 Terraform fails: Cloud Build connection not found (404)

**Symptom** — `google_cloudbuildv2_repository.source_repo` fails with `404` / `not found` / `Permission denied on resource … connections/<name>`.

**Why** — Terraform resolves the connection as `projects/<project>/locations/<DEPLOY_REGION>/connections/<repo_conn_name>`. The connection was created in another region, was deleted while troubleshooting, or the name has a typo.

**Fix** — create the connection in the deploy region (same name), or edit profile property `[6]` to the right name, then re-run.

### 6.3 Migration: Terraform failed half-way and the source database can no longer be exported

**Symptom** — a migration run (legacy V1 detected, or `--migrate-db`) exported the data successfully, then `terraform apply` failed part-way (a typical message is `role "studio_user" cannot be dropped because some objects depend on it`). On the next run Step 8 detects the legacy instance again, tries to export it and fails with e.g. `database "creative_studio" does not exist`, then stops with:

```
Failed to export legacy database. Aborting to prevent data loss.
(If a previous run already exported it, re-run with --use-existing-backup.)
```

**Why** — Terraform destroys the V1 `google_sql_database` and `google_sql_user` in parallel; the database can already be gone while the instance survives, so there is nothing left to export. The export from the **first** run is intact: a failed export never overwrites `gs://<project>-cstudio-<env>-tfstate/migration_backup.sql.gz`.

**Fix** — reuse that backup instead of exporting again:

```bash
# 1. Confirm the backup exists and its timestamp predates the failed apply
gcloud storage ls -l gs://<project>-cstudio-<env>-tfstate/migration_backup.sql.gz --project=<project>

# 2. Re-run the migration flow WITHOUT a new export
./bootstrap.sh --profile <name> --use-existing-backup
```

`--use-existing-backup` (which implies `--migrate-db`) makes Step 8 verify the object and skip the export, Step 9 deploy the dummy image, Step 10 apply Terraform, and the import step drop + recreate `creative_studio` on the new instance, restore the dump and restart the backend. The backup file is intentionally left in the bucket afterwards.

> [!CAUTION]
> Do **not** use plain `--migrate-db` in this situation. If the V1 instance is already gone, `--migrate-db` picks the new, **empty** `cs-<env>-db-<hex>` instance as the export source, and that successful export **overwrites** the good `migration_backup.sql.gz`. `--use-existing-backup` is the only flag that never exports.

Manual last resort (the data is always safe in the bucket):

```bash
gcloud sql databases delete creative_studio --instance=<cs-env-db-hex> --project=<project> --quiet
gcloud sql databases create creative_studio --instance=<cs-env-db-hex> --project=<project>
gcloud sql import sql <cs-env-db-hex> gs://<project>-cstudio-<env>-tfstate/migration_backup.sql.gz \
  --database=creative_studio --project=<project>
```

### 6.4 `terraform apply` hangs on the Cloud Run service

**Symptom** — the apply sits for a very long time on `module.compute.google_cloud_run_v2_service.backend: Still modifying…` while changing VPC/database settings of an **existing** service.

**Why** — the live container loses its database connection, the new revision fails its startup probe, and Cloud Run never reports the revision as ready, so Terraform waits. A brand-new service does not deadlock (it starts with the dummy image anyway).

**Fix** — from a second terminal, deploy the dummy image to the service; the revision becomes healthy and the apply completes:

```bash
gcloud run deploy cs-<env>-backend --image us-docker.pkg.dev/cloudrun/container/hello \
  --region <DEPLOY_REGION> --project <project> --quiet
```

Terraform ignores image changes on the service, so this does not fight the apply. Afterwards trigger the builds (or re-run the script with `--force-builds`) to put the real image back. **Prevention:** run migrations/network changes with `--migrate-db`, which deploys the dummy image *before* the apply (Step 9).

If you had to interrupt Terraform and the next run reports `Error acquiring the state lock`, see [§6.13](#613-error-acquiring-the-state-lock).

### 6.5 Import fails with `relation "…" already exists`

**Why** — the target instance was updated in place and still holds tables, so the SQL dump collides with the existing schema.

**Fix** — the script always drops and recreates `creative_studio` right before importing. If you import manually, run the three commands at the end of [§6.3](#63-migration-terraform-failed-half-way-and-the-source-database-can-no-longer-be-exported) in that order.

### 6.6 Database migrations / seeding job failed

**Symptom** — Step 14 prints *"Database seeding failed! Please check logs in the Cloud Run Job console for 'temp-db-bootstrap-job'"* and continues. Or it aborts with *"Backend service not updated after 15 minutes"*.

**Fix**

```bash
# inspect
gcloud run jobs executions list --job temp-db-bootstrap-job --region <DEPLOY_REGION> --project <project>
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="temp-db-bootstrap-job"' \
  --project <project> --limit 200 --format='value(textPayload)'

# fix the cause, then re-execute the same job
gcloud run jobs execute temp-db-bootstrap-job --region <DEPLOY_REGION> --project <project> --wait

# clean up once it succeeds
gcloud run jobs delete temp-db-bootstrap-job --region <DEPLOY_REGION> --project <project> --quiet
```

### 6.7 Backend revision still reading placeholder secrets

**Why** — Cloud Run resolves `latest` Secret Manager versions **only when a revision starts**. If a secret was updated after the backend revision started (e.g. with `--skip-builds`), start a fresh revision on the **same image** (an env-var "restart trigger" would become permanent Terraform drift):

```bash
IMG=$(gcloud run services describe cs-<env>-backend --region <DEPLOY_REGION> --project <project> --format='value(image)')
gcloud run deploy cs-<env>-backend --image "$IMG" --region <DEPLOY_REGION> --project <project> --quiet
```

### 6.8 Google sign-in fails (`auth/unauthorized-domain`, `redirect_uri_mismatch`, origin not allowed)

- `auth/unauthorized-domain` — Firebase console → **Authentication → Settings → Authorized domains** → add your Hosting domain (e.g. `<site>.web.app`). This happens when the Hosting site ID differs from the project ID.
- `redirect_uri_mismatch` / *"The given origin is not allowed"* — the OAuth Web client is missing the Hosting origins (Step 11 can only update IAP-managed clients). Add them as described in [§3.3 item 1](#33-open-the-app-to-your-users-before-verifying).
- The login succeeds but every API call returns **403** — the user has no Cloud Run invoker role on `cs-<env>-backend`; see [§3.3 item 3](#33-open-the-app-to-your-users-before-verifying).
- `auth/operation-not-allowed` — the Google provider is disabled; enable it (Step 6 (b)).

### 6.9 *"Could not automatically fetch Firebase API Key"*

Firebase is not linked to the project yet (Step 6 (b) not completed), `firebase login` is missing on a workstation, or the web app `cs-<env>-frontend` could not be created. Fix the cause and re-run; the step is idempotent.

### 6.10 The script picked up the wrong environment / state bucket

Step 5 auto-detects the **first** `<project>-cstudio-*-tfstate` bucket and proposes its environment name and bucket, which is what you want when updating a single deployment. If you intentionally run a second environment in the same project, answer `n` to *"Would you like to auto-correct the Terraform state bucket…?"*, keep your own `ENV_NAME`, and use a dedicated profile (and a dedicated Cloud Build connection, see §6.1).

### 6.11 Organisation policy / region errors

Errors mentioning `constraints/gcp.resourceLocations` (or a resource "not available in location") mean the chosen region is not allowed. Change profile property `[9] Deploy Region` and re-run — the script never hardcodes `us-central1`.

### 6.12 Export/import fails with an IAM binding error

Step 8/10 print `IAM Binding Error` when they cannot grant the Cloud SQL instance's service account access to the state bucket (`roles/storage.objectAdmin` for export, `roles/storage.objectViewer` for import). Your account needs `roles/storage.admin` on the project (Owner has it). Grant it and re-run. If you **are** Owner and the error says `storage.buckets.setIamPolicy … (or it may not exist)` only for buckets that already existed, an organisation guardrail is blocking you — see [§6.16](#616-a-project-owner-gets-storagebucketssetiampolicy--getiampolicy-denied-on-the-existing-buckets).

### 6.13 `Error acquiring the state lock`

A previous Terraform run was interrupted (Ctrl‑C, closed Cloud Shell). Make sure no other run is active, then:

```bash
cd <repo>/infrastructure
terraform init -reconfigure -backend-config=<env>.backend.tfvars
terraform force-unlock <LOCK_ID>     # the ID is printed in the error message
```

### 6.14 Cloud Build pipelines fail or never start

- The triggers fire on pushes to **exactly** the configured branch (`^<branch>$`) and the script runs them on the **current branch of the clone**; if Step 13 said "Could not parse ID", open the Cloud Build history to see why.
- The frontend pipeline reads the `FIREBASE_*` / `GOOGLE_CLIENT_ID` secrets at build time — if it fails on *"placeholder"* values, Step 12 did not complete; re-run.
- The trigger service account is `cs-trig-sa@<project>.iam.gserviceaccount.com`; its IAM roles are managed by Terraform (`ci_cd.tf`). Re-running the script restores any role someone removed.
- To re-run builds without touching anything else: `./bootstrap.sh --profile <name> --force-builds --skip-seeding`.

### 6.15 The console cannot create the host connection: *"Error processing oauth callback: Structured justification provided is incorrect"*

**Symptom** — in the Cloud Build "Create host connection" wizard, after *"Project N is requesting your GitHub OAuth token… the token will be stored in Secret Manager"*, clicking **Continue** fails with `Error processing oauth callback: Structured justification provided is incorrect`. Seen on projects whose organisation gates privileged console paths behind access justifications.

**Why** — the wizard finishes through an internal RPC, `google.devtools.cloudbuild.v2.RepositoryManagerInternal.ProcessGitHubOAuthCallback`, which Cloud Audit Logs record under **your** identity with exactly that message; that RPC is the only thing the organisation rejects. The public `RepositoryManager.CreateConnection` API, Secret Manager and the Cloud Build service agent all work normally.

**Fix** — create the connection **programmatically**: store a classic GitHub PAT (`repo`, `read:user`, plus `read:org` for org forks) in Secret Manager and create the connection referencing it (official flow: [Connect to a GitHub host programmatically](https://cloud.google.com/build/docs/automating-builds/github/connect-repo-github?generation=2nd-gen#connect_to_a_github_host_programmatically)):

```bash
PROJECT_ID=<project>; REGION=<DEPLOY_REGION>; CONN=<connection-name>; INSTALLATION_ID=<id>
PN=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')

read -rs GITHUB_PAT
echo -n "$GITHUB_PAT" | gcloud secrets create github-pat-cloudbuild --data-file=- \
  --replication-policy=user-managed --locations="$REGION" --project="$PROJECT_ID"
gcloud secrets add-iam-policy-binding github-pat-cloudbuild --project="$PROJECT_ID" \
  --member="serviceAccount:service-${PN}@gcp-sa-cloudbuild.iam.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor

gcloud builds connections create github "$CONN" --region="$REGION" --project="$PROJECT_ID" \
  --authorizer-token-secret-version="projects/${PROJECT_ID}/secrets/github-pat-cloudbuild/versions/1" \
  --app-installation-id="$INSTALLATION_ID"
```

**Fallback if no connection can exist at all** — run the two pipelines directly from a local clone with `gcloud builds submit` using the trigger service account:

```bash
cd <repo>
gcloud builds submit . --config=backend/cloudbuild.yaml --project="$PROJECT_ID" --region="$REGION" \
  --service-account="projects/${PROJECT_ID}/serviceAccounts/cs-trig-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --substitutions="_GCP_REGION=${REGION},_REPO_NAME=cs-<env>-repo,_SERVICE_NAME=cs-<env>-backend,SHORT_SHA=$(git rev-parse --short HEAD)"

gcloud builds submit . --config=frontend/cloudbuild-deploy.yaml --project="$PROJECT_ID" --region="$REGION" \
  --service-account="projects/${PROJECT_ID}/serviceAccounts/cs-trig-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --substitutions="_GCP_REGION=${REGION},_BACKEND_URL=https://<site>.web.app,_BACKEND_SERVICE_ID=cs-<env>-backend,_FIREBASE_SITE_ID=<site>,_FE_SERVICE_NAME=cs-<env>-frontend"
```

### 6.16 A project Owner gets `storage.buckets.setIamPolicy` / `getIamPolicy` denied on the *existing* buckets

**Why** — an organisation-level **IAM deny policy** (or similar guardrail) that targets human principals on existing buckets while allowing service accounts.

**Fix — run everything as a deployer service account:**

```bash
PROJECT_ID=<project>; REGION=<DEPLOY_REGION>; ME=$(gcloud config get-value account)
DEPLOYER="cs-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud services enable iamcredentials.googleapis.com --project="$PROJECT_ID"
gcloud iam service-accounts create cs-deployer --display-name="Creative Studio deployer" --project="$PROJECT_ID"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$DEPLOYER" --role=roles/owner
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER" --project="$PROJECT_ID" \
  --member="user:$ME" --role=roles/iam.serviceAccountTokenCreator

export GOOGLE_IMPERSONATE_SERVICE_ACCOUNT="$DEPLOYER"
export CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT="$GOOGLE_IMPERSONATE_SERVICE_ACCOUNT"
bash ./bootstrap.sh --profile <name> [--use-existing-backup]
```

---

## 7. Running Terraform by hand & optional variables (VPE)

All of this is what `bootstrap.sh` does under the hood; use it for debugging or for operations the script does not expose (imports, targeted applies, destroy).

```bash
cd <repo>/infrastructure
terraform init -reconfigure -upgrade -backend-config=<env>.backend.tfvars
terraform plan  -var-file=<env>.tfvars
terraform apply -parallelism=30 -var-file=<env>.tfvars
terraform output   # cloud_sql_connection_name, backend_service_url, frontend_service_url, db_secret_id, ...
```

> [!WARNING]
> `<env>.tfvars` is **regenerated by `bootstrap.sh` on every run** (and gitignored). Do not store manual customizations in `<env>.tfvars`. Variables `bootstrap.sh` does not prompt for — such as `custom_domain`, `cloud_run_cidr`, `vpe_*`, and `extra_env_vars` — should be passed via `TF_VAR_*` environment variables, which Terraform reads automatically while `bootstrap.sh` runs.

### 7.1 Configuring Video Processing Engine (VPE) & extra environment variables

[`infrastructure/variables.tf`](./infrastructure/variables.tf) and [`infrastructure/app.tf`](./infrastructure/app.tf) expose first-class variables for VPE 4K video upscaling (off by default) as well as `extra_env_vars` for any additional backend environment variables (e.g. `VPE_MAX_CONCURRENT_JOBS`, `IDENTITY_PLATFORM_ALLOWED_ORGS`, `ALLOWED_EMAILS`):

| Environment variable | Terraform variable | Default | Description |
| --- | --- | --- | --- |
| `TF_VAR_vpe_enabled` | `vpe_enabled` | `false` | Enables VPE routes (`VPE_ENABLED`) |
| `TF_VAR_vpe_project_id` | `vpe_project_id` | `""` | Allowlisted GCP project ID (`VPE_PROJECT_ID`) |
| `TF_VAR_vpe_bucket` | `vpe_bucket` | `""` | Dedicated scratch bucket inside `vpe_project_id` (`VPE_BUCKET`) |
| `TF_VAR_vpe_location` | `vpe_location` | `"us-central1"` | Region for VPE jobs (`VPE_LOCATION`) |
| `TF_VAR_vpe_dry_run` | `vpe_dry_run` | `false` | Dry-run request logging (`VPE_DRY_RUN`) |
| `TF_VAR_extra_env_vars` | `extra_env_vars` | `{}` | Map merged into Cloud Run `container_env_vars` |

Example enabling VPE during a `bootstrap.sh` run (after completing the project allowlisting and `backend/scripts/vpe_bootstrap_project.sh` steps in [`UPGRADING.md`](./UPGRADING.md#compatibility-notes-for-the-vpe-changes)):

```bash
TF_VAR_vpe_enabled=true \
TF_VAR_vpe_project_id="<allowlisted-project>" \
TF_VAR_vpe_bucket="<dedicated-vpe-bucket>" \
TF_VAR_vpe_location="us-central1" \
TF_VAR_extra_env_vars='{"VPE_MAX_CONCURRENT_JOBS":"3"}' \
./bootstrap.sh --profile prod --auto-approve
```

---

## 8. Tearing a deployment down

```bash
# 1. Keep your data (Cloud SQL deletion protection is OFF in this configuration)
gcloud sql export sql <cs-env-db-hex> gs://<project>-cstudio-<env>-tfstate/final_backup.sql.gz \
  --database=creative_studio --project=<project>

# 2. Destroy what Terraform manages
cd <repo>/infrastructure
terraform init -reconfigure -backend-config=<env>.backend.tfvars
terraform destroy -var-file=<env>.tfvars
```

Not managed by Terraform — delete manually if you want a clean project: the Terraform state bucket, the Cloud Build host connection, the Firebase web app / Authentication provider, a leftover `temp-db-bootstrap-job`, and the local profile in `~/.cstudio/profiles/`. `terraform destroy` fails on the asset bucket while it still contains objects (no `force_destroy`) — empty it first if you really want it gone. Enabled APIs are never disabled.

---

## 9. Reference: files the script generates

| Path | Purpose | Tracked in git? |
| --- | --- | --- |
| `~/.cstudio/profiles/<name>.cstudio_bootstrap.conf` | Deployment profile (resume state) | No (outside the repo) |
| `infrastructure/<env>.backend.tfvars` | GCS backend: `bucket` + `prefix` | No (`*.tfvars` is gitignored) |
| `infrastructure/<env>.tfvars` | Root variables; rewritten every run | No |
| `infrastructure/tfplan` | Transient plan file, removed after apply | No |
| `infrastructure/.terraform/` | Provider cache / backend config | No |
| `~/bin/terraform` | Terraform installed by the script when needed | — |
| `gs://<project>-cstudio-<env>-tfstate/infra/<env>/state/` | Terraform state | — |
| `gs://<project>-cstudio-<env>-tfstate/migration_backup.sql.gz` | Migration dump (kept as a safeguard) | — |
