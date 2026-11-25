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

import {
  Component,
  computed,
  OnDestroy,
  OnInit,
  Signal,
  signal,
} from '@angular/core';
import {NavigationEnd, Router} from '@angular/router';
import {Subject} from 'rxjs';
import {filter, takeUntil} from 'rxjs/operators';
import {
  GMP_FEATURES,
  GmpFeatureSpec,
  TAB_GUIDES,
  TabGuideSpec,
} from '../../config/gmp-guide.config';
import {MODEL_CONFIGS} from '../../config/model-config';
import {
  AssistantChatMessage,
  HelpDrawerTab,
  StudioAssistantService,
} from '../../../services/studio-assistant/studio-assistant.service';
import {VideoStateService} from '../../../services/video-state.service';

@Component({
  selector: 'app-studio-help-drawer',
  templateUrl: './studio-help-drawer.component.html',
  styleUrls: ['./studio-help-drawer.component.scss'],
})
export class StudioHelpDrawerComponent implements OnInit, OnDestroy {
  private readonly destroy$ = new Subject<void>();

  readonly currentRoute = signal<string>('/');
  readonly activeVideoModel = signal<string>('');
  readonly activeVideoMode = signal<string>('');
  readonly draftQuestion = signal<string>('');
  readonly selectedGmpInspectorId = signal<string>('omni-cine');

  readonly isOpen: Signal<boolean>;
  readonly activeDrawerTab: Signal<HelpDrawerTab>;
  readonly messages: Signal<AssistantChatMessage[]>;
  readonly isAsking: Signal<boolean>;

  readonly gmpFeatures = GMP_FEATURES;

  readonly activeTabGuide = computed<TabGuideSpec>(() => {
    const url = this.currentRoute();
    const matched = TAB_GUIDES.find(
      g => g.routePrefix !== '/' && url.startsWith(g.routePrefix),
    );
    return (
      matched ||
      TAB_GUIDES.find(g => g.routePrefix === '/') ||
      TAB_GUIDES[0]
    );
  });

  readonly activeModelDisplayName = computed<string>(() => {
    const modelVal = this.activeVideoModel();
    if (!modelVal) {
      return '';
    }
    return (
      MODEL_CONFIGS.find(m => m.value === modelVal)?.viewValue || modelVal
    );
  });

  readonly inspectedGmpFeature = computed<GmpFeatureSpec>(() => {
    const currentVideoModel = this.activeVideoModel();
    if (this.currentRoute().startsWith('/video') && currentVideoModel) {
      const fromActive = GMP_FEATURES.find(
        f => f.modelValue === currentVideoModel,
      );
      if (fromActive) {
        return fromActive;
      }
    }
    const chosenId = this.selectedGmpInspectorId();
    return (
      GMP_FEATURES.find(f => f.id === chosenId) || GMP_FEATURES[0]
    );
  });

  constructor(
    private readonly router: Router,
    private readonly assistantService: StudioAssistantService,
    private readonly videoStateService: VideoStateService,
  ) {
    this.isOpen = this.assistantService.isOpen;
    this.activeDrawerTab = this.assistantService.activeTab;
    this.messages = this.assistantService.messages;
    this.isAsking = this.assistantService.isLoading;
  }

  ngOnInit(): void {
    this.currentRoute.set(this.router.url || '/');

    this.router.events
      .pipe(
        filter((e): e is NavigationEnd => e instanceof NavigationEnd),
        takeUntil(this.destroy$),
      )
      .subscribe(event => {
        this.currentRoute.set(event.urlAfterRedirects || event.url || '/');
      });

    this.videoStateService.state$
      .pipe(takeUntil(this.destroy$))
      .subscribe(state => {
        this.activeVideoModel.set(state.model || '');
        this.activeVideoMode.set(state.mode || '');
        const matchingGmp = GMP_FEATURES.find(f => f.modelValue === state.model);
        if (matchingGmp) {
          this.selectedGmpInspectorId.set(matchingGmp.id);
        }
      });
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  toggleDrawer(): void {
    this.assistantService.toggleDrawer();
  }

  closeDrawer(): void {
    this.assistantService.closeDrawer();
  }

  switchTab(tab: HelpDrawerTab): void {
    this.assistantService.openDrawer(tab);
  }

  selectGmpInspector(event: Event): void {
    const selectEl = event.target as HTMLSelectElement | null;
    if (selectEl?.value) {
      this.selectedGmpInspectorId.set(selectEl.value);
    }
  }

  onQuestionInput(event: Event): void {
    const inputEl = event.target as HTMLTextAreaElement | null;
    this.draftQuestion.set(inputEl?.value || '');
  }

  onEnterKey(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void this.submitQuestion();
    }
  }

  async submitQuestion(prefilled?: string): Promise<void> {
    const text = (prefilled ?? this.draftQuestion()).trim();
    if (!text) {
      return;
    }
    if (!prefilled) {
      this.draftQuestion.set('');
    }
    this.assistantService.openDrawer('assistant');
    await this.assistantService.askQuestion(text, {
      currentRoute: this.currentRoute(),
      activeModel: this.activeVideoModel(),
      activeMode: this.activeVideoMode(),
    });
  }

  clearChat(): void {
    this.assistantService.clearConversation();
  }

  goToGmpHub(): void {
    this.closeDrawer();
    void this.router.navigate(['/gmp']);
  }

  launchInspectedModel(feature: GmpFeatureSpec): void {
    this.closeDrawer();
    if (feature.launchRoute === '/gallery') {
      void this.router.navigate(['/gallery']);
      return;
    }
    if (feature.defaultMode) {
      this.videoStateService.updateState({
        model: feature.modelValue,
        mode: feature.defaultMode,
      });
    }
    void this.router.navigate(['/video'], {
      state: {
        remixState: {
          generationModel: feature.modelValue,
          prompt: feature.samplePrompt || '',
        },
      },
    });
  }
}
