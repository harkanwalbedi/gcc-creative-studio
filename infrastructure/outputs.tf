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

output "firebase_dns_verification_records" {
  description = "The target DNS records you must add to your domain registrar to verify ownership and activate SSL."
  # Safely handle the output depending on whether the resource count is 0 or 1
  value = length(google_firebase_hosting_custom_domain.custom_domain) > 0 ? (
    google_firebase_hosting_custom_domain.custom_domain[0].required_dns_updates
  ) : null
}

output "cloud_sql_connection_name" {
  description = "The connection name of the Cloud SQL instance."
  value       = module.database.instance_connection_name
}

output "db_name" {
  description = "The name of the database."
  value       = module.database.database_name
}

output "db_user" {
  description = "The database user name."
  value       = module.database.user_name
}

output "db_secret_id" {
  description = "Secret Manager secret ID for DB Password."
  value       = module.database.secret_id
}

output "frontend_service_url" {
  description = "The default Firebase Hosting URL."
  value       = "https://${google_firebase_hosting_site.frontend.site_id}.web.app"
}

output "backend_service_url" {
  description = "The URL of the backend service."
  value       = module.compute.service_uri
}

output "cloud_run_subnet_name" {
  description = "The subnet name allocated for Cloud Run Direct VPC Egress."
  value       = module.network.cloud_run_subnet_name
}

output "backend_secrets" {
  description = "Application secrets created for the backend layer."
  value       = tolist(var.application_secrets)
}

output "frontend_secrets" {
  description = "Application secrets created for the frontend layer."
  value       = tolist(var.frontend_secrets)
}
