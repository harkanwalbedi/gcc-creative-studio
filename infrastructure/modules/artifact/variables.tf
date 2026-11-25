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
  description = "The GCP project ID."
}

variable "region" {
  type        = string
  description = "The GCP region where the Artifact Registry will be created."
}

variable "resource_prefix" {
  type        = string
  description = "Standard naming prefix assigned to the deployment."
}

variable "environment" {
  type        = string
  description = "Deployment environment identifier."
}

variable "labels" {
  type        = map(string)
  description = "A map of labels to apply to the resource."
  default     = {}
}

variable "repository_id" {
  type        = string
  description = "The ID of the repository."
  default     = "repo"
}

variable "remote_uri" {
  type        = string
  description = "The URI of the remote repository to proxy."
  default     = "https://ghcr.io"
}

variable "cleanup_older_than" {
  type        = string
  description = "Cleanup policy condition: delete cached layers unaccessed for this duration."
  default     = "2592000s"
}
