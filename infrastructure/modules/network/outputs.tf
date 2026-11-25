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

output "network_id" {
  description = "The fully qualified ID of the VPC network."
  value       = google_compute_network.vpc.id
}

output "network_name" {
  description = "The simple name of the VPC network."
  value       = google_compute_network.vpc.name
}

output "cloud_run_subnet_name" {
  description = "The name of the subnet allocated for Cloud Run Direct VPC Egress."
  value       = google_compute_subnetwork.cloud_run_subnet.name
}

# Architectural Guardrail: Resolves classic GCP Terraform deployment race conditions.
# Cloud SQL modules must explicitly reference this output in their 'depends_on' block
# to guarantee the peering connection finishes provisioning before database creation begins.
output "peering_completed" {
  description = "Dependency trigger string confirming Private Services Access peering is active."
  value       = google_service_networking_connection.private_vpc_connection.id
}
