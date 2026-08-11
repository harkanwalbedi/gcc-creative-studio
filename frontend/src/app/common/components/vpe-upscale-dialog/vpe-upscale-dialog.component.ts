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
  VpeFindingResponse,
  VpeScreeningResponse,
  VpeUpscaleResolution,
} from '../../models/vpe.model';
import {VpeService} from '../../../services/vpe/vpe.service';
import {handleErrorSnackbar} from '../../../utils/handleMessageSnackbar';

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
  readonly resolutionOptions: DropdownOption[] = [
    {value: VpeUpscaleResolution.UHD_4K, label: '4K'},
    {value: VpeUpscaleResolution.HD_1080P, label: '1080p'},
  ];

  resolution: VpeUpscaleResolution = VpeUpscaleResolution.UHD_4K;
  // Matches the backend parameter's own default, read off the same registry
  // the DTO validates against - see UpscaleVideoDto.sharpness.
  sharpness = 1;
  isSubmitting = false;

  constructor(
    public dialogRef: MatDialogRef<VpeUpscaleDialogComponent>,
    @Inject(MAT_DIALOG_DATA) public data: VpeUpscaleDialogData,
    private vpeService: VpeService,
    private snackBar: MatSnackBar,
  ) {}

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

  onResolutionChange(value: VpeUpscaleResolution): void {
    this.resolution = value;
  }

  onSharpnessChange(value: number): void {
    this.sharpness = value;
  }

  confirm(): void {
    if (this.isSubmitting) {
      return;
    }
    this.isSubmitting = true;
    this.vpeService
      .startUpscale({
        workspaceId: this.data.workspaceId,
        mediaItemId: this.data.mediaItemId,
        mediaIndex: this.data.mediaIndex,
        resolution: this.resolution,
        sharpness: this.sharpness,
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
