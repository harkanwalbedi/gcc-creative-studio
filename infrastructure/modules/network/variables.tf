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
  type        = string
  description = "The customer's Google Cloud project ID."
}

variable "region" {
  type        = string
  description = "The primary GCP region for network subnets."
}

variable "resource_prefix" {
  type        = string
  description = "Standard naming prefix assigned to the customer deployment."
}

variable "environment" {
  type        = string
  description = "Deployment environment identifier (e.g., 'dev', 'stg', 'prd')."
}

variable "cloud_run_cidr" {
  type        = string
  description = "Dedicated CIDR range for Cloud Run Direct VPC egress (minimum /28 recommended)."
  default     = "10.0.0.0/26"
}
