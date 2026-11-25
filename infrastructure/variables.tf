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

variable "project_id" {
  type = string
}

variable "region" {
  type = string
}

variable "environment" {
  type    = string
  default = "development"
}

variable "apis_to_enable" {
  type = list(string)
  default = [
    "serviceusage.googleapis.com",     # Required to enable other APIs
    "iam.googleapis.com",              # Required for IAM management
    "cloudbuild.googleapis.com",       # Required for Cloud Build
    "artifactregistry.googleapis.com", # Required for Artifact Registry
    "run.googleapis.com",              # Required for Cloud Run
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "cloudfunctions.googleapis.com",
    "iamcredentials.googleapis.com",
    "aiplatform.googleapis.com",
    "firestore.googleapis.com",
    "texttospeech.googleapis.com",
    "workflows.googleapis.com",
    "drive.googleapis.com",
    "servicenetworking.googleapis.com", # Required for Private VPC Peering (Cloud SQL)
    "sqladmin.googleapis.com",          # Required for Cloud SQL instance management
    "secretmanager.googleapis.com",     # Required for application secret management
    "vpcaccess.googleapis.com"          # Required for Serverless Direct VPC routing
  ]
}

variable "custom_domain" {
  type        = string
  description = "Optional custom domain name to link to Firebase Hosting (e.g., 'app.creativestudio.com'). Leave blank to skip."
  default     = ""
}

variable "application_secrets" {
  type        = set(string)
  description = "List of Secret Manager secret IDs mounted by the backend Cloud Run service."
  default = [
    "GOOGLE_TOKEN_AUDIENCE",
    "GOOGLE_CLIENT_ID",
  ]
}

variable "frontend_secrets" {
  type        = set(string)
  description = "The list of application secret identifiers required by the frontend application layer."
  default     = [
    "FIREBASE_API_KEY",
    "FIREBASE_AUTH_DOMAIN",
    "FIREBASE_PROJECT_ID",
    "FIREBASE_STORAGE_BUCKET",
    "FIREBASE_MESSAGING_SENDER_ID",
    "FIREBASE_APP_ID",
    "FIREBASE_MEASUREMENT_ID",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_TOKEN_AUDIENCE"
  ]
}

variable "resource_prefix" {
  type        = string
  description = "Standard naming prefix assigned to the deployment."
}

variable "cloud_run_cidr" {
  type        = string
  description = "Dedicated CIDR range for Cloud Run Direct VPC egress."
  default     = "10.0.0.0/26"
}

variable "backend_image_name" {
  type        = string
  description = "The name of the backend container image."
  default     = "backend"
}

variable "backend_image_tag" {
  type        = string
  description = "The tag of the backend container image."
  default     = "latest"
}



variable "firebase_site_id" {
  type        = string
  description = "The Firebase Hosting Site ID. If empty, defaults to the project ID."
  default     = ""
}

variable "labels" {
  type        = map(string)
  description = "Standard resource labels to apply across resources."
  default     = {}
}

variable "app_version" {
  type = string
  description = "version of the creative studio app we're deploying"
  default = "latest"
}

# --- Repository CI/CD Configuration ---
variable "repo_conn_name" {
  type        = string
  description = "The name of the Cloud Build repository connection."
}

variable "repo_host" {
  type        = string
  description = "The host of the repository (e.g., github.com, gitlab.com)."
  default     = "github.com"
}

variable "repo_owner" {
  type        = string
  description = "The owner of the repository."
}

variable "repo_name" {
  type        = string
  description = "The name of the repository."
}

variable "repo_branch_name" {
  type        = string
  description = "The branch name to trigger builds from."
}

variable "asset_bucket_name_override" {
  type        = string
  description = "Override the default asset bucket name (useful for legacy migrations)."
  default     = ""
}

variable "db_tier" {
  type        = string
  description = "The machine tier/type for the Cloud SQL instance."
  default     = "db-custom-2-7680"
}

variable "db_availability_type" {
  type        = string
  description = "The availability type for the Cloud SQL instance (ZONAL or REGIONAL)."
  default     = "ZONAL"
}


variable "custom_audiences" {
  type        = list(string)
  description = "Custom OIDC audiences accepted by the backend Cloud Run service."
  default     = []
}

# --- Optional Veo Pro Experimental (VPE) Configuration ---
variable "vpe_enabled" {
  type        = bool
  description = "Enable Veo Pro Experimental capabilities (4K Video Upscale, Lip Sync, Video Transform)."
  default     = false
}

variable "vpe_project_id" {
  type        = string
  description = "Allowlisted GCP project ID for VPE prediction jobs (defaults to project_id when empty)."
  default     = ""
}

variable "vpe_bucket" {
  type        = string
  description = "GCS bucket for VPE staging inputs and outputs."
  default     = ""
}

variable "vpe_location" {
  type        = string
  description = "Vertex AI region for VPE jobs."
  default     = "us-central1"
}

variable "vpe_dry_run" {
  type        = bool
  description = "When true, VPE builds payloads without submitting paid Vertex jobs."
  default     = false
}

variable "extra_env_vars" {
  type        = map(string)
  description = "Optional extra non-secret environment variables for the backend service."
  default     = {}
}
