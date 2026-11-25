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
  extractYouTubeVideoId,
  getYouTubeEmbedUrl,
  getYouTubeThumbnailUrl,
} from './youtube.utils';

describe('youtube.utils', () => {
  describe('extractYouTubeVideoId', () => {
    it('should extract ID from standard watch URLs', () => {
      expect(
        extractYouTubeVideoId('https://www.youtube.com/watch?v=dQw4w9WgXcQ'),
      ).toBe('dQw4w9WgXcQ');
      expect(
        extractYouTubeVideoId(
          'https://m.youtube.com/watch?t=10&v=dQw4w9WgXcQ&feature=share',
        ),
      ).toBe('dQw4w9WgXcQ');
    });

    it('should extract ID from short, shorts, and embed URLs', () => {
      expect(extractYouTubeVideoId('https://youtu.be/dQw4w9WgXcQ?t=42')).toBe(
        'dQw4w9WgXcQ',
      );
      expect(
        extractYouTubeVideoId('https://www.youtube.com/shorts/dQw4w9WgXcQ'),
      ).toBe('dQw4w9WgXcQ');
      expect(
        extractYouTubeVideoId(
          'https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ',
        ),
      ).toBe('dQw4w9WgXcQ');
    });

    it('should extract ID from scheme-less YouTube URLs', () => {
      expect(extractYouTubeVideoId('youtube.com/watch?v=dQw4w9WgXcQ')).toBe(
        'dQw4w9WgXcQ',
      );
    });

    it('should return null for empty or non-YouTube URLs', () => {
      expect(extractYouTubeVideoId(null)).toBeNull();
      expect(extractYouTubeVideoId('   ')).toBeNull();
      expect(
        extractYouTubeVideoId('https://vimeo.com/12345678901'),
      ).toBeNull();
      expect(
        extractYouTubeVideoId('https://evil-youtube.com/watch?v=dQw4w9WgXcQ'),
      ).toBeNull();
    });
  });

  describe('getYouTubeThumbnailUrl', () => {
    it('should return mqdefault thumbnail URL for valid video ID', () => {
      expect(getYouTubeThumbnailUrl('dQw4w9WgXcQ')).toBe(
        'https://img.youtube.com/vi/dQw4w9WgXcQ/mqdefault.jpg',
      );
    });

    it('should return null for invalid video IDs', () => {
      expect(getYouTubeThumbnailUrl(null)).toBeNull();
      expect(getYouTubeThumbnailUrl('short')).toBeNull();
    });
  });

  describe('getYouTubeEmbedUrl', () => {
    it('should return embed URL from raw ID or full YouTube URL', () => {
      expect(getYouTubeEmbedUrl('dQw4w9WgXcQ')).toBe(
        'https://www.youtube.com/embed/dQw4w9WgXcQ',
      );
      expect(
        getYouTubeEmbedUrl('https://www.youtube.com/watch?v=dQw4w9WgXcQ'),
      ).toBe('https://www.youtube.com/embed/dQw4w9WgXcQ');
    });

    it('should return null for invalid input', () => {
      expect(getYouTubeEmbedUrl(undefined)).toBeNull();
      expect(getYouTubeEmbedUrl('https://example.com/video')).toBeNull();
    });
  });
});
