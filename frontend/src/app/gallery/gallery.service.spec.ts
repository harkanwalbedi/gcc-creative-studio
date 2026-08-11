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

import {TestBed} from '@angular/core/testing';
import {HttpClientTestingModule} from '@angular/common/http/testing';

import {GalleryService} from './gallery.service';

describe('GalleryService', () => {
  let service: GalleryService;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
    });
    service = TestBed.inject(GalleryService);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  describe('mapUnifiedItem', () => {
    it('populates duration from the backend durationSeconds alias', () => {
      const result = service.mapUnifiedItem({
        id: 1,
        itemType: 'media_item',
        mimeType: 'video/mp4',
        durationSeconds: 10,
      });

      expect(result.duration).toBe(10);
    });

    it('populates duration from durationSeconds nested in metadata', () => {
      const result = service.mapUnifiedItem({
        id: 2,
        itemType: 'media_item',
        metadata: {mimeType: 'video/mp4', durationSeconds: 6},
      });

      expect(result.duration).toBe(6);
    });

    it('keeps a zero duration rather than treating it as absent', () => {
      const result = service.mapUnifiedItem({
        id: 3,
        itemType: 'media_item',
        durationSeconds: 0,
      });

      expect(result.duration).toBe(0);
    });

    it('still reads the legacy short duration name', () => {
      const result = service.mapUnifiedItem({
        id: 4,
        itemType: 'source_asset',
        duration: 4,
      });

      expect(result.duration).toBe(4);
    });
  });
});
