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

import {Component, Inject} from '@angular/core';
import {MAT_DIALOG_DATA, MatDialogRef} from '@angular/material/dialog';
import {MatSnackBar} from '@angular/material/snack-bar';
import {DropdownOption} from '../studio-dropdown/studio-dropdown.component';
import {MediaItem} from '../../models/media-item.model';
import {
  VpeCompressionQuality,
  VpeFindingResponse,
  VpeOutputCodec,
  VpeScreeningResponse,
  VpeUpscaleMode,
  VpeUpscaleResolution,
} from '../../models/vpe.model';
import {VpeService} from '../../../services/vpe/vpe.service';
import {handleErrorSnackbar} from '../../../utils/handleMessageSnackbar';

export type VpeDeliveryFormat =
  | 'h264_optimized'
  | 'prores_optimized'
  | 'prores_lossless'
  | 'dnxhr_optimized';

export interface VpeUpscaleDialogData {
  workspaceId: number;
  mediaItemId: number;
  mediaIndex: number;
  screening: VpeScreeningResponse;
}

@Component({
  selector: 'app-vpe-upscale-dialog',
  templateUrl: './vpe-upscale-dialog.component.html',
  styleUrls: ['./vpe-upscale-dialog.component.scss'],
})
export class VpeUpscaleDialogComponent {
  readonly allModeOptions: {value: VpeUpscaleMode; label: string}[] = [
    {value: '4k', label: '4K'},
    {value: '4k_pro', label: '4K Pro'},
    {value: '8k', label: '8K'},
    {value: '1080p', label: '1080p'},
  ];

  readonly stepOptions: DropdownOption[] = [
    {value: 1, label: '1 — Fast'},
    {value: 2, label: '2 — Max Detail'},
  ];

  /**
   * GMP 8K rule: DNxHR Lossless is unsupported at 8K, so we expose DNxHR HQ
   * (`optimized`) and Apple ProRes 4444 (`lossless`), both valid up to 8K.
   */
  readonly deliveryFormatOptions: DropdownOption[] = [
    {value: 'h264_optimized', label: 'MP4 (H.264)'},
    {value: 'prores_optimized', label: 'ProRes 422 (.mov)'},
    {value: 'prores_lossless', label: 'ProRes 4444 Lossless (.mov)'},
    {value: 'dnxhr_optimized', label: 'DNxHR HQ (.mxf)'},
  ];

  modeOptions: DropdownOption[];
  upscaleMode: VpeUpscaleMode = '4k';
  resolution: VpeUpscaleResolution = VpeUpscaleResolution.UHD_4K;
  steps: 1 | 2 = 1;
  // Matches the backend parameter's own default, read off the same registry
  // the DTO validates against - see UpscaleVideoDto.sharpness.
  sharpness = 1;
  deliveryFormat: VpeDeliveryFormat = 'h264_optimized';
  outputCodec: VpeOutputCodec = 'h264';
  compressionQuality: VpeCompressionQuality = 'optimized';
  isSubmitting = false;

  constructor(
    public dialogRef: MatDialogRef<VpeUpscaleDialogComponent>,
    @Inject(MAT_DIALOG_DATA) public data: VpeUpscaleDialogData,
    private vpeService: VpeService,
    private snackBar: MatSnackBar,
  ) {
    const supported = this.data.screening?.supported_modes;
    if (supported && supported.length > 0) {
      const filtered = this.allModeOptions.filter(opt =>
        supported.includes(opt.value),
      );
      this.modeOptions =
        filtered.length > 0 ? filtered : [...this.allModeOptions];
    } else {
      this.modeOptions = [...this.allModeOptions];
    }

    const defaultMode = (
      this.modeOptions.some(opt => opt.value === '4k')
        ? '4k'
        : this.modeOptions[0].value
    ) as VpeUpscaleMode;
    this.onModeChange(defaultMode);
  }

  /**
   * Non-blocking findings worth showing. The screening that got the dialog
   * opened already ruled out the fatal case - it only rules out, never
   * confirms - so anything left here is a caveat, not a reason to refuse.
   */
  get warnings(): VpeFindingResponse[] {
    return this.data.screening.findings.filter(
      finding => finding.severity !== 'ok',
    );
  }

  get isSuperResolution(): boolean {
    return this.upscaleMode !== '1080p';
  }

  onModeChange(value: VpeUpscaleMode): void {
    this.upscaleMode = value;
    this.resolution =
      value === '1080p'
        ? VpeUpscaleResolution.HD_1080P
        : VpeUpscaleResolution.UHD_4K;
    if (
      value === '8k' &&
      this.outputCodec === 'dnxhr' &&
      this.compressionQuality === 'lossless'
    ) {
      this.compressionQuality = 'optimized';
      this.deliveryFormat = 'dnxhr_optimized';
    }
  }

  onStepsChange(value: 1 | 2): void {
    this.steps = value;
  }

  onSharpnessChange(value: number): void {
    this.sharpness = value;
  }

  onDeliveryFormatChange(format: VpeDeliveryFormat): void {
    this.deliveryFormat = format;
    switch (format) {
      case 'prores_optimized':
        this.outputCodec = 'prores';
        this.compressionQuality = 'optimized';
        break;
      case 'prores_lossless':
        this.outputCodec = 'prores';
        this.compressionQuality = 'lossless';
        break;
      case 'dnxhr_optimized':
        this.outputCodec = 'dnxhr';
        this.compressionQuality = 'optimized';
        break;
      case 'h264_optimized':
      default:
        this.outputCodec = 'h264';
        this.compressionQuality = 'optimized';
        break;
    }
  }

  confirm(): void {
    if (this.isSubmitting) {
      return;
    }
    this.isSubmitting = true;

    const effectiveResolution =
      this.upscaleMode === '1080p'
        ? VpeUpscaleResolution.HD_1080P
        : VpeUpscaleResolution.UHD_4K;
    const effectiveQuality: VpeCompressionQuality =
      this.upscaleMode === '8k' &&
      this.outputCodec === 'dnxhr' &&
      this.compressionQuality === 'lossless'
        ? 'optimized'
        : this.compressionQuality;

    this.vpeService
      .startUpscale({
        workspaceId: this.data.workspaceId,
        mediaItemId: this.data.mediaItemId,
        mediaIndex: this.data.mediaIndex,
        resolution: effectiveResolution,
        upscaleMode: this.upscaleMode,
        ...(this.upscaleMode !== '1080p' ? {steps: this.steps} : {}),
        sharpness: this.sharpness,
        outputCodec: this.outputCodec,
        compressionQuality: effectiveQuality,
      })
      .subscribe({
        next: (placeholder: MediaItem) => {
          this.isSubmitting = false;
          this.dialogRef.close(placeholder);
        },
        error: err => {
          this.isSubmitting = false;
          handleErrorSnackbar(this.snackBar, err, 'Start upscale');
        },
      });
  }
}
