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
import {
  HttpClientTestingModule,
  HttpTestingController,
} from '@angular/common/http/testing';
import {StudioAssistantService} from './studio-assistant.service';
import {environment} from '../../../environments/environment';

describe('StudioAssistantService', () => {
  let service: StudioAssistantService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [StudioAssistantService],
    });
    service = TestBed.inject(StudioAssistantService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  it('toggles drawer state and switches tabs via signals', () => {
    expect(service.isOpen()).toBeFalse();
    service.openDrawer('assistant');
    expect(service.isOpen()).toBeTrue();
    expect(service.activeTab()).toBe('assistant');
    service.closeDrawer();
    expect(service.isOpen()).toBeFalse();
  });

  it('sends questions to /gemini/assistant and appends assistant response', async () => {
    const askPromise = service.askQuestion('How does Omni-Cine work?', {
      currentRoute: '/video',
      activeModel: 'omni-cine',
      activeMode: 'Text to Video',
    });

    const req = httpMock.expectOne(`${environment.backendURL}/gemini/assistant`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body.question).toBe('How does Omni-Cine work?');
    expect(req.request.body.currentRoute).toBe('/video');

    req.flush({
      answer: 'Omni-Cine supports up to 7 reference images.',
      modelUsed: 'gemini-3-flash-preview',
    });

    await askPromise;

    const msgs = service.messages();
    expect(msgs[msgs.length - 1].content).toContain('7 reference images');
    expect(msgs[msgs.length - 1].modelUsed).toBe('gemini-3-flash-preview');
  });
});
