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

import {SecurityContext} from '@angular/core';
import {TestBed} from '@angular/core/testing';
import {DomSanitizer} from '@angular/platform-browser';
import {SafeUrlPipe} from './safe-url.pipe';

describe('SafeUrlPipe', () => {
  let pipe: SafeUrlPipe;
  let sanitizer: DomSanitizer;

  beforeEach(() => {
    TestBed.configureTestingModule({});
    sanitizer = TestBed.inject(DomSanitizer);
    pipe = new SafeUrlPipe(sanitizer);
  });

  it('should create an instance', () => {
    expect(pipe).toBeTruthy();
  });

  it('should transform valid YouTube URLs into sanitized embed URLs', () => {
    const safeUrl = pipe.transform(
      'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
      'youtube',
    );
    expect(sanitizer.sanitize(SecurityContext.RESOURCE_URL, safeUrl)).toBe(
      'https://www.youtube.com/embed/dQw4w9WgXcQ',
    );
  });

  it('should return empty sanitized URL for unsupported types or invalid URLs', () => {
    const unsupported = pipe.transform(
      'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
      'other',
    );
    expect(sanitizer.sanitize(SecurityContext.RESOURCE_URL, unsupported)).toBe(
      '',
    );

    const empty = pipe.transform(null, 'youtube');
    expect(sanitizer.sanitize(SecurityContext.RESOURCE_URL, empty)).toBe('');
  });
});
