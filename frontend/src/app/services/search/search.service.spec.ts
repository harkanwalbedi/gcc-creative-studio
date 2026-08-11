/**
 * Copyright 2025 Google LLC
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

import {SearchService} from './search.service';
import {environment} from '../../../environments/environment';
import {JobStatus, MediaItem} from '../../common/models/media-item.model';
import {ImagenRequest, VeoRequest} from '../../common/models/search.model';

describe('SearchService', () => {
  let service: SearchService;
  let httpMock: HttpTestingController;

  const JOB_ID = 7;
  const itemUrl = `${environment.backendURL}/gallery/item/${JOB_ID}`;

  const jobWithStatus = (status: JobStatus): MediaItem => ({
    id: JOB_ID,
    gcsUris: [],
    status,
  });

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
    service = TestBed.inject(SearchService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  describe('video polling', () => {
    /**
     * Starts a video job and answers its first poll with the given status.
     * Must be called from inside fakeAsync.
     */
    const startJobAndPollOnce = (status: JobStatus) => {
      service.startVeoGeneration({} as VeoRequest).subscribe();
      httpMock
        .expectOne(`${environment.backendURL}/videos/generate-videos`)
        .flush(jobWithStatus(JobStatus.PROCESSING));

      tick(5000);
      httpMock.expectOne(itemUrl).flush(jobWithStatus(status));
    };

    it('stops polling once the job has been stopped', fakeAsync(() => {
      startJobAndPollOnce(JobStatus.STOPPED);

      tick(15000);

      httpMock.expectNone(itemUrl);
    }));

    it('does not report a stopped job as a generation failure', fakeAsync(() => {
      const consoleError = spyOn(console, 'error');

      startJobAndPollOnce(JobStatus.STOPPED);

      expect(
        consoleError.calls
          .allArgs()
          .some(args => String(args[0]).includes('Video generation failed')),
      ).toBeFalse();
    }));

    it('keeps polling while the job is still processing', fakeAsync(() => {
      startJobAndPollOnce(JobStatus.PROCESSING);

      tick(15000);

      httpMock.expectOne(itemUrl).flush(jobWithStatus(JobStatus.COMPLETED));
    }));

    it('stops polling when the active job is cleared', fakeAsync(() => {
      startJobAndPollOnce(JobStatus.PROCESSING);

      service.clearActiveVideoJob();
      tick(15000);

      httpMock.expectNone(itemUrl);
    }));
  });

  describe('image polling', () => {
    it('stops polling once the job has been stopped', fakeAsync(() => {
      service.startImagenGeneration({} as ImagenRequest).subscribe();
      httpMock
        .expectOne(`${environment.backendURL}/images/generate-images`)
        .flush(jobWithStatus(JobStatus.PROCESSING));

      tick(2000);
      httpMock.expectOne(itemUrl).flush(jobWithStatus(JobStatus.STOPPED));

      tick(5000);

      httpMock.expectNone(itemUrl);
    }));
  });
});
