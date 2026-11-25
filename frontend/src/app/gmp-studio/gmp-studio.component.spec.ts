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

import {ComponentFixture, TestBed} from '@angular/core/testing';
import {NO_ERRORS_SCHEMA, signal} from '@angular/core';
import {Router} from '@angular/router';
import {of} from 'rxjs';
import {GmpStudioComponent} from './gmp-studio.component';
import {VpeService} from '../services/vpe/vpe.service';
import {VideoStateService} from '../services/video-state.service';
import {StudioAssistantService} from '../services/studio-assistant/studio-assistant.service';
import {GMP_FEATURES} from '../common/config/gmp-guide.config';

describe('GmpStudioComponent', () => {
  let component: GmpStudioComponent;
  let fixture: ComponentFixture<GmpStudioComponent>;
  let routerSpy: jasmine.SpyObj<Router>;
  let videoStateSpy: jasmine.SpyObj<VideoStateService>;
  let assistantSpy: jasmine.SpyObj<StudioAssistantService>;

  beforeEach(async () => {
    routerSpy = jasmine.createSpyObj('Router', ['navigate']);
    videoStateSpy = jasmine.createSpyObj('VideoStateService', ['updateState']);
    assistantSpy = jasmine.createSpyObj('StudioAssistantService', [
      'openDrawer',
      'askQuestion',
    ]);
    assistantSpy.askQuestion.and.returnValue(Promise.resolve());

    const vpeMock = {
      vpeEnabled: signal(true),
      vpeConfigLoaded: signal(true),
      loadConfig: () => of({enabled: true, capabilities: []}),
    };

    await TestBed.configureTestingModule({
      declarations: [GmpStudioComponent],
      providers: [
        {provide: Router, useValue: routerSpy},
        {provide: VpeService, useValue: vpeMock},
        {provide: VideoStateService, useValue: videoStateSpy},
        {provide: StudioAssistantService, useValue: assistantSpy},
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();

    fixture = TestBed.createComponent(GmpStudioComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('filters GMP features by category signal', () => {
    expect(component.filteredFeatures().length).toBe(GMP_FEATURES.length);
    component.selectCategory('upscale');
    expect(
      component.filteredFeatures().every(f => f.category === 'upscale'),
    ).toBeTrue();
  });

  it('launches a GMP video model into /video with remixState', () => {
    const omniCine = GMP_FEATURES.find(f => f.id === 'omni-cine')!;
    component.launchFeature(omniCine);
    expect(videoStateSpy.updateState).toHaveBeenCalled();
    expect(routerSpy.navigate).toHaveBeenCalledWith(
      ['/video'],
      jasmine.objectContaining({state: jasmine.any(Object)}),
    );
  });
});
