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
"""Veo Pro Experimental (VPE) support.

VPE is a third video API surface, separate from the app's google-genai Veo
path and its Omni interactions path: raw REST against the shared
``veo-experimental`` publisher endpoint, GCS in and GCS out, long-running
operations. Everything in this package is inert unless
``config_service.VPE_ENABLED`` is true.

Nothing is re-exported here on purpose. Modules import from
``src.videos.vpe.capabilities`` (and its siblings) directly, so adding a
module cannot change what a package-level import pulls in.
"""
