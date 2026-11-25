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
  AudioStateService,
  DEFAULT_GEMINI_TTS_MODEL,
  DEFAULT_LYRIA_MODEL,
} from './audio-state.service';

describe('AudioStateService', () => {
  let service: AudioStateService;

  beforeEach(() => {
    try {
      localStorage.removeItem('audio_generation_state');
    } catch {
      /* ignore */
    }
    TestBed.configureTestingModule({});
    service = TestBed.inject(AudioStateService);
  });

  afterEach(() => {
    try {
      localStorage.removeItem('audio_generation_state');
    } catch {
      /* ignore */
    }
  });

  it('should default to Lyria 3 Clip and Gemini 3.1 Flash TTS', () => {
    const state = service.getState();
    expect(state.model).toBe('lyria');
    expect(state.lyriaModel).toBe(DEFAULT_LYRIA_MODEL);
    expect(state.geminiTtsModel).toBe(DEFAULT_GEMINI_TTS_MODEL);
  });

  it('should persist Lyria 3 Pro and Gemini TTS sub-model selections', () => {
    service.updateState({
      model: 'gemini-tts',
      lyriaModel: 'lyria-3-pro-preview',
      geminiTtsModel: 'gemini-3.1-flash-tts-preview',
      prompt: 'Read with warm dramatic tone',
    });

    const loaded = service.getState();
    expect(loaded.model).toBe('gemini-tts');
    expect(loaded.lyriaModel).toBe('lyria-3-pro-preview');
    expect(loaded.geminiTtsModel).toBe('gemini-3.1-flash-tts-preview');
    expect(loaded.prompt).toBe('Read with warm dramatic tone');
  });

  it('should reset invalid stored sub-models to safe defaults', () => {
    localStorage.setItem(
      'audio_generation_state',
      JSON.stringify({
        model: 'invalid-tab',
        lyriaModel: 'lyria-nonexistent',
        geminiTtsModel: 'tts-nonexistent',
      }),
    );

    const loaded = service.getState();
    expect(loaded.model).toBe('lyria');
    expect(loaded.lyriaModel).toBe(DEFAULT_LYRIA_MODEL);
    expect(loaded.geminiTtsModel).toBe(DEFAULT_GEMINI_TTS_MODEL);
  });
});
