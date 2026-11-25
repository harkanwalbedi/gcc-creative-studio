/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/** Matches the backend's VpeUpscaleResolution wire values. */
export enum VpeUpscaleResolution {
  HD_1080P = '1080p',
  UHD_4K = '4k',
}

export type VpeUpscaleMode = '1080p' | '4k' | '4k_pro' | '8k';
export type VpeOutputCodec = 'h264' | 'prores' | 'dnxhr';
export type VpeCompressionQuality = 'optimized' | 'lossless';

export interface VpeConfigResponse {
  enabled: boolean;
  capabilities: string[];
}

export interface VpeFindingResponse {
  code: string;
  severity: string;
  message: string;
  remedy: string;
  measured: string;
  required: string;
}

/**
 * Whether a gallery row is worth offering the upscale action on.
 *
 * `VpeScreeningResponse` on the backend is a plain Pydantic model with no
 * camelCase alias generator (unlike most DTOs in this app), so
 * `capability_id` and `supported_modes` stay snake_case on the wire.
 */
export interface VpeScreeningResponse {
  capability_id: string;
  screening: 'ruled_out' | 'not_ruled_out' | 'unknown';
  offer: boolean;
  findings: VpeFindingResponse[];
  supported_modes?: string[];
}

export interface UpscaleVideoDto {
  workspaceId: number;
  mediaItemId: number;
  mediaIndex: number;
  resolution: VpeUpscaleResolution;
  upscaleMode?: VpeUpscaleMode;
  steps?: 1 | 2;
  sharpness?: number;
  seed?: number;
  outputCodec?: VpeOutputCodec;
  compressionQuality?: VpeCompressionQuality;
}
