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

const YOUTUBE_ID_PATTERN = /^[a-zA-Z0-9_-]{11}$/;

/* eslint-disable n/no-unsupported-features/node-builtins */
export function extractYouTubeVideoId(
  url: string | null | undefined,
): string | null {
  if (!url) return null;
  const trimmed = url.trim();
  if (!trimmed) return null;

  try {
    const parsed = new URL(trimmed);
    const hostname = parsed.hostname.toLowerCase();
    const isYouTube =
      hostname === 'youtube.com' ||
      hostname.endsWith('.youtube.com') ||
      hostname === 'youtube-nocookie.com' ||
      hostname.endsWith('.youtube-nocookie.com');
    const isYoutuBe = hostname === 'youtu.be' || hostname.endsWith('.youtu.be');

    if (isYouTube) {
      if (
        parsed.pathname.startsWith('/embed/') ||
        parsed.pathname.startsWith('/shorts/') ||
        parsed.pathname.startsWith('/v/')
      ) {
        const parts = parsed.pathname.split('/');
        const id = parts[2] || parts[1];
        if (id && YOUTUBE_ID_PATTERN.test(id)) return id;
      }
      const v = parsed.searchParams.get('v');
      if (v && YOUTUBE_ID_PATTERN.test(v)) return v;
    } else if (isYoutuBe) {
      const id = parsed.pathname.slice(1).split('/')[0];
      if (id && YOUTUBE_ID_PATTERN.test(id)) return id;
    } else {
      return null;
    }
  } catch {
    // Ignore invalid URL exceptions and fall back to regex
  }

  // Regex fallback for URLs missing protocol scheme (e.g. youtube.com/watch?v=...)
  const regExp =
    /^(?:https?:\/\/)?(?:[\w-]+\.)?(?:youtube(?:-nocookie)?\.com\/(?:v\/|u\/\w\/|embed\/|shorts\/|watch\?(?:.*&)?v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})(?:[?&#/]|$)/;
  const match = trimmed.match(regExp);
  return match && YOUTUBE_ID_PATTERN.test(match[1]) ? match[1] : null;
}

export function getYouTubeThumbnailUrl(
  videoId: string | null | undefined,
): string | null {
  if (!videoId || !YOUTUBE_ID_PATTERN.test(videoId)) return null;
  return `https://img.youtube.com/vi/${videoId}/mqdefault.jpg`;
}

export function getYouTubeEmbedUrl(
  videoIdOrUrl: string | null | undefined,
): string | null {
  if (!videoIdOrUrl) return null;
  const trimmed = videoIdOrUrl.trim();
  const id = YOUTUBE_ID_PATTERN.test(trimmed)
    ? trimmed
    : extractYouTubeVideoId(trimmed);
  if (!id) return null;
  return `https://www.youtube.com/embed/${id}`;
}
