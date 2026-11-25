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

import {HttpClientTestingModule} from '@angular/common/http/testing';
import {NO_ERRORS_SCHEMA, signal} from '@angular/core';
import {ComponentFixture, TestBed} from '@angular/core/testing';
import {FormsModule} from '@angular/forms';
import {MatDialog} from '@angular/material/dialog';
import {MatIconRegistry} from '@angular/material/icon';
import {MatMenuModule} from '@angular/material/menu';
import {MatSnackBar} from '@angular/material/snack-bar';
import {NoopAnimationsModule} from '@angular/platform-browser/animations';
import {Router} from '@angular/router';
import {of} from 'rxjs';

import {SourceAssetService} from '../common/services/source-asset.service';
import {GalleryService} from '../gallery/gallery.service';
import {SearchService} from '../services/search/search.service';
import {
  DEFAULT_VIDEO_MODEL,
  VideoStateService,
} from '../services/video-state.service';
import {VpeService} from '../services/vpe/vpe.service';
import {WorkspaceStateService} from '../services/workspace/workspace-state.service';
import {VideoComponent} from './video.component';

describe('VideoComponent', () => {
  let component: VideoComponent;
  let fixture: ComponentFixture<VideoComponent>;
  let searchService: jasmine.SpyObj<SearchService>;
  let workspaceStateService: jasmine.SpyObj<WorkspaceStateService>;
  let sourceAssetService: jasmine.SpyObj<SourceAssetService>;
  let videoStateService: jasmine.SpyObj<VideoStateService>;
  let vpeService: jasmine.SpyObj<VpeService>;
  let galleryService: jasmine.SpyObj<GalleryService>;
  let router: jasmine.SpyObj<Router>;

  const defaultVideoState = () => ({
    prompt: '',
    aspectRatio: '16:9',
    resolution: '1K' as const,
    model: DEFAULT_VIDEO_MODEL,
    style: null,
    colorAndTone: null,
    lighting: null,
    numberOfMedia: 1,
    durationSeconds: 8,
    composition: null,
    generateAudio: true,
    negativePrompt: '',
    useBrandGuidelines: false,
    enhancePrompt: false,
    mode: 'Text to Video',
    referenceImages: [],
    referenceImagesType: 'ASSET' as const,
    referenceVideo: null,
    referenceVideos: [],
    referenceAudio: null,
    editSource: null,
    maskSource: null,
    stripSourceAudio: true,
    transformStrength: 0.5,
    seed: null,
    numDiffusionSteps: 20,
    outputCodec: 'h264' as const,
    compressionQuality: 'optimized' as const,
    audioControlMode: null,
    isBlueMeshInput: false,
    seamlessLoop: true,
    tessellateHorizontal: false,
    tessellateVertical: false,
  });

  beforeEach(async () => {
    searchService = jasmine.createSpyObj(
      'SearchService',
      [
        'startVeoGeneration',
        'concatenateVideos',
        'rewritePrompt',
        'getRandomPrompt',
      ],
      {
        activeVideoJob$: of(null),
        videoPrompt: '',
      },
    );
    workspaceStateService = jasmine.createSpyObj('WorkspaceStateService', [
      'getActiveWorkspaceId',
    ]);
    workspaceStateService.getActiveWorkspaceId.and.returnValue(1);

    sourceAssetService = jasmine.createSpyObj('SourceAssetService', [
      'uploadAsset',
    ]);
    videoStateService = jasmine.createSpyObj('VideoStateService', [
      'getState',
      'updateState',
      'resetState',
    ]);
    videoStateService.getState.and.returnValue(defaultVideoState());

    vpeService = jasmine.createSpyObj('VpeService', ['loadConfig'], {
      vpeEnabled: signal(false),
      vpeConfigLoaded: signal(false),
    });
    vpeService.loadConfig.and.returnValue(
      of({enabled: false, capabilities: []}),
    );

    galleryService = jasmine.createSpyObj('GalleryService', [
      'mapUnifiedItem',
      'bulkDelete',
    ]);
    router = jasmine.createSpyObj('Router', [
      'navigate',
      'getCurrentNavigation',
    ]);
    router.getCurrentNavigation.and.returnValue(null);

    const iconRegistryStub = {
      addSvgIcon: jasmine
        .createSpy('addSvgIcon')
        .and.callFake(() => iconRegistryStub),
    };

    await TestBed.configureTestingModule({
      declarations: [VideoComponent],
      imports: [
        HttpClientTestingModule,
        FormsModule,
        MatMenuModule,
        NoopAnimationsModule,
      ],
      providers: [
        {provide: SearchService, useValue: searchService},
        {provide: WorkspaceStateService, useValue: workspaceStateService},
        {provide: SourceAssetService, useValue: sourceAssetService},
        {provide: VideoStateService, useValue: videoStateService},
        {provide: VpeService, useValue: vpeService},
        {provide: GalleryService, useValue: galleryService},
        {provide: Router, useValue: router},
        {provide: MatIconRegistry, useValue: iconRegistryStub},
        {
          provide: MatSnackBar,
          useValue: jasmine.createSpyObj('MatSnackBar', ['open']),
        },
        {
          provide: MatDialog,
          useValue: jasmine.createSpyObj('MatDialog', ['open']),
        },
      ],
      schemas: [NO_ERRORS_SCHEMA],
    }).compileComponents();
  });

  it('should create and hide GMP models when VPE is disabled', () => {
    fixture = TestBed.createComponent(VideoComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();

    expect(component).toBeTruthy();
    expect(
      component.generationModels.every(m => !m.capabilities.requiresVpe),
    ).toBeTrue();
  });

  it('exposes GMP models and restores persisted GMP model when VPE is enabled', () => {
    videoStateService.getState.and.returnValue({
      ...defaultVideoState(),
      model: 'omni-cine',
      outputCodec: 'prores',
      compressionQuality: 'lossless',
      audioControlMode: 'target_audio',
    });
    vpeService.loadConfig.and.returnValue(
      of({enabled: true, capabilities: ['gmp_models']}),
    );

    fixture = TestBed.createComponent(VideoComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();

    expect(
      component.generationModels.some(m => m.value === 'omni-cine'),
    ).toBeTrue();
    expect(component.searchRequest.generationModel).toBe('omni-cine');
    expect(component.outputCodec).toBe('prores');
    expect(component.compressionQuality).toBe('lossless');
    expect(component.audioControlMode).toBe('target_audio');
  });

  it('falls back to DEFAULT_VIDEO_MODEL if a GMP model was persisted while VPE is disabled', () => {
    videoStateService.getState.and.returnValue({
      ...defaultVideoState(),
      model: 'omni-cine',
    });
    vpeService.loadConfig.and.returnValue(
      of({enabled: false, capabilities: []}),
    );

    fixture = TestBed.createComponent(VideoComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();

    expect(component.searchRequest.generationModel).toBe(DEFAULT_VIDEO_MODEL);
  });

  it('includes GMP fields in VeoRequest payload for omni-cine', () => {
    vpeService.loadConfig.and.returnValue(
      of({enabled: true, capabilities: ['gmp_models']}),
    );
    searchService.startVeoGeneration.and.returnValue(
      of({id: 1, gcsUris: []}),
    );

    fixture = TestBed.createComponent(VideoComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();

    const omniCine = component.generationModels.find(
      m => m.value === 'omni-cine',
    )!;
    component.selectModel(omniCine);
    component.searchRequest.prompt = 'Cinematic dolly shot';
    component.onOutputCodecChanged('prores');
    component.onCompressionQualityChanged('optimized');
    component.onAudioControlModeChanged('reference_audio');

    component.searchTerm();

    expect(searchService.startVeoGeneration).toHaveBeenCalled();
    const sent = searchService.startVeoGeneration.calls.mostRecent().args[0];
    expect(sent.generationModel).toBe('omni-cine');
    expect(sent.outputCodec).toBe('prores');
    expect(sent.compressionQuality).toBe('optimized');
    expect(sent.audioControlMode).toBe('reference_audio');
  });
});
