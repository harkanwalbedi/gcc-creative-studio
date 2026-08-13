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

import {CommonModule} from '@angular/common';
import {NO_ERRORS_SCHEMA} from '@angular/core';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {MAT_DIALOG_DATA, MatDialogRef} from '@angular/material/dialog';
import {MatSnackBar} from '@angular/material/snack-bar';
import {Subject, of, throwError} from 'rxjs';

import {
  VpeUpscaleDialogComponent,
  VpeUpscaleDialogData,
} from './vpe-upscale-dialog.component';
import {JobStatus, MediaItem} from '../../models/media-item.model';
import {
  VpeFindingResponse,
  VpeScreeningResponse,
  VpeUpscaleResolution,
} from '../../models/vpe.model';
import {VpeService} from '../../../services/vpe/vpe.service';

describe('VpeUpscaleDialogComponent', () => {
  let component: VpeUpscaleDialogComponent;
  let fixture: ComponentFixture<VpeUpscaleDialogComponent>;
  let vpeService: jasmine.SpyObj<VpeService>;
  let dialogRef: jasmine.SpyObj<MatDialogRef<VpeUpscaleDialogComponent>>;

  const placeholder: MediaItem = {
    id: 501,
    gcsUris: [],
    status: JobStatus.PROCESSING,
  };

  const finding = (severity: string, message: string): VpeFindingResponse => ({
    code: 'frame_count',
    severity,
    message,
    remedy: '',
    measured: '',
    required: '',
  });

  const screeningWith = (
    findings: VpeFindingResponse[],
  ): VpeScreeningResponse => ({
    capability_id: 'video_upscale',
    screening: 'not_ruled_out',
    offer: true,
    findings,
  });

  const build = (screening: VpeScreeningResponse) => {
    const data: VpeUpscaleDialogData = {
      workspaceId: 3,
      mediaItemId: 77,
      mediaIndex: 1,
      screening,
    };

    TestBed.configureTestingModule({
      imports: [CommonModule],
      declarations: [VpeUpscaleDialogComponent],
      providers: [
        {provide: MatDialogRef, useValue: dialogRef},
        {provide: MAT_DIALOG_DATA, useValue: data},
        {provide: VpeService, useValue: vpeService},
        {
          provide: MatSnackBar,
          useValue: jasmine.createSpyObj('MatSnackBar', ['open']),
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    });

    fixture = TestBed.createComponent(VpeUpscaleDialogComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  };

  beforeEach(() => {
    vpeService = jasmine.createSpyObj('VpeService', ['startUpscale']);
    dialogRef = jasmine.createSpyObj('MatDialogRef', ['close']);
    vpeService.startUpscale.and.returnValue(of(placeholder));
  });

  it('should create', () => {
    build(screeningWith([]));

    expect(component).toBeTruthy();
  });

  describe('defaults', () => {
    beforeEach(() => build(screeningWith([])));

    it('offers 4K first and starts there', () => {
      expect(component.resolution).toBe(VpeUpscaleResolution.UHD_4K);
      expect(component.resolutionOptions[0].value).toBe(
        VpeUpscaleResolution.UHD_4K,
      );
    });

    it('starts at the sharpness the backend parameter itself defaults to', () => {
      expect(component.sharpness).toBe(1);
    });

    it('is not submitting before anything is clicked', () => {
      expect(component.isSubmitting).toBeFalse();
    });
  });

  describe('warnings', () => {
    it('shows findings that are not ok', () => {
      build(
        screeningWith([
          finding('warning', 'Audio will be re-laid over the master.'),
          finding('ok', 'Frame rate is 24fps.'),
        ]),
      );

      expect(component.warnings.length).toBe(1);
      expect(component.warnings[0].message).toBe(
        'Audio will be re-laid over the master.',
      );
    });

    it('shows nothing when the screening was entirely clean', () => {
      build(screeningWith([finding('ok', 'Frame rate is 24fps.')]));

      expect(component.warnings).toEqual([]);
    });

    /**
     * The screening that got this dialog opened only ever rules out - it
     * never confirms - so a blocking finding that survives to here is still
     * a caveat to show rather than a reason to hide the button.
     */
    it('shows a blocking finding rather than swallowing it', () => {
      build(screeningWith([finding('blocking', 'Frame count unknown.')]));

      expect(component.warnings.length).toBe(1);
    });
  });

  describe('confirm', () => {
    beforeEach(() => build(screeningWith([])));

    it('sends the dialog data and the current selections', () => {
      component.onResolutionChange(VpeUpscaleResolution.HD_1080P);
      component.onSharpnessChange(3);

      component.confirm();

      expect(vpeService.startUpscale).toHaveBeenCalledWith({
        workspaceId: 3,
        mediaItemId: 77,
        mediaIndex: 1,
        resolution: VpeUpscaleResolution.HD_1080P,
        sharpness: 3,
      });
    });

    it('closes with the placeholder row so the caller can track it', () => {
      component.confirm();

      expect(dialogRef.close).toHaveBeenCalledWith(placeholder);
      expect(component.isSubmitting).toBeFalse();
    });

    it('ignores a second click while the first is still in flight', () => {
      // An upscale is billed per segment and runs for minutes, so a
      // double-click must not queue two jobs.
      const pending = new Subject<MediaItem>();
      vpeService.startUpscale.and.returnValue(pending.asObservable());

      component.confirm();
      component.confirm();

      expect(vpeService.startUpscale).toHaveBeenCalledTimes(1);
      expect(component.isSubmitting).toBeTrue();

      pending.next(placeholder);
      pending.complete();
    });

    it('stays open and reports the failure when the job will not start', () => {
      const consoleError = spyOn(console, 'error');
      vpeService.startUpscale.and.returnValue(
        throwError(() => new Error('not allowlisted')),
      );

      component.confirm();

      expect(dialogRef.close).not.toHaveBeenCalled();
      expect(component.isSubmitting).toBeFalse();
      expect(
        consoleError.calls
          .allArgs()
          .some(args => String(args[0]).includes('Start upscale')),
      ).toBeTrue();
    });

    it('can be retried after a failure', () => {
      spyOn(console, 'error');
      vpeService.startUpscale.and.returnValue(
        throwError(() => new Error('not allowlisted')),
      );
      component.confirm();

      vpeService.startUpscale.and.returnValue(of(placeholder));
      component.confirm();

      expect(vpeService.startUpscale).toHaveBeenCalledTimes(2);
      expect(dialogRef.close).toHaveBeenCalledWith(placeholder);
    });
  });
});
