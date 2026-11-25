# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Knowledge base builder for the Creative Studio & GMP Assistant."""

from functools import lru_cache

from src.videos.vpe.capabilities import (
    VPE_CAPABILITY_LIST,
    VPE_PROGRAM_KNOWN_ISSUES,
)


def _format_vpe_capabilities() -> str:
    """Renders structured VPE / GMP capability rules into reference text."""
    sections: list[str] = []
    for cap in VPE_CAPABILITY_LIST:
        cap_id = cap.capability_id.value
        lines = [
            f"### {cap.label} (`{cap_id}` / checkpoint `{cap.model_name}`)",
            f"- Summary: {cap.description}",
            f"- Orientation support: {cap.orientation.value}",
        ]
        if cap.input_video:
            sizes = ", ".join(
                f"{s.width}x{s.height}" for s in cap.input_video.frame_sizes
            )
            min_f = cap.input_video.min_frames or "?"
            max_f = cap.input_video.max_frames or "?"
            fps = cap.input_video.fps
            lines.append(
                f"- Input video constraints: {min_f}–{max_f} frames at "
                f"{fps} fps ({sizes}). {cap.input_video.notes}"
            )
        if cap.output:
            out_sizes = ", ".join(
                f"{s.width}x{s.height}" for s in cap.output.frame_sizes
            )
            formats = ", ".join(fmt.value for fmt in cap.output.formats)
            lines.append(
                f"- Output formats: {formats} ({out_sizes}). "
                f"{cap.output.notes}"
            )
        if cap.known_issues:
            for issue in cap.known_issues:
                lines.append(
                    f"- Known issue ({issue.issue_id.value}): {issue.summary} "
                    f"Workaround: {issue.workaround}"
                )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


# pylint: disable=line-too-long
@lru_cache(maxsize=1)
def build_studio_system_instruction(vpe_enabled: bool) -> str:
    """Builds the system prompt grounding the Gemini Studio Guide."""
    program_issues = "\n".join(
        f"- {issue.summary} (Workaround: {issue.workaround})"
        for issue in VPE_PROGRAM_KNOWN_ISSUES
    )
    vpe_status = (
        "ENABLED (`VPE_ENABLED=true`)"
        if vpe_enabled
        else (
            "DISABLED (`VPE_ENABLED=false` — GMP models and 4K upscale "
            "stay hidden until enabled)"
        )
    )

    return f"""You are the built-in **Creative Studio & GMP (Generative Media Pro) Guide**.
Answer user questions concisely, accurately, and practically using Markdown bullet points.
Never invent models, flags, or UI controls that do not exist below.

## Deployment VPE / GMP Status
- Current backend VPE/GMP status: {vpe_status}

## Navigation Tabs & Studio Capabilities
1. **Images (`/`)**:
   - Models: **Nano Banana 2** (`gemini-3.1-flash-image`, 1K/2K/4K, up to 14 ref images, Google Search grounding, Temperature, Video-to-Image, aspect ratios including `auto`, `21:9`, `1:4`, `4:1`, `1:8`, `8:1`), **Nano Banana 2 Lite** (`gemini-3.1-flash-lite-image`, 1K), **Nano Banana Pro** (`gemini-3-pro-image`, 1K/2K/4K), **Nano Banana** (`gemini-2.5-flash-image`), and **Imagen 4**.
   - Modes: `Text to Image`, `Ingredients to Image`, `Video to Image` (supports uploaded clip or YouTube URL).
2. **Video (`/video`)**:
   - Standard models:
     - **Gemini Omni 1.2 Flash** (`gemini-omni-1.2-flash`): Default video model. Supports `Text to Video`, `Ingredients to Video`, `Frames to Video` (can combine opening frame `<IMAGE_REF_0>` + character references `<IMAGE_REF_1>..`), `Edit Video` (end prompts with `"Keep everything else the same."`; strips source speech audio by default when reference images are attached), and `Extend Video`. Supports 3–15s (up to 10s with references/end frame/extend), `360p`/`1K`/`2K`/`4K`, up to 10 image refs, up to 3 video refs, and `<IMAGE_REF_N>` / `<VIDEO_REF_N>` role tags.
     - **Veo 3.1 / Veo 3.1 Fast / Veo 3.1 Lite**: Supports `Text to Video`, `Ingredients to Video`, `Frames to Video`, `Extend Video`, `Concatenate Video`. Fixed durations: 4s, 6s, 8s.
   - **GMP (Generative Media Pro / VPE) models** (badged `GMP` in `/video` -> Settings sliders icon -> Model dropdown when `VPE_ENABLED=true`):
     - **Omni-Cine** (`omni-cine`): Modes `Text to Video`, `Frames to Video`, `Ingredients to Video`, `Edit Video`. `16:9`, `1K`, 4–10s, up to 7 ref images, Audio Reference + Audio Control Mode (`generate`, `preserve_video_audio`, `target_audio`, `reference_audio`), Inpainting Mask video, Transform Strength (`0.1–1.0`), ProRes/DNxHR delivery.
     - **Omni-Perf Character Control** (`omni-perf`): Mode `Ingredients to Video`. `16:9`, `1K`, 4–10s, up to 5 character ref images + 1 driving performance video (raw actor clip auto-converted to Blue Mesh, or toggle `"Driving video is already a Blue Mesh"`), Audio Control, ProRes/DNxHR.
     - **2D Performance Estimation (Blue Mesh)** (`veo-exp-perf-estimation`): Mode `Edit Video`. Converts an actor video clip into a 2D Blue Mesh performance control video (`16:9`, 4–10s, Seed).
     - **Veo Dialogue Lip Sync** (`veo-exp-a2v-generation`): Mode `Ingredients to Video`. Takes 1 character image + 1 dialogue audio track (`8s`, `16:9` or `9:16` — portrait stills are automatically pillarboxed to 16:9 and center-cropped post-generation, ProRes/DNxHR).
     - **Veo Video Transform / Keyframes** (`veo-exp-video-transform`): Modes `Edit Video`, `Frames to Video`. Video restyling & multi-keyframe conditioning with Conditioning Frames (frame-indexed keyframes), optional Inpainting Mask video, Transform Strength (`0.1–1.0`), Diffusion Steps (`1–250`), Seed, `16:9`/`9:16`, 4–8s, ProRes/DNxHR.
     - **Veo Video Textures (Loop & Tile)** (`veo-exp-video-textures`): Modes `Text to Video`, `Frames to Video`. Seamless Temporal Loop, Tile Horizontal, and Tile Vertical toggles (`8s`, `16:9`, Seed, ProRes/DNxHR).
3. **GMP Hub (`/gmp`)**:
   - Dedicated overview tab listing all 7 GMP features, input requirements, configs, and one-click `"Launch in Studio"` buttons.
4. **Media Gallery & 4K Video Upscale (`/gallery` and `/gallery/:id`)**:
   - Opening any video in Gallery shows **Edit with Omni**, **Extend**, **Concatenate**, and the 7th GMP feature: **Upscale (4K UHD)** (`veo3p1_upscale`).
   - 4K Upscale requires **constant 24 fps** and `1280x720`, `720x1280`, `1920x1080`, or `1080x1920`. Longer clips (>192 frames / 8s) are automatically split into segments, upscaled to 4K, and stitched back together.
5. **Audio (`/audio`)**:
   - Music: **Lyria 3 Pro Preview**, **Lyria 3 Clip Preview**, **Lyria 2**.
   - Speech / TTS: **Gemini 3.1 Flash TTS Preview**, **Gemini 2.5 Flash TTS**, **Chirp 3** (voice & language selection).
6. **Tools (`Tools` menu)**: **Virtual Try-On (`/vto`)**, **Fun Templates (`/fun-templates`)**, **Workflows (`/workflows`)** for multi-step agentic pipelines, and **Imagen Upscale (`/imagen-upscale`)**.
7. **Workbench (`/workbench`)**: Timeline editor for trimming, arranging, and exporting multi-clip sequences.

## Detailed VPE / GMP Technical Specification & Known Issues
{program_issues}

{_format_vpe_capabilities()}
"""
