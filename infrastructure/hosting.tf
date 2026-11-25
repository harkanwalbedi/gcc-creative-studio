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

# Creates the Firebase Hosting site to deploy to
resource "google_firebase_hosting_site" "frontend" {
  provider = google-beta
  project  = var.project_id
  site_id  = var.firebase_site_id
}

# Conditionally map the Custom Domain if provided
resource "google_firebase_hosting_custom_domain" "custom_domain" {
  provider = google-beta
  # Evaluates to 1 if a domain string is provided, 0 if left blank
  count = var.custom_domain != "" ? 1 : 0

  project       = var.project_id
  site_id       = google_firebase_hosting_site.frontend.site_id
  custom_domain = var.custom_domain

  # Prevent Terraform runs from timing out while waiting for manual DNS propagation
  wait_dns_verification = false
}
