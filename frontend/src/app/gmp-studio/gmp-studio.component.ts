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

import {Component, computed, OnInit, Signal, signal} from '@angular/core';
import {Router} from '@angular/router';
import {
  GMP_FEATURES,
  GmpCategory,
  GmpFeatureSpec,
} from '../common/config/gmp-guide.config';
import {VpeService} from '../services/vpe/vpe.service';
import {VideoStateService} from '../services/video-state.service';
import {StudioAssistantService} from '../services/studio-assistant/studio-assistant.service';

@Component({
  selector: 'app-gmp-studio',
  templateUrl: './gmp-studio.component.html',
  styleUrls: ['./gmp-studio.component.scss'],
})
export class GmpStudioComponent implements OnInit {
  readonly selectedCategory = signal<GmpCategory>('all');
  readonly searchQuery = signal<string>('');
  readonly allFeatures = GMP_FEATURES;

  readonly categoryOptions: {value: GmpCategory; label: string}[] = [
    {value: 'all', label: 'All GMP Features (7)'},
    {value: 'cinematic', label: 'Cinema, Performance & Lip Sync'},
    {value: 'editing', label: 'Transform, Keyframes & Rigging'},
    {value: 'upscale', label: '4K Upscale & Seamless Textures'},
  ];

  readonly filteredFeatures = computed(() => {
    const category = this.selectedCategory();
    const query = this.searchQuery().trim().toLowerCase();
    return this.allFeatures.filter(feature => {
      const matchesCategory =
        category === 'all' || feature.category === category;
      if (!matchesCategory) {
        return false;
      }
      if (!query) {
        return true;
      }
      const searchable = [
        feature.title,
        feature.badge,
        feature.summary,
        ...feature.supportedModes,
        ...feature.keyConfigs,
      ]
        .join(' ')
        .toLowerCase();
      return searchable.includes(query);
    });
  });

  readonly vpeEnabled: Signal<boolean>;
  readonly vpeConfigLoaded: Signal<boolean>;

  constructor(
    private readonly router: Router,
    private readonly vpeService: VpeService,
    private readonly videoStateService: VideoStateService,
    private readonly assistantService: StudioAssistantService,
  ) {
    this.vpeEnabled = this.vpeService.vpeEnabled;
    this.vpeConfigLoaded = this.vpeService.vpeConfigLoaded;
  }

  ngOnInit(): void {
    this.vpeService.loadConfig().subscribe();
  }

  selectCategory(category: GmpCategory): void {
    this.selectedCategory.set(category);
  }

  onSearchInput(event: Event): void {
    const target = event.target as HTMLInputElement | null;
    this.searchQuery.set(target?.value || '');
  }

  launchFeature(feature: GmpFeatureSpec): void {
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

  askAboutFeature(feature: GmpFeatureSpec): void {
    this.assistantService.openDrawer('assistant');
    void this.assistantService.askQuestion(
      `How do I configure and get the best results from ${feature.title} (${feature.modelValue})?`,
      {
        currentRoute: '/gmp',
        activeModel: feature.modelValue,
        activeMode: feature.defaultMode || '',
      },
    );
  }

  openHelpGuide(): void {
    this.assistantService.openDrawer('guide');
  }
}
