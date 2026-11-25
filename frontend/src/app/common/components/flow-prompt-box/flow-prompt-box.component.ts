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

import {
  Component,
  EventEmitter,
  Input,
  Output,
  signal,
  HostListener,
  ViewChild,
  ElementRef,
  computed,
  OnInit,
  OnDestroy,
  inject,
} from '@angular/core';
import {MatSnackBar} from '@angular/material/snack-bar';
import {
  KeyframeSlot,
  ReferenceImage,
  ReferenceVideo,
} from '../../models/search.model';
import {
  ASPECT_RATIO_AUTO,
  GenerationModelConfig,
  getSupportedModelDurations,
} from '../../config/model-config';
import {
  extractYouTubeVideoId,
  getYouTubeThumbnailUrl,
} from '../../../utils/youtube.utils';
import {MatIconModule} from '@angular/material/icon';
import {CommonModule} from '@angular/common';
import {FormsModule} from '@angular/forms';
import {MatButtonModule} from '@angular/material/button';
import {MatMenuModule} from '@angular/material/menu';
import {MatTooltipModule} from '@angular/material/tooltip';

export type NumPos = 1 | 2;

@Component({
  standalone: true,
  selector: 'app-flow-prompt-box',
  templateUrl: './flow-prompt-box.component.html',
  styleUrls: ['./flow-prompt-box.component.scss'],
  imports: [
    CommonModule,
    FormsModule,
    MatIconModule,
    MatButtonModule,
    MatMenuModule,
    MatTooltipModule,
  ],
})
export class FlowPromptBoxComponent implements OnInit, OnDestroy {
  private snackBar = inject(MatSnackBar);

  @Input() searchRequest!: any; // Keep for now, but prefer individual inputs
  @Input() isLoading = false;
  @Input() prompt = '';
  @Input() aspectRatio = '16:9';
  @Input() outputs = 4;
  @Input() aspectRatioOptions: {
    value: string;
    viewValue: string;
    disabled: boolean;
    icon?: string;
  }[] = [];
  @Input() modes: {value: string; icon: string; label: string}[] = [];

  // --- setter inputs ---
  private _generationModels: GenerationModelConfig[] = [];
  private generationModelsSignal = signal<GenerationModelConfig[]>([]);
  @Input() set generationModels(val: GenerationModelConfig[]) {
    this._generationModels = val || [];
    this.generationModelsSignal.set(val || []);
    this.updateSupportedResolutions();
    this.syncSelectedDuration();
  }
  get generationModels(): GenerationModelConfig[] {
    return this._generationModels;
  }
  private _selectedGenerationModel = '';
  private selectedGenerationModelSignal = signal<string>('');
  @Input() set selectedGenerationModel(val: string) {
    this._selectedGenerationModel = val;
    this.selectedGenerationModelSignal.set(val);
    this.updateSupportedResolutions();
    this.syncSelectedDuration();
  }
  get selectedGenerationModel(): string {
    return this._selectedGenerationModel;
  }

  @Input() set mode(val: string) {
    if (val) {
      const oldMode = this.selectedMode();
      this.selectedMode.set(val);
      this.updateSupportedResolutions(
        undefined,
        oldMode !== '' && oldMode !== val,
      );
      this.syncSelectedDuration();
    }
  }
  get mode(): string {
    return this.selectedMode();
  }

  @Input() set resolution(
    val: '360p' | '1K' | '2K' | '4K' | '720p' | '1080p' | undefined,
  ) {
    if (val) {
      const normalized = val === '720p' ? '1K' : val === '1080p' ? '2K' : val;
      this.selectedResolution.set(normalized as '360p' | '1K' | '2K' | '4K');
      this.syncSelectedDuration();
    }
  }
  @Input() set duration(val: number | undefined) {
    if (val) {
      this.selectedDuration.set(val);
    }
  }

  // --- outputs ---
  @Output() generateClicked = new EventEmitter<void>();
  @Output() rewriteClicked = new EventEmitter<void>();
  @Output() modelSelected = new EventEmitter<any>();
  @Output() promptChanged = new EventEmitter<string>();
  @Output() resolutionChanged = new EventEmitter<
    '360p' | '1K' | '2K' | '4K' | '720p' | '1080p'
  >();
  @Output() durationChanged = new EventEmitter<number>();
  @Output() aspectRatioChanged = new EventEmitter<string>();
  @Output() outputsChanged = new EventEmitter<number>();
  @Output() modeChanged = new EventEmitter<string>();
  @Output() openImageSelector = new EventEmitter<NumPos>();
  @Output() editImage = new EventEmitter<{num: NumPos}>();
  @Output() clearImage = new EventEmitter<{num: NumPos; event: Event}>();
  @Output() openImageSelectorForReference = new EventEmitter<void>();
  @Output() onReferenceImageDrop = new EventEmitter<DragEvent>();
  @Output() editReferenceImage = new EventEmitter<{
    index: number;
    ref: ReferenceImage;
  }>();
  @Output() clearReferenceImage = new EventEmitter<{
    index: number;
    event: Event;
  }>();
  @Output() toggleReferenceImagesType = new EventEmitter<boolean>();
  @Output() openVideoSelectorForReference = new EventEmitter<void>();
  @Output() clearReferenceVideo = new EventEmitter<
    {index: number; event: Event} | Event
  >();
  @Output() openVideoUrlInputForReference = new EventEmitter<void>();
  @Output() clearExternalUrl = new EventEmitter<void>();
  @Output() openVideoSelectorForEdit = new EventEmitter<void>();
  @Output() clearEditSource = new EventEmitter<Event>();
  @Output() openVideoSelectorForMask = new EventEmitter<void>();
  @Output() clearMaskSource = new EventEmitter<Event>();
  @Output() stripSourceAudioChanged = new EventEmitter<boolean>();
  @Output() resetClicked = new EventEmitter<void>();
  @Output() openAudioSelectorForReference = new EventEmitter<void>();
  @Output() clearReferenceAudio = new EventEmitter<Event>();
  @Output() temperatureChanged = new EventEmitter<number | null>();
  @Output() transformStrengthChanged = new EventEmitter<number>();
  @Output() seedChanged = new EventEmitter<number | null>();
  @Output() numDiffusionStepsChanged = new EventEmitter<number>();
  @Output() addKeyframeClicked = new EventEmitter<void>();
  @Output() removeKeyframeClicked = new EventEmitter<number>();
  @Output() keyframeFrameNumberChanged = new EventEmitter<{
    index: number;
    frameNumber: number;
  }>();
  @Output() outputCodecChanged = new EventEmitter<
    'h264' | 'prores' | 'dnxhr'
  >();
  @Output() compressionQualityChanged = new EventEmitter<
    'optimized' | 'lossless'
  >();
  @Output() audioControlModeChanged = new EventEmitter<
    | 'generate'
    | 'preserve_video_audio'
    | 'target_audio'
    | 'reference_audio'
    | null
  >();
  @Output() isBlueMeshInputChanged = new EventEmitter<boolean>();
  @Output() seamlessLoopChanged = new EventEmitter<boolean>();
  @Output() tessellateHorizontalChanged = new EventEmitter<boolean>();
  @Output() tessellateVerticalChanged = new EventEmitter<boolean>();

  readonly validFrameNumbers: number[] = [
    8, 16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 128, 136, 144,
    152, 160, 168, 176, 184,
  ];

  readonly deliveryFormatOptions: {
    key: string;
    label: string;
    codec: 'h264' | 'prores' | 'dnxhr';
    quality: 'optimized' | 'lossless';
  }[] = [
    {
      key: 'h264_optimized',
      label: 'Web MP4 (H.264)',
      codec: 'h264',
      quality: 'optimized',
    },
    {
      key: 'prores_optimized',
      label: 'Apple ProRes 422 (.mov)',
      codec: 'prores',
      quality: 'optimized',
    },
    {
      key: 'prores_lossless',
      label: 'Apple ProRes 4444 Lossless (.mov)',
      codec: 'prores',
      quality: 'lossless',
    },
    {
      key: 'dnxhr_optimized',
      label: 'Avid DNxHR HQ (.mxf)',
      codec: 'dnxhr',
      quality: 'optimized',
    },
  ];

  @Input() image1Preview: string | null = null;
  private _image2Preview: string | null = null;
  private image2PreviewSignal = signal<string | null>(null);
  @Input() set image2Preview(val: string | null) {
    this._image2Preview = val ?? null;
    this.image2PreviewSignal.set(val ?? null);
    this.syncSelectedDuration();
  }
  get image2Preview(): string | null {
    return this._image2Preview;
  }

  @Input() conditioningKeyframes: KeyframeSlot[] = [];
  private _referenceImages: ReferenceImage[] = [];
  private referenceImagesSignal = signal<ReferenceImage[]>([]);
  @Input() set referenceImages(val: ReferenceImage[]) {
    this._referenceImages = val || [];
    this.referenceImagesSignal.set(val || []);
    this.syncSelectedDuration();
  }
  get referenceImages(): ReferenceImage[] {
    return this._referenceImages;
  }

  @Input() referenceImagesType: 'ASSET' | 'STYLE' = 'ASSET';
  @Input() referenceVideo: any | null = null;
  @Input() referenceVideos: any[] = [];

  private _externalUrl: string | null = null;
  private externalUrlSignal = signal<string | null>(null);
  @Input() set externalUrl(val: string | null) {
    this._externalUrl = val ?? null;
    this.externalUrlSignal.set(val ?? null);
  }
  get externalUrl(): string | null {
    return this._externalUrl;
  }

  get effectiveReferenceVideos(): any[] {
    if (this.referenceVideos && this.referenceVideos.length > 0) {
      return this.referenceVideos;
    }
    return this.referenceVideo ? [this.referenceVideo] : [];
  }
  /** The clip being modified in Edit Video mode. */
  @Input() editSource: ReferenceVideo | null = null;
  /** Optional inpainting mask video for localized edits. */
  @Input() maskSource: ReferenceVideo | null = null;
  @Input() stripSourceAudio = true;
  @Input() referenceAudio: any | null = null;
  /** null means "use the model's default" rather than a chosen value. */
  @Input() temperature: number | null = null;
  @Input() transformStrength = 0.5;
  @Input() seed: number | null = null;
  @Input() numDiffusionSteps = 20;
  @Input() outputCodec: 'h264' | 'prores' | 'dnxhr' = 'h264';
  @Input() compressionQuality: 'optimized' | 'lossless' = 'optimized';
  @Input() audioControlMode:
    | 'generate'
    | 'preserve_video_audio'
    | 'target_audio'
    | 'reference_audio'
    | null = null;
  @Input() isBlueMeshInput = false;
  @Input() seamlessLoop = true;
  @Input() tessellateHorizontal = false;
  @Input() tessellateVertical = false;

  @ViewChild('promptInput') promptInput?: ElementRef<HTMLTextAreaElement>;
  @ViewChild('modeTrigger') modeTrigger!: ElementRef;
  @ViewChild('modeMenu') modeMenu!: ElementRef;
  @ViewChild('settingsTrigger') settingsTrigger!: ElementRef;
  @ViewChild('settingsMenu') settingsMenu!: ElementRef;

  private resolutionTimeoutId: ReturnType<typeof setTimeout> | null = null;
  private durationTimeoutId: ReturnType<typeof setTimeout> | null = null;

  constructor(private eRef: ElementRef) {}

  @HostListener('document:click', ['$event'])
  clickout(event: any) {
    // Close Mode Menu if clicked outside trigger and menu
    if (
      this.isModeMenuOpen() &&
      this.modeTrigger &&
      !this.modeTrigger.nativeElement.contains(event.target) &&
      (!this.modeMenu || !this.modeMenu.nativeElement.contains(event.target))
    ) {
      this.isModeMenuOpen.set(false);
    }

    // Close Settings Menu if clicked outside trigger and menu
    if (
      this.isSettingsMenuOpen() &&
      this.settingsTrigger &&
      !this.settingsTrigger.nativeElement.contains(event.target) &&
      (!this.settingsMenu ||
        !this.settingsMenu.nativeElement.contains(event.target))
    ) {
      this.isSettingsMenuOpen.set(false);
    }
  }

  // All possible resolutions
  readonly ALL_RESOLUTIONS: ('360p' | '1K' | '2K' | '4K')[] = [
    '360p',
    '1K',
    '2K',
    '4K',
  ];

  getResolutionLabel(resolution: '360p' | '1K' | '2K' | '4K'): string {
    const isVideo = this.modes.some(m => m.value.includes('Video'));
    if (isVideo) {
      switch (resolution) {
        case '360p':
          return '360p';
        case '1K':
          return '720p';
        case '2K':
          return '1080p';
        case '4K':
          return '4K';
        default:
          return resolution;
      }
    }
    return resolution;
  }

  getAspectRatioIcon(aspectRatioStr: string): string {
    const matched = this.aspectRatioOptions.find(
      opt =>
        opt.value === aspectRatioStr ||
        opt.viewValue === aspectRatioStr ||
        aspectRatioStr.startsWith(opt.value + ' '),
    );
    if (matched?.icon) {
      return matched.icon;
    }
    const normalized = (aspectRatioStr || '').toLowerCase();
    if (normalized.startsWith(ASPECT_RATIO_AUTO)) {
      return 'hdr_auto';
    }
    if (normalized.includes('1:1')) {
      return 'crop_square';
    }
    if (
      normalized.includes('16:9') ||
      normalized.includes('4:3') ||
      normalized.includes('3:2') ||
      normalized.includes('5:4') ||
      normalized.includes('21:9') ||
      normalized.includes('4:1') ||
      normalized.includes('8:1')
    ) {
      return 'crop_landscape';
    }
    return 'crop_portrait';
  }

  // --- Logic moved from VideoComponent ---

  promptText = signal<string>('');

  // Menu open/close states
  isModeMenuOpen = signal<boolean>(false);
  isSettingsMenuOpen = signal<boolean>(false);
  isSettingsDropdownOpen = signal<
    | 'aspect'
    | 'outputs'
    | 'model'
    | 'resolution'
    | 'duration'
    | 'deliveryFormat'
    | 'audioControl'
    | null
  >(null);
  selectedMode = signal<string>('Text to Video');
  selectedPreset = signal<string>('');
  selectedResolution = signal<'360p' | '1K' | '2K' | '4K'>('1K');
  selectedDuration = signal<number>(4);

  supportedResolutions = signal<('360p' | '1K' | '2K' | '4K')[]>([]);

  // --- Computed Values ---
  isExtendVideo = computed(() => this.selectedMode() === 'Extend Video');
  isIngredientsToImage = computed(
    () => this.selectedMode() === 'Ingredients to Image',
  );
  isVideoToImage = computed(() => this.selectedMode() === 'Video to Image');
  isTextToVideo = computed(() => this.selectedMode() === 'Text to Video');
  isEditVideo = computed(() => this.selectedMode() === 'Edit Video');
  youtubeVideoId = computed(() =>
    extractYouTubeVideoId(this.externalUrlSignal()),
  );
  youtubeThumbnailUrl = computed(() =>
    getYouTubeThumbnailUrl(this.youtubeVideoId()),
  );
  availableDurations = computed(() =>
    getSupportedModelDurations(
      this.getSelectedModelObject(),
      this.selectedMode(),
      this.selectedResolution(),
      !!this.image2PreviewSignal(),
      this.referenceImagesSignal().length > 0,
    ),
  );
  hasResolutionOptions = computed(() => this.supportedResolutions().length > 0);
  hasDurationOptions = computed(
    () =>
      this.selectedMode() !== 'Edit Video' &&
      this.availableDurations().length > 0,
  );
  hasAspectRatioOptions = computed(() => this.selectedMode() !== 'Edit Video');

  // --- Lifecycle Hooks ---
  ngOnInit(): void {
    this.updateSupportedResolutions();
    this.syncSelectedDuration();
  }

  ngOnDestroy(): void {
    if (this.resolutionTimeoutId) {
      clearTimeout(this.resolutionTimeoutId);
    }
    if (this.durationTimeoutId) {
      clearTimeout(this.durationTimeoutId);
    }
  }

  // --- Event Handlers ---

  onPromptInput(event: Event) {
    const target = event.target as HTMLTextAreaElement;
    this.promptChanged.emit(target.value);
  }

  /** True when there is anything for the reset control to clear. */
  get hasResettableInput(): boolean {
    return !!(
      this.prompt ||
      this.referenceImages.length ||
      this.image1Preview ||
      this.image2Preview ||
      this.referenceVideo ||
      this.referenceVideos.length ||
      this.externalUrl ||
      this.editSource ||
      this.maskSource ||
      this.conditioningKeyframes.length
    );
  }

  /**
   * Whether reference images can accompany an opening frame in Frames to Video.
   *
   * Veo types its reference images separately from its input image and rejects
   * both together. Omni carries every image in one multimodal input, so an
   * opening frame plus character sheets is the normal way to anchor a shot
   * while holding identities - the backend routes it to image_to_video so the
   * frame stays frame 1.
   */
  get supportsFramePlusReferences(): boolean {
    return !!this.getSelectedModelObject()?.capabilities
      ?.supportsFrameWithReferences;
  }

  get supportsTransformStrength(): boolean {
    return !!this.getSelectedModelObject()?.capabilities
      ?.supportsTransformStrength;
  }

  get supportsDiffusionSteps(): boolean {
    return !!this.getSelectedModelObject()?.capabilities
      ?.supportsDiffusionSteps;
  }

  get supportsSeed(): boolean {
    return !!this.getSelectedModelObject()?.capabilities?.supportsSeed;
  }

  get supportsSeamlessLoop(): boolean {
    return !!this.getSelectedModelObject()?.capabilities?.supportsSeamlessLoop;
  }

  get supportsPerformanceControl(): boolean {
    return !!this.getSelectedModelObject()?.capabilities
      ?.supportsPerformanceControl;
  }

  get supportsAudioControlMode(): boolean {
    return !!this.getSelectedModelObject()?.capabilities
      ?.supportsAudioControlMode;
  }

  get supportsProFormats(): boolean {
    return !!this.getSelectedModelObject()?.capabilities?.supportsProFormats;
  }

  get proFormatBadgeLabel(): string | null {
    if (!this.supportsProFormats || this.outputCodec === 'h264') {
      return null;
    }
    if (this.outputCodec === 'prores') {
      return this.compressionQuality === 'lossless' ? 'ProRes 4444' : 'ProRes';
    }
    if (this.outputCodec === 'dnxhr') {
      return 'DNxHR';
    }
    return null;
  }

  get selectedDeliveryFormatLabel(): string {
    const matched = this.deliveryFormatOptions.find(
      opt =>
        opt.codec === this.outputCodec &&
        opt.quality === this.compressionQuality,
    );
    if (matched) {
      return matched.label;
    }
    if (this.outputCodec === 'prores') {
      return 'Apple ProRes 422 (.mov)';
    }
    if (this.outputCodec === 'dnxhr') {
      return 'Avid DNxHR HQ (.mxf)';
    }
    return 'Web MP4 (H.264)';
  }

  get availableAudioControlOptions(): {
    value:
      | 'generate'
      | 'preserve_video_audio'
      | 'target_audio'
      | 'reference_audio';
    label: string;
  }[] {
    const options: {
      value:
        | 'generate'
        | 'preserve_video_audio'
        | 'target_audio'
        | 'reference_audio';
      label: string;
    }[] = [
      {value: 'generate', label: 'Generate Audio'},
      {value: 'preserve_video_audio', label: 'Preserve Video Audio'},
      {value: 'target_audio', label: 'Sync Target Audio (Lip-Sync / Dub)'},
    ];
    if (this.getSelectedModelObject()?.value === 'omni-cine') {
      options.push({value: 'reference_audio', label: 'Voice/Sound Reference'});
    }
    return options;
  }

  get selectedAudioControlLabel(): string {
    const current = this.audioControlMode ?? 'generate';
    return (
      this.availableAudioControlOptions.find(opt => opt.value === current)
        ?.label ?? 'Generate Audio'
    );
  }

  get showAudioReferenceSlot(): boolean {
    if (!this.getSelectedModelObject()?.capabilities?.supportsAudioReference) {
      return false;
    }
    if (this.mode === 'Ingredients to Video') {
      return true;
    }
    return (
      this.supportsAudioControlMode &&
      (this.audioControlMode === 'target_audio' ||
        this.audioControlMode === 'reference_audio')
    );
  }

  selectDeliveryFormat(option: {
    codec: 'h264' | 'prores' | 'dnxhr';
    quality: 'optimized' | 'lossless';
  }): void {
    this.outputCodec = option.codec;
    this.compressionQuality = option.quality;
    this.outputCodecChanged.emit(option.codec);
    this.compressionQualityChanged.emit(option.quality);
    this.isSettingsDropdownOpen.set(null);
  }

  selectAudioControlMode(
    value:
      | 'generate'
      | 'preserve_video_audio'
      | 'target_audio'
      | 'reference_audio',
  ): void {
    this.audioControlMode = value;
    this.audioControlModeChanged.emit(value);
    this.isSettingsDropdownOpen.set(null);
  }

  onBlueMeshInputToggle(event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;
    this.isBlueMeshInput = checked;
    this.isBlueMeshInputChanged.emit(checked);
  }

  onSeamlessLoopToggle(event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;
    this.seamlessLoop = checked;
    this.seamlessLoopChanged.emit(checked);
  }

  onTessellateHorizontalToggle(event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;
    this.tessellateHorizontal = checked;
    this.tessellateHorizontalChanged.emit(checked);
  }

  onTessellateVerticalToggle(event: Event): void {
    const checked = (event.target as HTMLInputElement).checked;
    this.tessellateVertical = checked;
    this.tessellateVerticalChanged.emit(checked);
  }

  onTransformStrengthInput(event: Event) {
    const val = parseFloat((event.target as HTMLInputElement).value);
    this.transformStrength = val;
    this.transformStrengthChanged.emit(val);
  }

  onNumDiffusionStepsInput(event: Event) {
    const val = parseInt((event.target as HTMLInputElement).value, 10);
    if (!isNaN(val)) {
      this.numDiffusionSteps = val;
      this.numDiffusionStepsChanged.emit(val);
    }
  }

  onSeedInput(event: Event) {
    const raw = (event.target as HTMLInputElement).value.trim();
    if (!raw) {
      this.seed = null;
      this.seedChanged.emit(null);
      return;
    }
    const val = parseInt(raw, 10);
    if (!isNaN(val) && val >= 0) {
      this.seed = val;
      this.seedChanged.emit(val);
    }
  }

  randomizeSeed() {
    const randomVal = Math.floor(Math.random() * 2147483647);
    this.seed = randomVal;
    this.seedChanged.emit(randomVal);
  }

  /**
   * Whether the opening frame carries its own role-tag badge.
   *
   * Only worth showing where the frame travels alongside references and the
   * numbering therefore matters.
   */
  get showFrameRoleTag(): boolean {
    return (
      this.mode === 'Frames to Video' &&
      !!this.image1Preview &&
      this.supportsFramePlusReferences
    );
  }

  /**
   * How far the reference images' tag numbers are pushed along.
   *
   * <IMAGE_REF_N> counts every image in the request, and the backend sends the
   * opening frame first, so with a frame attached the references start at 1.
   * Numbering them from 0 in the UI would point every tag at the wrong image -
   * the same off-by-one that made role tags look broken in Edit Video.
   */
  get roleTagOffset(): number {
    return this.showFrameRoleTag ? 1 : 0;
  }

  /**
   * Whether the second input slot is offered.
   *
   * It means different things per mode: a closing frame in Frames to Video, a
   * second clip in Concatenate. Only the closing frame depends on the model -
   * Gemini Omni cannot interpolate and the backend rejects an end frame, so
   * showing the slot only led to a rejected generate.
   */
  get showSecondSlot(): boolean {
    if (this.mode === 'Concatenate Video') {
      return true;
    }
    if (this.mode === 'Frames to Video') {
      return !!this.getSelectedModelObject()?.capabilities?.supportsLastFrame;
    }
    return false;
  }

  /** Whether the active model binds <IMAGE_REF_N> tags to reference images. */
  get supportsRoleTags(): boolean {
    return !!this.getSelectedModelObject()?.capabilities?.supportsRoleTags;
  }

  /**
   * Inserts a role tag at the caret.
   *
   * Reference images are positional, so the index a tag refers to is otherwise
   * invisible - a user would have to count thumbnails and type the tag by hand.
   */
  insertRoleTag(tag: string): void {
    const textarea: HTMLTextAreaElement | undefined =
      this.promptInput?.nativeElement;
    const current = this.prompt ?? '';

    if (!textarea) {
      this.promptChanged.emit(`${current} ${tag}`.trim());
      return;
    }

    const start = textarea.selectionStart ?? current.length;
    const end = textarea.selectionEnd ?? start;
    const before = current.slice(0, start);
    const after = current.slice(end);

    // Keep the tag as its own word so it does not fuse with adjacent text.
    const lead = before && !/\s$/.test(before) ? ' ' : '';
    const trail = after && !/^\s/.test(after) ? ' ' : '';
    const insertion = `${lead}${tag}${trail}`;

    this.promptChanged.emit(before + insertion + after);

    // The prompt round-trips through the parent, so restore the caret once the
    // new value has been applied.
    const caret = start + insertion.length;
    setTimeout(() => {
      textarea.focus();
      textarea.setSelectionRange(caret, caret);
    });
  }

  /** Whether the active model accepts a sampling temperature. */
  get supportsTemperature(): boolean {
    return !!this.getSelectedModelObject()?.capabilities?.supportsTemperature;
  }

  onTemperatureInput(event: Event): void {
    const value = (event.target as HTMLInputElement).value;
    this.temperatureChanged.emit(value === '' ? null : Number(value));
  }

  onEditOverlayClick(num?: NumPos, index?: number, ref?: ReferenceImage): void {
    if (num) {
      this.editImage.emit({num});
    } else if (index !== undefined && ref) {
      this.editReferenceImage.emit({index, ref});
    }
  }

  // --- Menu Toggles ---

  toggleModeMenu() {
    this.isModeMenuOpen.set(!this.isModeMenuOpen());
    this.isSettingsMenuOpen.set(false);
  }

  toggleSettingsMenu() {
    this.isSettingsMenuOpen.set(!this.isSettingsMenuOpen());
    this.isModeMenuOpen.set(false);
    this.isSettingsDropdownOpen.set(null); // Close inner dropdowns
  }

  // --- Select Handlers ---

  selectMode(mode: string) {
    const oldMode = this.selectedMode();
    this.selectedMode.set(mode);
    this.modeChanged.emit(mode);
    this.isModeMenuOpen.set(false);

    // Call the centralized update method and explicitly pass modeChanged=true
    this.updateSupportedResolutions(
      undefined,
      oldMode !== '' && oldMode !== mode,
    );

    const supportedDurations = this.getSelectedModelDurations();
    if (
      supportedDurations.length > 0 &&
      !supportedDurations.includes(this.selectedDuration())
    ) {
      const fallback = supportedDurations[supportedDurations.length - 1];
      this.selectDuration(fallback);
    }
  }

  selectResolution(resolution: '360p' | '1K' | '2K' | '4K', model?: any) {
    if (!this.supportedResolutions().includes(resolution)) return;

    this.selectedResolution.set(resolution);
    this.resolutionChanged.emit(resolution);
    this.isSettingsDropdownOpen.set(null);

    const supportedDurations = this.getSelectedModelDurations(model);
    if (
      supportedDurations.length > 0 &&
      !supportedDurations.includes(this.selectedDuration())
    ) {
      const fallback = supportedDurations[supportedDurations.length - 1];
      this.selectDuration(fallback);
    }
  }

  selectDuration(duration: number) {
    this.selectedDuration.set(duration);
    this.durationChanged.emit(duration);
    this.isSettingsDropdownOpen.set(null);
  }

  selectNewAspectRatio(ratio: string) {
    this.aspectRatioChanged.emit(ratio);
    this.isSettingsDropdownOpen.set(null);
  }

  selectOutputs(count: number) {
    this.outputsChanged.emit(count);
    this.isSettingsDropdownOpen.set(null);
  }

  // Triggered from internal dropdown
  selectInternalModel(model: any) {
    this.isSettingsDropdownOpen.set(null);
    this.modelSelected.emit(model);

    this.updateSupportedResolutions(model);
    this.syncSelectedDuration(model);
  }

  selectPreset(preset: string) {
    this.selectedPreset.set(preset);
  }

  getSelectedModelObject(): GenerationModelConfig | undefined {
    return this.generationModelsSignal().find(
      m => m.viewValue === this.selectedGenerationModelSignal(),
    );
  }

  getSelectedModelResolutions(model?: any): ('360p' | '1K' | '2K' | '4K')[] {
    const activeModel = model || this.getSelectedModelObject();
    const all = (activeModel?.capabilities?.supportedResolutions ?? []) as (
      | '360p'
      | '1K'
      | '2K'
      | '4K'
    )[];

    // Extending a video in Veo drops to the lowest resolution. Omni 1.1+ allows all resolutions.
    if (
      this.isExtendVideo() &&
      !activeModel?.value?.startsWith('gemini-omni')
    ) {
      const smallest = all[0];
      return smallest ? [smallest] : [];
    }
    return all;
  }

  getSelectedModelDurations(model?: any): number[] {
    const activeModel = model || this.getSelectedModelObject();
    return getSupportedModelDurations(
      activeModel,
      this.selectedMode(),
      this.selectedResolution(),
      !!this.image2PreviewSignal(),
      this.referenceImagesSignal().length > 0,
    );
  }

  private syncSelectedDuration(model?: GenerationModelConfig): void {
    const supported = this.getSelectedModelDurations(model);
    if (supported.length > 0 && !supported.includes(this.selectedDuration())) {
      const clamped = Math.min(
        this.selectedDuration(),
        supported[supported.length - 1],
      );
      const fallback = supported.includes(clamped)
        ? clamped
        : supported[supported.length - 1];
      this.selectedDuration.set(fallback);
      if (this.durationTimeoutId) clearTimeout(this.durationTimeoutId);
      this.durationTimeoutId = setTimeout(() =>
        this.durationChanged.emit(fallback),
      );
    }
  }

  /** Output counts the active model allows, for the x1..xN selector. */
  availableOutputs(): number[] {
    const max = this.getSelectedModelObject()?.capabilities?.maxOutputs ?? 4;
    return Array.from({length: Math.max(1, max)}, (_, i) => i + 1);
  }

  private updateSupportedResolutions(model?: any, modeChanged = false) {
    const supported = this.getSelectedModelResolutions(model);
    this.supportedResolutions.set(supported);

    const activeModel = model || this.getSelectedModelObject();

    // Notify user if resolution was artificially restricted by the current mode
    if (
      activeModel &&
      activeModel.capabilities?.supportedResolutions?.length > 1 &&
      supported.length === 1
    ) {
      // Only show the snackbar if the dropdown is opened or mode changed (avoid spamming on init)
      if (
        model ||
        modeChanged ||
        !supported.includes(this.selectedResolution())
      ) {
        this.snackBar.open(
          `For ${this.selectedMode()} mode, ${activeModel.viewValue} supports only ${supported[0]} resolution.`,
          'Close',
          {duration: 5000},
        );
      }
    }

    if (
      supported.length > 0 &&
      !supported.includes(this.selectedResolution())
    ) {
      const fallbackResolution = supported[0];
      this.selectedResolution.set(fallbackResolution);
      // Defer event emission to avoid ExpressionChangedAfterItHasBeenCheckedError in parent during change detection
      if (this.resolutionTimeoutId) clearTimeout(this.resolutionTimeoutId);
      this.resolutionTimeoutId = setTimeout(() =>
        this.resolutionChanged.emit(fallbackResolution),
      );
    }
  }
}
