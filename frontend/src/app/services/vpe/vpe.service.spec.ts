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

import {TestBed, fakeAsync, tick} from '@angular/core/testing';
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import {MatSnackBar} from '@angular/material/snack-bar';

import {VpeService} from './vpe.service';
import {environment} from '../../../environments/environment';
import {JobStatus, MediaItem} from '../../common/models/media-item.model';
import {
  UpscaleVideoDto,
  VpeScreeningResponse,
  VpeUpscaleResolution,
} from '../../common/models/vpe.model';

describe('VpeService', () => {
  let service: VpeService;
  let httpMock: HttpTestingController;

  const JOB_ID = 42;
  const upscaleUrl = `${environment.backendURL}/videos/vpe/upscale`;
  const itemUrl = `${environment.backendURL}/gallery/item/${JOB_ID}`;

  const jobWithStatus = (
    status: JobStatus,
    errorMessage?: string,
  ): MediaItem => ({
    id: JOB_ID,
    gcsUris: [],
    status,
    ...(errorMessage ? {errorMessage} : {}),
  });

  const payload: UpscaleVideoDto = {
    workspaceId: 1,
    mediaItemId: 99,
    mediaIndex: 0,
    resolution: VpeUpscaleResolution.UHD_4K,
    sharpness: 1,
  };

  /**
   * `handleErrorSnackbar` resolves NotificationService through the module
   * global `AppInjector`, which the first spec to call `setAppInjector` owns
   * for the whole karma run - so asserting on the notification itself would
   * make this suite order-dependent. It does unconditionally log its context
   * string first, though, which is a stable signal for "the failure branch
   * ran" and the same one search.service.spec.ts leans on.
   */
  const loggedUpscaleFailure = (consoleError: jasmine.Spy): boolean =>
    consoleError.calls
      .allArgs()
      .some(args => String(args[0]).includes('Upscale failed'));

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        {
          provide: MatSnackBar,
          useValue: jasmine.createSpyObj('MatSnackBar', ['open']),
        },
      ],
    });
    service = TestBed.inject(VpeService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  describe('screenForUpscale', () => {
    it('asks the screening route for that gallery row', () => {
      const screening: VpeScreeningResponse = {
        capability_id: 'video_upscale',
        screening: 'not_ruled_out',
        offer: true,
        findings: [],
      };
      let received: VpeScreeningResponse | undefined;

      service.screenForUpscale(7).subscribe(value => (received = value));

      const request = httpMock.expectOne(
        `${environment.backendURL}/videos/vpe/screening/7`,
      );
      expect(request.request.method).toBe('GET');
      request.flush(screening);

      expect(received).toEqual(screening);
    });

    it('does not start polling - screening queues no job', fakeAsync(() => {
      service.screenForUpscale(7).subscribe();
      httpMock
        .expectOne(`${environment.backendURL}/videos/vpe/screening/7`)
        .flush({
          capability_id: 'video_upscale',
          screening: 'not_ruled_out',
          offer: true,
          findings: [],
        });

      tick(20000);

      httpMock.expectNone(itemUrl);
    }));
  });

  describe('startUpscale', () => {
    it('posts the payload to the upscale route', () => {
      service.startUpscale(payload).subscribe();

      const request = httpMock.expectOne(upscaleUrl);
      expect(request.request.method).toBe('POST');
      expect(request.request.body).toEqual(payload);
      request.flush(jobWithStatus(JobStatus.PROCESSING));

      service.clearActiveUpscaleJob();
    });

    it('publishes the placeholder row before the first poll', fakeAsync(() => {
      const seen: (MediaItem | null)[] = [];
      service.activeUpscaleJob$.subscribe(item => seen.push(item));

      service.startUpscale(payload).subscribe();
      httpMock.expectOne(upscaleUrl).flush(jobWithStatus(JobStatus.PROCESSING));

      // Null on subscribe, then the placeholder - and nothing polled yet.
      expect(seen.length).toBe(2);
      expect(seen[0]).toBeNull();
      expect(seen[1]?.status).toBe(JobStatus.PROCESSING);
      httpMock.expectNone(itemUrl);

      service.clearActiveUpscaleJob();
    }));
  });

  describe('polling', () => {
    /**
     * Queues an upscale and answers its first poll with the given status.
     * Must be called from inside fakeAsync.
     */
    const startJobAndPollOnce = (status: JobStatus, errorMessage?: string) => {
      service.startUpscale(payload).subscribe();
      httpMock.expectOne(upscaleUrl).flush(jobWithStatus(JobStatus.PROCESSING));

      tick(5000);
      httpMock.expectOne(itemUrl).flush(jobWithStatus(status, errorMessage));
    };

    it('waits 5s before the first poll', fakeAsync(() => {
      service.startUpscale(payload).subscribe();
      httpMock.expectOne(upscaleUrl).flush(jobWithStatus(JobStatus.PROCESSING));

      tick(4999);
      httpMock.expectNone(itemUrl);

      tick(1);
      httpMock.expectOne(itemUrl).flush(jobWithStatus(JobStatus.COMPLETED));
    }));

    it('keeps polling on the slow cadence while processing', fakeAsync(() => {
      startJobAndPollOnce(JobStatus.PROCESSING);

      // An upscale runs for minutes, so this must not fall back to the
      // faster image cadence between polls.
      tick(14999);
      httpMock.expectNone(itemUrl);

      tick(1);
      httpMock.expectOne(itemUrl).flush(jobWithStatus(JobStatus.COMPLETED));
    }));

    it('publishes every intermediate poll, not just the last', fakeAsync(() => {
      const seen: (MediaItem | null)[] = [];
      service.activeUpscaleJob$.subscribe(item => seen.push(item));

      startJobAndPollOnce(JobStatus.PROCESSING);
      tick(15000);
      httpMock.expectOne(itemUrl).flush(jobWithStatus(JobStatus.COMPLETED));

      expect(seen.map(item => item?.status ?? null)).toEqual([
        null,
        JobStatus.PROCESSING,
        JobStatus.PROCESSING,
        JobStatus.COMPLETED,
      ]);
    }));

    it('stops polling once the job has completed', fakeAsync(() => {
      startJobAndPollOnce(JobStatus.COMPLETED);

      tick(15000);

      httpMock.expectNone(itemUrl);
    }));

    it('stops polling once the job has been stopped', fakeAsync(() => {
      startJobAndPollOnce(JobStatus.STOPPED);

      tick(15000);

      httpMock.expectNone(itemUrl);
    }));

    it('stops polling once the job has failed', fakeAsync(() => {
      startJobAndPollOnce(JobStatus.FAILED, 'segment 1 of 2 came back empty');

      tick(15000);

      httpMock.expectNone(itemUrl);
    }));

    it('reports a failed job as an upscale failure', fakeAsync(() => {
      const consoleError = spyOn(console, 'error');

      startJobAndPollOnce(JobStatus.FAILED, 'segment 1 of 2 came back empty');

      expect(loggedUpscaleFailure(consoleError)).toBeTrue();
    }));

    it('does not report a stopped job as an upscale failure', fakeAsync(() => {
      const consoleError = spyOn(console, 'error');

      startJobAndPollOnce(JobStatus.STOPPED);

      expect(loggedUpscaleFailure(consoleError)).toBeFalse();
    }));

    it('does not report a completed job as an upscale failure', fakeAsync(() => {
      const consoleError = spyOn(console, 'error');

      startJobAndPollOnce(JobStatus.COMPLETED);

      expect(loggedUpscaleFailure(consoleError)).toBeFalse();
    }));

    it('stops polling when the active job is cleared', fakeAsync(() => {
      startJobAndPollOnce(JobStatus.PROCESSING);

      service.clearActiveUpscaleJob();
      tick(15000);

      httpMock.expectNone(itemUrl);
    }));

    it('publishes null when the active job is cleared', fakeAsync(() => {
      startJobAndPollOnce(JobStatus.PROCESSING);

      let latest: MediaItem | null | undefined;
      service.activeUpscaleJob$.subscribe(item => (latest = item));
      expect(latest?.status).toBe(JobStatus.PROCESSING);

      service.clearActiveUpscaleJob();

      expect(latest).toBeNull();
    }));

    it('gives up polling after a failed request', fakeAsync(() => {
      spyOn(console, 'error');
      service.startUpscale(payload).subscribe();
      httpMock.expectOne(upscaleUrl).flush(jobWithStatus(JobStatus.PROCESSING));

      tick(5000);
      httpMock
        .expectOne(itemUrl)
        .flush('gone', {status: 500, statusText: 'Server Error'});

      tick(15000);

      httpMock.expectNone(itemUrl);
    }));

    it('replaces the previous poll when a second upscale starts', fakeAsync(() => {
      startJobAndPollOnce(JobStatus.PROCESSING);

      service.startUpscale(payload).subscribe();
      httpMock.expectOne(upscaleUrl).flush(jobWithStatus(JobStatus.PROCESSING));

      // One poll, not two - the first subscription must have been dropped
      // rather than left running alongside the new one.
      tick(5000);
      httpMock.expectOne(itemUrl).flush(jobWithStatus(JobStatus.COMPLETED));

      tick(15000);
      httpMock.expectNone(itemUrl);
    }));
  });

  describe('getUpscaleMediaItem', () => {
    it('reads the row back off the gallery route', () => {
      let received: MediaItem | undefined;

      service.getUpscaleMediaItem(JOB_ID).subscribe(item => (received = item));

      const request = httpMock.expectOne(itemUrl);
      expect(request.request.method).toBe('GET');
      request.flush(jobWithStatus(JobStatus.COMPLETED));

      expect(received?.status).toBe(JobStatus.COMPLETED);
    });
  });
});
