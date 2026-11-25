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

export type GmpCategory = 'all' | 'cinematic' | 'editing' | 'upscale';

export interface GmpFeatureSpec {
  id: string;
  modelValue: string;
  title: string;
  badge: string;
  category: Exclude<GmpCategory, 'all'>;
  icon: string;
  summary: string;
  supportedModes: string[];
  launchRoute: '/video' | '/gallery';
  defaultMode?: string;
  samplePrompt?: string;
  inputsAndSpecs: string[];
  keyConfigs: string[];
  proTips: string[];
}

export interface TabGuideSpec {
  routePrefix: string;
  tabTitle: string;
  subtitle: string;
  icon: string;
  capabilities: {title: string; detail: string}[];
  suggestedQuestions: string[];
}

export const GMP_FEATURES: GmpFeatureSpec[] = [
  {
    id: 'omni-cine',
    modelValue: 'omni-cine',
    title: 'Omni-Cine',
    badge: 'GMP · Multimodal Cinema',
    category: 'cinematic',
    icon: 'movie_filter',
    summary:
      'High-control cinematic generation and localized shot editing combining multi-reference identity anchoring, audio control, and optional inpainting masks.',
    supportedModes: [
      'Text to Video',
      'Frames to Video',
      'Ingredients to Video',
      'Edit Video',
    ],
    launchRoute: '/video',
    defaultMode: 'Ingredients to Video',
    samplePrompt:
      'Wide anamorphic tracking shot through a neon rain-slick alley as the protagonist turns toward camera, dramatic rim lighting.',
    inputsAndSpecs: [
      'Aspect ratio: 16:9 · Resolution: 1K · Duration: 4–10s',
      'Up to 7 reference images + opening/closing frames',
      'Optional audio reference & localized inpainting mask clip',
    ],
    keyConfigs: [
      'Audio Control: Generate, Preserve Video Audio, Target Audio, or Reference Audio',
      'Transform Strength slider (0.1 subtle – 1.0 full restyle)',
      'Delivery Format: H.264 MP4, Apple ProRes (.mov), or Avid DNxHR (.mxf)',
    ],
    proTips: [
      'Use Target Audio when syncing ambient soundscapes or dialogue timing directly to the generated shot.',
      'Attach an Inpainting Mask in Edit Video mode to isolate edits to a specific region while preserving the surrounding scene.',
    ],
  },
  {
    id: 'omni-perf',
    modelValue: 'omni-perf',
    title: 'Omni-Perf Character Control',
    badge: 'GMP · Actor Performance',
    category: 'cinematic',
    icon: 'accessibility_new',
    summary:
      'Transfers subtle facial and body performance from a driving actor video or 2D Blue Mesh onto up to 5 reference character identities.',
    supportedModes: ['Ingredients to Video'],
    launchRoute: '/video',
    defaultMode: 'Ingredients to Video',
    samplePrompt:
      'Close-up dramatic performance in a warm candlelit study, preserving exact facial expressions and head motion from the driving clip.',
    inputsAndSpecs: [
      'Mode: Ingredients to Video · 16:9 · 1K · Duration: 4–10s',
      'Up to 5 character/environment reference images + 1 driving video',
      'Optional target or reference audio track',
    ],
    keyConfigs: [
      'Checkbox: "Driving video is already a Blue Mesh" (leave unchecked to auto-extract from a raw actor clip)',
      'Audio Control Mode + ProRes / DNxHR mezzanine output',
    ],
    proTips: [
      'Pre-compute reusable performance rigs with 2D Performance Estimation (Blue Mesh), then toggle "Driving video is already a Blue Mesh" on.',
    ],
  },
  {
    id: 'perf-estimation',
    modelValue: 'veo-exp-perf-estimation',
    title: '2D Performance Estimation (Blue Mesh)',
    badge: 'GMP · Motion Rigging',
    category: 'editing',
    icon: 'directions_run',
    summary:
      'Extracts a clean 2D Blue Mesh skeletal & facial performance control video from live-action actor footage for driving Omni-Perf.',
    supportedModes: ['Edit Video'],
    launchRoute: '/video',
    defaultMode: 'Edit Video',
    samplePrompt: 'Extract 2D Blue Mesh performance control track.',
    inputsAndSpecs: [
      'Mode: Edit Video · 16:9 · 1K · Duration: 4–10s',
      '1 source actor clip in the Edit Video slot (24 fps recommended)',
    ],
    keyConfigs: ['Deterministic Seed support for reproducible mesh extraction'],
    proTips: [
      'Keep the actor clearly framed without heavy motion blur for crisp facial landmark and pose estimation.',
    ],
  },
  {
    id: 'lip-sync',
    modelValue: 'veo-exp-a2v-generation',
    title: 'Veo Dialogue Lip Sync',
    badge: 'GMP · Audio-to-Video',
    category: 'cinematic',
    icon: 'record_voice_over',
    summary:
      'Generates expressive 8-second spoken dialogue performances driven by a character still image and a recorded voice or TTS audio clip.',
    supportedModes: ['Ingredients to Video'],
    launchRoute: '/video',
    defaultMode: 'Ingredients to Video',
    samplePrompt:
      'Natural conversational studio portrait speaking clearly with subtle eye contact and expressive gestures.',
    inputsAndSpecs: [
      'Mode: Ingredients to Video · 16:9 & 9:16 Portrait · Fixed 8s duration',
      '1 character reference image + 1 dialogue audio clip',
    ],
    keyConfigs: [
      'Portrait (9:16) auto-pillarboxing & post-generation lossless center crop',
      'Delivery Format: H.264, ProRes, or DNxHR',
    ],
    proTips: [
      'Pair with Gemini 3.1 Flash TTS in the Audio tab to synthesize clean dialogue tracks before running Lip Sync.',
    ],
  },
  {
    id: 'video-transform',
    modelValue: 'veo-exp-video-transform',
    title: 'Veo Video Transform / Keyframes',
    badge: 'GMP · V2V & Keyframes',
    category: 'editing',
    icon: 'auto_fix_high',
    summary:
      'Restyles existing footage or interpolates across arbitrary frame-indexed conditioning keyframes with granular diffusion control.',
    supportedModes: ['Edit Video', 'Frames to Video'],
    launchRoute: '/video',
    defaultMode: 'Frames to Video',
    samplePrompt:
      'Stylized painterly stop-motion miniature aesthetic with volumetric warm sunlight and handcrafted textures.',
    inputsAndSpecs: [
      'Modes: Edit Video (V2V + optional Mask) or Frames to Video (multi-keyframe)',
      'Aspect ratios: 16:9 & 9:16 · Duration: 4–8s · 1K resolution',
    ],
    keyConfigs: [
      'Transform Strength (0.10–0.35 subtle, 0.40–0.65 balanced, 0.70–1.00 heavy structural change)',
      'Diffusion Steps (1–250, default 20) + fixed Seed',
      'Intermediate Conditioning Keyframes pinned to exact frame numbers',
      'Delivery Format: H.264, ProRes, or DNxHR',
    ],
    proTips: [
      'Lower Transform Strength (~0.30) preserves camera motion and staging while changing lighting, weather, or art style.',
    ],
  },
  {
    id: 'video-textures',
    modelValue: 'veo-exp-video-textures',
    title: 'Veo Video Textures (Loop & Tile)',
    badge: 'GMP · Seamless Motion',
    category: 'upscale',
    icon: 'grid_on',
    summary:
      'Creates seamless temporal loops and horizontally/vertically tileable motion textures for virtual production walls, games, and backgrounds.',
    supportedModes: ['Text to Video', 'Frames to Video'],
    launchRoute: '/video',
    defaultMode: 'Text to Video',
    samplePrompt:
      'Top-down bioluminescent ocean ripples gently caressing volcanic black sand, seamless ambient motion.',
    inputsAndSpecs: [
      'Modes: Text to Video or Frames to Video · 16:9 · Fixed 8s duration',
      'Optional opening/closing frames + deterministic Seed',
    ],
    keyConfigs: [
      'Temporal Loop toggle (matches end frame smoothly back to frame 0)',
      'Tile Horizontal & Tile Vertical spatial tessellation toggles',
      'Delivery Format: H.264, ProRes, or DNxHR',
    ],
    proTips: [
      'Enable Temporal Loop + Tile Horizontal together to build infinite panoramic LED stage backdrops.',
    ],
  },
  {
    id: 'vpe-4k-upscale',
    modelValue: 'veo3p1_upscale',
    title: '4K UHD Video Upscaler',
    badge: 'GMP · Gallery Post-Production',
    category: 'upscale',
    icon: 'hd',
    summary:
      'Standalone Veo 3.1 super-resolution upsampler elevating 720p or 1080p clips to broadcast-grade 4K UHD directly from Media Gallery.',
    supportedModes: ['Media Gallery → Video Detail → Upscale'],
    launchRoute: '/gallery',
    inputsAndSpecs: [
      'Source requirements: Constant 24 fps · 1280x720, 720x1280, 1920x1080, or 1080x1920',
      'Clips longer than 192 frames (8s) are automatically split, upscaled in 4K segments, and stitched seamlessly',
    ],
    keyConfigs: [
      'Target Resolution: 4K UHD (3840x2160 / 2160x3840) or 1080p',
      'Compression Quality: Optimized or Lossless mezzanine options',
    ],
    proTips: [
      'Open any video in Media Gallery — VPE automatically runs preflight screening and explains in the button tooltip if a clip is not 24 fps.',
    ],
  },
];

export const TAB_GUIDES: TabGuideSpec[] = [
  {
    routePrefix: '/gmp',
    tabTitle: 'GMP Features Hub',
    subtitle: 'Generative Media Pro (VPE) capabilities & quick-launch presets',
    icon: 'auto_awesome',
    capabilities: [
      {
        title: 'One-Click Studio Launch',
        detail:
          'Click "Launch in Studio" on any GMP card to jump into /video with that model, mode, and starter prompt pre-loaded.',
      },
      {
        title: '6 Generation Models + 4K Upscaler',
        detail:
          'Covers Omni-Cine, Omni-Perf, 2D Blue Mesh, Dialogue Lip Sync, Video Transform/Keyframes, Video Textures, and Gallery 4K Upscaling.',
      },
      {
        title: 'Mezzanine Post-Production Formats',
        detail:
          'GMP models support H.264 (.mp4), Apple ProRes (.mov), and Avid DNxHR (.mxf) inside the Settings menu.',
      },
    ],
    suggestedQuestions: [
      'How do 2D Blue Mesh and Omni-Perf work together?',
      'Which GMP model should I use for localized video inpainting?',
      'What are the input requirements for 4K Video Upscale?',
    ],
  },
  {
    routePrefix: '/video',
    tabTitle: 'Video & GMP Studio',
    subtitle: 'Gemini Omni 1.2, Veo 3.1, and GMP experimental video generation',
    icon: 'movie_edit',
    capabilities: [
      {
        title: 'Model & GMP Selector',
        detail:
          'Click the Settings (sliders) icon in the prompt bar → Model to switch between Gemini Omni 1.2 Flash, Veo 3.1, and GMP-badged models.',
      },
      {
        title: 'Mode-Driven Input Slots',
        detail:
          'Click the Mode pill (bottom-left of prompt bar) to switch between Text, Frames, Ingredients, Edit, Extend, and Concatenate modes.',
      },
      {
        title: 'Gemini Omni Role Tags & Editing',
        detail:
          'Click numbered badges on reference thumbnails to insert <IMAGE_REF_0> tags. End Edit Video prompts with "Keep everything else the same."',
      },
    ],
    suggestedQuestions: [
      'Where do I find Delivery Format (ProRes/DNxHR) and Audio Control?',
      'How do <IMAGE_REF_N> role tags work with an opening frame?',
      'Why should Omni Edit Video prompts end with "Keep everything else the same"?',
    ],
  },
  {
    routePrefix: '/gallery',
    tabTitle: 'Media Gallery & 4K Upscale',
    subtitle: 'Browse assets, inspect metadata, Edit with Omni, or Upscale to 4K',
    icon: 'bookmark_heart',
    capabilities: [
      {
        title: 'GMP 4K Video Upscaling',
        detail:
          'Open any 24 fps 720p/1080p video in the gallery and click "Upscale" in the action bar to run Veo Pro 4K super-resolution.',
      },
      {
        title: 'Edit with Omni & Extend',
        detail:
          'Send any generated video directly into Gemini Omni Edit Video, Veo Extend, or Concatenate with one click.',
      },
    ],
    suggestedQuestions: [
      'Why is the Upscale button disabled on one of my gallery clips?',
      'How does VPE handle upscaling videos longer than 8 seconds?',
    ],
  },
  {
    routePrefix: '/audio',
    tabTitle: 'Audio & Voice Studio',
    subtitle: 'Lyria 3 music generation, Gemini 3.1 TTS, and Chirp 3 voices',
    icon: 'graphic_eq',
    capabilities: [
      {
        title: 'Music Generation (Lyria 3)',
        detail:
          'Generate instrumental scores and stems with Lyria 3 Pro Preview, Lyria 3 Clip Preview, and Lyria 2.',
      },
      {
        title: 'Dialogue & TTS for Lip Sync',
        detail:
          'Synthesize multi-language voiceovers with Gemini 3.1 Flash TTS Preview or Chirp 3, then use them in Veo Dialogue Lip Sync.',
      },
    ],
    suggestedQuestions: [
      'How can I generate dialogue audio here and lip-sync a character to it?',
      'What is the difference between Lyria 3 Clip and Lyria 3 Pro?',
    ],
  },
  {
    routePrefix: '/workbench',
    tabTitle: 'Timeline Workbench',
    subtitle: 'Multi-track timeline assembly, trimming, and sequence export',
    icon: 'construction',
    capabilities: [
      {
        title: 'Multi-Clip Sequencing',
        detail:
          'Arrange generated video shots and audio tracks on a timeline, trim in/out points, and export a master cut.',
      },
    ],
    suggestedQuestions: [
      'How do I combine multiple GMP shots and Lyria music in Workbench?',
    ],
  },
  {
    routePrefix: '/workflows',
    tabTitle: 'Agentic Workflows',
    subtitle: 'Multi-step automated pipelines chaining Gemini, Imagen, and Veo',
    icon: 'hub',
    capabilities: [
      {
        title: 'Chained Multimodal Steps',
        detail:
          'Build reusable DAGs passing outputs from text/image steps directly into downstream video and audio steps.',
      },
    ],
    suggestedQuestions: [
      'Which video models and parameters are supported inside Workflows?',
    ],
  },
  {
    routePrefix: '/',
    tabTitle: 'Image Studio',
    subtitle: 'Nano Banana 2, Nano Banana Pro, and Imagen 4 generation & editing',
    icon: 'image',
    capabilities: [
      {
        title: 'Nano Banana 2 & Pro (1K / 2K / 4K)',
        detail:
          'Supports up to 14 reference images, Google Search grounding, Temperature control, and panoramic ratios (21:9, 4:1, 8:1).',
      },
      {
        title: 'Ingredients & Video-to-Image',
        detail:
          'Switch modes in the prompt bar to combine character/style ingredients or extract stills from video clips and YouTube links.',
      },
    ],
    suggestedQuestions: [
      'How can I generate consistent character turnarounds for Omni-Cine or Omni-Perf?',
      'What is the difference between Nano Banana 2 and Nano Banana Pro?',
    ],
  },
];
