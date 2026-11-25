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

# --- Artifact Registry Module ---
module "artifact" {
  source = "./modules/artifact"

  project_id      = var.project_id
  region          = var.region
  resource_prefix = var.resource_prefix
  environment     = var.environment

  depends_on = [google_project_service.apis]
}

# --- Compute Module (Cloud Run) ---
module "compute" {
  source = "./modules/compute"

  project_id       = var.project_id
  region           = var.region
  resource_prefix  = var.resource_prefix
  environment      = var.environment
  custom_audiences = var.custom_audiences
  depends_on       = [google_secret_manager_secret_version.app_secrets_placeholder]

  # The deployment script dynamically sets this value (e.g., "latest" or "v1.2.0")
  app_version           = var.app_version
  service_name          = "${var.resource_prefix}-${var.environment}-backend"
  cpu                   = "2"
  memory                = "8Gi"
  scaling_min_instances = 1

  # Direct VPC routing configurations
  vpc_network_name = module.network.network_name
  vpc_subnet_name  = module.network.cloud_run_subnet_name
  database_ip      = module.database.private_ip_address

  # Dynamically construct the image URL.
  # For initial deployments (when version is 'latest'), we use a Google placeholder image
  # to prevent Cloud Run from crashing before the actual Docker image is built and pushed.
  image_url = var.app_version == "latest" ? "us-docker.pkg.dev/cloudrun/container/hello" : "${module.artifact.repository_url}/${var.backend_image_name}:${var.app_version}"

  # References the list keys to configure secret environment block mappings
  secret_ids = var.application_secrets

  container_env_vars = merge(
    {
      PROJECT_ID               = var.project_id
      REGION                   = var.region
      DB_NAME                  = module.database.database_name
      DB_USER                  = module.database.user_name
      INSTANCE_CONNECTION_NAME = module.database.instance_connection_name
      GENMEDIA_BUCKET          = google_storage_bucket.genmedia.name
      WORKFLOWS_LOCATION       = var.region
      FRONTEND_URL             = "https://${google_firebase_hosting_site.frontend.site_id}.web.app"
      BACKEND_URL              = local.backend_url
      WORKFLOWS_EXECUTOR_URL   = "${local.backend_url}/api/workflows-executor"
      SIGNING_SA_EMAIL         = google_service_account.bucket_reader_sa.email
      VPE_ENABLED              = tostring(var.vpe_enabled)
      VPE_PROJECT_ID           = var.vpe_project_id
      VPE_BUCKET               = var.vpe_bucket
      VPE_LOCATION             = var.vpe_location
      VPE_DRY_RUN              = tostring(var.vpe_dry_run)
    },
    var.extra_env_vars
  )

  runtime_secrets = {
    "DB_PASS" = module.database.secret_id
  }
}
