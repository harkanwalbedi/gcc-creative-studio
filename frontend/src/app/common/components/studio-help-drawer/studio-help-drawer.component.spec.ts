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
import {RouterTestingModule} from '@angular/router/testing';
import {BehaviorSubject} from 'rxjs';
import {StudioHelpDrawerComponent} from './studio-help-drawer.component';
import {
  AssistantChatMessage,
  HelpDrawerTab,
  StudioAssistantService,
} from '../../../services/studio-assistant/studio-assistant.service';
import {VideoStateService} from '../../../services/video-state.service';

describe('StudioHelpDrawerComponent', () => {
  let component: StudioHelpDrawerComponent;
  let fixture: ComponentFixture<StudioHelpDrawerComponent>;
  let isOpenSig = signal(false);
  let activeTabSig = signal<HelpDrawerTab>('guide');

  beforeEach(async () => {
    isOpenSig = signal(false);
    activeTabSig = signal<HelpDrawerTab>('guide');

    const assistantMock = {
      isOpen: isOpenSig,
      activeTab: activeTabSig,
      messages: signal<AssistantChatMessage[]>([]),
      isLoading: signal(false),
      toggleDrawer: jasmine
        .createSpy('toggleDrawer')
        .and.callFake(() => isOpenSig.update(v => !v)),
      closeDrawer: jasmine
        .createSpy('closeDrawer')
        .and.callFake(() => isOpenSig.set(false)),
      openDrawer: jasmine
        .createSpy('openDrawer')
        .and.callFake((t: HelpDrawerTab) => {
          activeTabSig.set(t);
          isOpenSig.set(true);
        }),
      askQuestion: jasmine
        .createSpy('askQuestion')
        .and.returnValue(Promise.resolve()),
      clearConversation: jasmine.createSpy('clearConversation'),
    };

    const videoStateMock = {
      state$: new BehaviorSubject({
        model: 'omni-perf',
        mode: 'Ingredients to Video',
      }),
      updateState: jasmine.createSpy('updateState'),
    };

    await TestBed.configureTestingModule({
      imports: [RouterTestingModule],
      declarations: [StudioHelpDrawerComponent],
      providers: [
        {provide: StudioAssistantService, useValue: assistantMock},
        {provide: VideoStateService, useValue: videoStateMock},
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();

    fixture = TestBed.createComponent(StudioHelpDrawerComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('tracks active GMP model from VideoStateService', () => {
    expect(component.activeVideoModel()).toBe('omni-perf');
    expect(component.inspectedGmpFeature().id).toBe('omni-perf');
  });

  it('toggles the help drawer open and closed', () => {
    expect(component.isOpen()).toBeFalse();
    component.toggleDrawer();
    expect(component.isOpen()).toBeTrue();
  });
});
