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
"""Tests for VPE media preflight.

Unlike the rest of the VPE integration, this module can be verified for
real: it only measures files. Three sources of truth are used, in order of
preference.

* The clips under ``_review/`` - genuine 24 fps Veo output, 120 to 240
  frames, portrait and landscape, most with an AAC track. They are the
  material this feature will actually be pointed at, and they are the reason
  frame count rather than container duration is authoritative: they carry
  exactly 240 frames in a container that reports 10.005 seconds. That
  directory is gitignored, so where it is absent an equivalent is generated
  to the same specification rather than skipped - see ``_corpus``.
* Files generated with ffmpeg into a temporary directory, for the off-spec
  cases no real clip in the repo covers - 30 fps, 23.976 fps, 640x360,
  ProRes, stills and audio. Nothing is written into the repository.
* Hand-built ``VpeMediaProbe`` values for the few states that cannot be
  produced on demand, such as a container that carries no frame count.
  ``validate`` is a pure function of a probe and a capability, so this is
  ordinary input, not mocking.
"""

import pathlib
import shutil
import subprocess
import tempfile
from fractions import Fraction
from typing import NamedTuple

import pytest

from src.videos.vpe.capabilities import VpeCapabilityId, get_capability
from src.videos.vpe.preflight import (
    VpeFrameOrientation,
    VpeMediaKind,
    VpeMediaProbe,
    VpeProbeError,
    VpeReasonCode,
    VpeSeverity,
    preflight_media,
    probe_media,
    validate,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
_CORPUS_ROOT = _REPO_ROOT / "_review"

_CORPUS_FPS = 24


class _CorpusClip(NamedTuple):
    """A real clip, and the specification a stand-in has to meet without it.

    Every field was measured off the real file with ffprobe rather than
    assumed, because the point of a stand-in is that it probes identically.

    Attributes:
        path: Location under ``_review/``, relative to the corpus root.
        width: Frame width in pixels.
        height: Frame height in pixels.
        frames: Exact frame count. Every corpus clip runs at 24 fps.
        audio: Whether the clip carries an AAC track at 48 kHz.
        container_seconds: What the container reports. This is not always
            ``frames / 24``: on most of these the audio track outruns the
            video by a few milliseconds and the container follows the longer
            stream, which is the discrepancy the module exists to survive.
    """

    path: str
    width: int
    height: int
    frames: int
    audio: bool
    container_seconds: float


# Real Veo output, picked for the spread of frame counts and orientations.
# Every one of these probes at exactly 24 fps.
_CORPUS = {
    "landscape_120": _CorpusClip(
        path="tag-adherence/ab_swap/A-tagged-1.mp4",
        width=1280,
        height=720,
        frames=120,
        audio=True,
        container_seconds=5.013,
    ),
    "landscape_240": _CorpusClip(
        path="woman-swap/denim-1.mp4",
        width=1280,
        height=720,
        frames=240,
        audio=True,
        container_seconds=10.005,
    ),
    "landscape_240_silent": _CorpusClip(
        path="woman-swap/source_train_noaudio.mp4",
        width=1280,
        height=720,
        frames=240,
        audio=False,
        container_seconds=10.0,
    ),
    "portrait_120_silent": _CorpusClip(
        path="tag-adherence/source/source_clip.mp4",
        width=720,
        height=1280,
        frames=120,
        audio=False,
        container_seconds=5.0,
    ),
    "portrait_144": _CorpusClip(
        path="videos/T1-01.mp4",
        width=720,
        height=1280,
        frames=144,
        audio=True,
        container_seconds=6.016,
    ),
    "portrait_192": _CorpusClip(
        path="videos/T3-01.mp4",
        width=720,
        height=1280,
        frames=192,
        audio=True,
        container_seconds=8.0,
    ),
    "portrait_216": _CorpusClip(
        path="micronovela-shots/s2_ballroom_enriqueta.mp4",
        width=720,
        height=1280,
        frames=216,
        audio=True,
        container_seconds=9.002,
    ),
    "portrait_240": _CorpusClip(
        path="cantina-first-encounter/cantina_first_encounter.mp4",
        width=720,
        height=1280,
        frames=240,
        audio=True,
        container_seconds=10.005,
    ),
}

_HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

# Stand-ins are cached between runs, outside the repository. The filename
# carries the whole specification, so changing a spec asks for a file that
# does not exist yet rather than silently reusing one built to the old one.
_STANDIN_ROOT = pathlib.Path(tempfile.gettempdir()) / "vpe_preflight_corpus"


def _build_standin(name: str, spec: _CorpusClip) -> str:
    """Builds a clip that probes identically to a corpus clip.

    The audio is encoded separately and muxed in with ``-c copy``, which
    looks roundabout but is the only way it keeps a duration of its own:
    encoding both streams in one pass ends the audio with the video and
    loses the container overhang, and that overhang is the whole point of
    several of these tests.

    Args:
        name: A key of ``_CORPUS``.
        spec: The specification to meet.

    Returns:
        The absolute path to the built clip.
    """
    audio = "aac" if spec.audio else "silent"
    stem = (
        f"{name}_{spec.width}x{spec.height}"
        f"_{spec.frames}f_{spec.container_seconds:.3f}s_{audio}"
    )
    target = _STANDIN_ROOT / f"{stem}.mp4"
    if target.is_file():
        return str(target)

    _STANDIN_ROOT.mkdir(parents=True, exist_ok=True)
    video_seconds = spec.frames / _CORPUS_FPS
    with tempfile.TemporaryDirectory(dir=_STANDIN_ROOT) as scratch_name:
        scratch = pathlib.Path(scratch_name)
        built = scratch / "video.mp4"
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={spec.width}x{spec.height}"
            f":rate={_CORPUS_FPS}:duration={video_seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-frames:v",
            str(spec.frames),
            str(built),
        )
        if spec.audio:
            track = scratch / "audio.m4a"
            _ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000"
                f":duration={spec.container_seconds}",
                "-c:a",
                "aac",
                "-t",
                str(spec.container_seconds),
                str(track),
            )
            muxed = scratch / "muxed.mp4"
            _ffmpeg(
                "-i", str(built), "-i", str(track), "-c", "copy", str(muxed)
            )
            built = muxed
        # Moved into place only once complete, so an interrupted run cannot
        # leave a truncated file for the next one to trust.
        built.replace(target)
    return str(target)


def _corpus(name: str) -> str:
    """Returns a clip meeting ``name``'s specification.

    The real clip under ``_review/`` is used wherever it is checked out,
    because it is genuine model output and nothing synthetic is as
    convincing. That directory is gitignored, so everywhere else an
    equivalent is generated instead: same dimensions, same exact frame count
    at 24 fps, same audio track, same container duration. Those are the only
    properties these tests measure, so the stand-in tests the same thing.

    Skipping was the previous behaviour and it was worse than useless: 83
    tests reported green on every machine but the one laptop the corpus
    happens to live on, which is the shape of a suite that is not running at
    all rather than one that is passing.

    Args:
        name: A key of ``_CORPUS``.

    Returns:
        The absolute path to a clip meeting the specification.
    """
    if not _HAS_FFMPEG:
        pytest.skip("ffmpeg and ffprobe are not installed")
    spec = _CORPUS[name]
    real = _CORPUS_ROOT / spec.path
    if real.is_file():
        return str(real)
    return _build_standin(name, spec)


def _ffmpeg(*args: str) -> None:
    """Runs ffmpeg, failing the test loudly if it cannot produce a fixture.

    Args:
        *args: Arguments after ``ffmpeg -v error -y``.
    """
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="session", name="fixtures")
def fixtures_fixture(tmp_path_factory) -> dict[str, str]:
    """Builds the off-spec media the repository has no real example of.

    Args:
        tmp_path_factory: pytest's session-scoped temporary directory maker.

    Returns:
        A mapping from logical name to path, all outside the repository.
    """
    if not _HAS_FFMPEG:
        pytest.skip("ffmpeg is not installed")
    root = tmp_path_factory.mktemp("vpe_preflight")

    def path(name: str) -> str:
        return str(root / name)

    # 1280x720 at 24 fps, exactly 192 frames: in spec for every landscape
    # capability, and the one shape the real corpus does not contain.
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=1280x720:rate=24:duration=8",
        "-pix_fmt",
        "yuv420p",
        path("landscape_192.mp4"),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=1280x720:rate=30:duration=5",
        "-pix_fmt",
        "yuv420p",
        path("thirty_fps.mp4"),
    )
    # 23.976 fps: the rate that rounds to 24 in float and is rejected.
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=1280x720:rate=24000/1001:duration=5",
        "-pix_fmt",
        "yuv420p",
        path("ntsc.mp4"),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=640x360:rate=24:duration=5",
        "-pix_fmt",
        "yuv420p",
        path("tiny.mp4"),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=1920x1080:rate=24:duration=5",
        "-pix_fmt",
        "yuv420p",
        path("hd_1080p.mp4"),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=1080x1920:rate=24:duration=5",
        "-pix_fmt",
        "yuv420p",
        path("hd_1080p_portrait.mp4"),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=1280x720:rate=24:duration=5",
        "-c:v",
        "prores_ks",
        path("prores.mov"),
    )
    # A raw elementary stream carries no frame count, which exercises the
    # counting fallback in probe_media.
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=1280x720:rate=24:duration=5",
        "-c:v",
        "libx264",
        "-bsf:v",
        "h264_mp4toannexb",
        "-f",
        "h264",
        path("raw.h264"),
    )
    for name, size in (
        ("still_720p.png", "1280x720"),
        ("still_1080p.png", "1920x1080"),
        ("still_portrait.jpg", "720x1280"),
    ):
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={size}:rate=24:duration=1",
            "-frames:v",
            "1",
            path(name),
        )
    for name, seconds in (
        ("speech_8s.wav", 8),
        ("speech_12s.wav", 12),
        ("speech_5s.wav", 5),
    ):
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:a",
            "pcm_s16le",
            path(name),
        )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=8",
        "-c:a",
        "aac",
        path("speech_8s.m4a"),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=1280x720:rate=24:duration=5",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=5",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        path("landscape_120_audio.mp4"),
    )

    text_file = root / "notes.txt"
    text_file.write_text("not media", encoding="utf-8")

    return {item.name: str(item) for item in root.iterdir()}


def _probe(fixtures: dict[str, str], name: str) -> VpeMediaProbe:
    """Measures one generated fixture.

    Args:
        fixtures: The generated fixture mapping.
        name: File name within it.

    Returns:
        The measurement.
    """
    return probe_media(fixtures[name])


def _finding(verdict, code: VpeReasonCode):
    """Returns the single finding carrying a code, asserting it exists.

    Args:
        verdict: A verdict from ``validate``.
        code: The reason code to pull out.

    Returns:
        The matching finding.
    """
    matches = [item for item in verdict.findings if item.code is code]
    assert (
        matches
    ), f"expected {code.value}, got {[c.value for c in verdict.codes]}"
    assert len(matches) == 1
    return matches[0]


# --- probe_media, against real clips ------------------------------------


def test_probe_reports_frame_rate_as_an_exact_rational():
    probe = probe_media(_corpus("landscape_120"))
    assert probe.fps == Fraction(24, 1)
    assert probe.nominal_fps == Fraction(24, 1)
    assert isinstance(probe.fps, Fraction)


def test_probe_prefers_frame_count_to_container_duration():
    """The exact discrepancy the module exists to survive."""
    probe = probe_media(_corpus("landscape_240"))
    assert probe.frame_count == 240
    assert probe.frame_count_exact
    assert probe.container_seconds == pytest.approx(10.005, abs=0.001)
    assert probe.frame_seconds == pytest.approx(10.0)


def test_probe_reports_size_codec_and_container():
    probe = probe_media(_corpus("landscape_120"))
    assert (probe.width, probe.height) == (1280, 720)
    assert probe.video_codec == "h264"
    assert "mp4" in probe.container
    assert probe.mime_type == "video/mp4"
    assert probe.size_bytes > 0


def test_probe_reports_orientation_both_ways():
    landscape = probe_media(_corpus("landscape_120"))
    portrait = probe_media(_corpus("portrait_192"))
    assert landscape.orientation is VpeFrameOrientation.LANDSCAPE
    assert not landscape.is_portrait
    assert portrait.orientation is VpeFrameOrientation.PORTRAIT
    assert portrait.is_portrait


def test_probe_detects_an_audio_stream():
    with_audio = probe_media(_corpus("portrait_216"))
    without_audio = probe_media(_corpus("landscape_240_silent"))
    assert with_audio.has_audio
    assert with_audio.audio_codec == "aac"
    assert not without_audio.has_audio
    assert without_audio.audio_codec is None


@pytest.mark.parametrize(
    ("name", "frames"),
    [
        ("landscape_120", 120),
        ("portrait_144", 144),
        ("portrait_192", 192),
        ("portrait_216", 216),
        ("portrait_240", 240),
    ],
)
def test_probe_counts_frames_across_the_corpus(name: str, frames: int):
    probe = probe_media(_corpus(name))
    assert probe.kind is VpeMediaKind.VIDEO
    assert probe.frame_count == frames
    assert probe.fps == Fraction(24, 1)


# --- probe_media, against generated media -------------------------------


def test_probe_reads_a_non_integer_frame_rate_without_rounding(fixtures):
    probe = _probe(fixtures, "ntsc.mp4")
    assert probe.fps == Fraction(24000, 1001)
    assert probe.fps != Fraction(24, 1)
    assert float(probe.fps) == pytest.approx(23.976, abs=0.001)


def test_probe_counts_frames_when_the_container_omits_them(fixtures):
    probe = _probe(fixtures, "raw.h264")
    assert probe.frame_count == 120
    assert probe.frame_count_exact


def test_probe_classifies_a_still_image(fixtures):
    probe = _probe(fixtures, "still_720p.png")
    assert probe.kind is VpeMediaKind.IMAGE
    assert (probe.width, probe.height) == (1280, 720)
    assert probe.mime_type == "image/png"
    assert probe.frame_count == 1
    # ffprobe invents 25/1 for a PNG; a still must not carry a frame rate.
    assert probe.fps is None
    assert probe.frame_seconds is None


def test_probe_classifies_a_jpeg_still(fixtures):
    probe = _probe(fixtures, "still_portrait.jpg")
    assert probe.kind is VpeMediaKind.IMAGE
    assert probe.mime_type == "image/jpeg"
    assert probe.orientation is VpeFrameOrientation.PORTRAIT


def test_probe_classifies_audio(fixtures):
    probe = _probe(fixtures, "speech_8s.wav")
    assert probe.kind is VpeMediaKind.AUDIO
    assert probe.mime_type == "audio/wav"
    assert probe.has_audio
    assert probe.width is None
    assert probe.frame_size is None
    assert probe.orientation is None
    assert probe.container_seconds == pytest.approx(8.0, abs=0.01)


def test_probe_distinguishes_prores_from_mp4_in_the_same_container(fixtures):
    prores = _probe(fixtures, "prores.mov")
    h264 = _probe(fixtures, "landscape_192.mp4")
    assert prores.container == h264.container
    assert prores.mime_type == "video/quicktime"
    assert h264.mime_type == "video/mp4"


def test_probe_rejects_a_missing_file():
    with pytest.raises(VpeProbeError, match="No such media file"):
        probe_media("/tmp/definitely-not-here-8f2c.mp4")


def test_probe_rejects_a_file_that_is_not_media(fixtures):
    with pytest.raises(VpeProbeError):
        probe_media(fixtures["notes.txt"])


# --- validate, against real clips ---------------------------------------


def test_in_spec_landscape_clip_passes_the_upscaler():
    verdict = preflight_media(_corpus("landscape_120"), VpeCapabilityId.UPSCALE)
    assert not verdict.is_blocked
    # 120 frames is inside 96-192, so the only remark is the audio track.
    assert verdict.codes == (VpeReasonCode.SOURCE_AUDIO_NOT_PRESERVED,)


def test_silent_in_spec_clip_produces_no_findings_at_all():
    verdict = preflight_media(
        _corpus("portrait_120_silent"),
        VpeCapabilityId.UPSCALE,
    )
    assert verdict.severity is VpeSeverity.OK
    assert verdict.findings == ()
    assert verdict.summary().endswith("is within spec for Upscaler.")


def test_portrait_is_fine_for_the_one_portrait_capable_model():
    verdict = preflight_media(_corpus("portrait_192"), VpeCapabilityId.UPSCALE)
    assert not verdict.is_blocked
    assert not verdict.has(VpeReasonCode.PORTRAIT_NOT_SUPPORTED)


def test_portrait_blocks_a_landscape_only_model_with_the_arithmetic():
    verdict = preflight_media(
        _corpus("portrait_192"),
        VpeCapabilityId.VIDEO_TRANSFORM,
    )
    assert verdict.is_blocked
    finding = _finding(verdict, VpeReasonCode.PORTRAIT_NOT_SUPPORTED)
    # A warning would be wrong: the API accepts this and bills for a frame
    # that is mostly black, so the message has to show the loss.
    assert finding.severity is VpeSeverity.BLOCK
    assert "720x1280" in finding.message
    assert "405x720" in finding.message
    assert "875 px" in finding.message
    assert "32%" in finding.message
    assert "Crop to 1280x720" in finding.remedy


def test_over_length_clip_states_measured_and_required_length():
    verdict = preflight_media(_corpus("portrait_216"), VpeCapabilityId.UPSCALE)
    assert verdict.is_blocked
    finding = _finding(verdict, VpeReasonCode.FRAME_COUNT_TOO_HIGH)
    assert "9s (216 frames)" in finding.message
    assert "4-8s (96-192 frames)" in finding.message
    assert finding.remedy == "Trim to at most 192 frames (8s)."


def test_exact_length_models_say_exactly():
    verdict = preflight_media(
        _corpus("landscape_240"),
        VpeCapabilityId.PERF_ESTIMATION,
    )
    finding = _finding(verdict, VpeReasonCode.FRAME_COUNT_TOO_HIGH)
    assert "10s (240 frames)" in finding.message
    assert "exactly 8s (192 frames)" in finding.message


def test_240_frames_is_in_spec_for_omni_cine():
    """Omni-Cine is the only capability that takes more than 192 frames."""
    verdict = preflight_media(
        _corpus("landscape_240_silent"),
        VpeCapabilityId.OMNI_CINE,
    )
    assert verdict.severity is VpeSeverity.OK
    assert verdict.findings == ()


def test_omni_cine_warns_that_it_ignores_a_source_audio_track():
    verdict = preflight_media(
        _corpus("landscape_240"),
        VpeCapabilityId.OMNI_CINE,
    )
    assert not verdict.is_blocked
    finding = _finding(verdict, VpeReasonCode.AUDIO_INPUT_UNSUPPORTED)
    assert "does not accept audio input" in finding.message


def test_performance_generation_calls_its_video_the_blue_mesh():
    verdict = preflight_media(
        _corpus("landscape_240"),
        VpeCapabilityId.PERF_GENERATION,
    )
    finding = _finding(verdict, VpeReasonCode.FRAME_COUNT_TOO_HIGH)
    assert "This blue mesh is 10s (240 frames)" in finding.message


@pytest.mark.parametrize("clip", sorted(_CORPUS))
@pytest.mark.parametrize(
    "capability_id",
    list(VpeCapabilityId),
    ids=[item.value for item in VpeCapabilityId],
)
def test_every_real_clip_against_every_capability_is_answerable(
    clip: str,
    capability_id: VpeCapabilityId,
):
    """No combination may raise, and every finding must be legible."""
    verdict = preflight_media(_corpus(clip), capability_id)
    assert verdict.capability_id is capability_id
    assert verdict.severity in tuple(VpeSeverity)
    for finding in verdict.findings:
        assert finding.message.endswith(".")
        assert finding.measured
        assert finding.required
    # Blocking findings are reported before advisory ones.
    ranks = [finding.severity.rank for finding in verdict.findings]
    assert ranks == sorted(ranks, reverse=True)


# --- validate, against generated media ----------------------------------


def test_thirty_fps_is_blocked_with_both_numbers(fixtures):
    verdict = validate(_probe(fixtures, "thirty_fps.mp4"), "upscale")
    finding = _finding(verdict, VpeReasonCode.FPS_MISMATCH)
    assert finding.severity is VpeSeverity.BLOCK
    assert "30 fps" in finding.message
    assert "exactly 24 fps" in finding.message
    assert "24 fps" in finding.remedy


def test_23_976_fps_is_blocked_and_named_as_a_rational(fixtures):
    """Float comparison is the trap this test exists to keep shut."""
    verdict = validate(_probe(fixtures, "ntsc.mp4"), VpeCapabilityId.UPSCALE)
    finding = _finding(verdict, VpeReasonCode.FPS_MISMATCH)
    assert "23.976 fps (24000/1001)" in finding.message


def test_sub_720p_is_blocked_and_not_told_to_crop(fixtures):
    verdict = validate(
        _probe(fixtures, "tiny.mp4"),
        VpeCapabilityId.VIDEO_TRANSFORM,
    )
    finding = _finding(verdict, VpeReasonCode.FRAME_SIZE_UNSUPPORTED)
    assert "640x360" in finding.message
    assert "1280x720" in finding.message
    # Cropping a source that is already too small removes the pixels that
    # are missing; the remedy has to point the other way.
    assert "smaller than 1280x720" in finding.remedy
    assert "below 720p" in finding.remedy


def test_oversized_landscape_source_is_told_to_crop(fixtures):
    verdict = validate(
        _probe(fixtures, "hd_1080p.mp4"),
        VpeCapabilityId.VIDEO_TRANSFORM,
    )
    finding = _finding(verdict, VpeReasonCode.FRAME_SIZE_UNSUPPORTED)
    assert "1920x1080" in finding.message
    assert "Crop rather than pad" in finding.remedy


def test_generated_in_spec_clip_passes_performance_estimation(fixtures):
    verdict = validate(
        _probe(fixtures, "landscape_192.mp4"),
        VpeCapabilityId.PERF_ESTIMATION,
    )
    assert verdict.severity is VpeSeverity.OK
    assert verdict.findings == ()


def test_prores_is_blocked_only_where_h264_is_the_only_format(fixtures):
    probe = _probe(fixtures, "prores.mov")
    estimation = validate(probe, VpeCapabilityId.PERF_ESTIMATION)
    upscale = validate(probe, VpeCapabilityId.UPSCALE)
    finding = _finding(estimation, VpeReasonCode.MIME_TYPE_UNSUPPORTED)
    assert "video/quicktime" in finding.message
    assert "video/mp4" in finding.required
    assert not upscale.has(VpeReasonCode.MIME_TYPE_UNSUPPORTED)


def test_1080p_source_warns_about_the_aspect_ratio_defect(fixtures):
    landscape = validate(
        _probe(fixtures, "hd_1080p.mp4"),
        VpeCapabilityId.UPSCALE,
    )
    portrait = validate(
        _probe(fixtures, "hd_1080p_portrait.mp4"),
        VpeCapabilityId.UPSCALE,
    )
    assert not landscape.is_blocked
    assert not portrait.is_blocked
    assert (
        '"16:9"'
        in _finding(
            landscape,
            VpeReasonCode.UPSCALE_1080P_NEEDS_ASPECT_RATIO,
        ).remedy
    )
    assert (
        '"9:16"'
        in _finding(
            portrait,
            VpeReasonCode.UPSCALE_1080P_NEEDS_ASPECT_RATIO,
        ).remedy
    )


def test_720p_still_passes_dialogue_driven(fixtures):
    verdict = validate(
        _probe(fixtures, "still_720p.png"),
        VpeCapabilityId.DIALOGUE_DRIVEN,
    )
    assert verdict.severity is VpeSeverity.OK


def test_portrait_still_blocks_dialogue_driven(fixtures):
    verdict = validate(
        _probe(fixtures, "still_portrait.jpg"),
        VpeCapabilityId.DIALOGUE_DRIVEN,
    )
    finding = _finding(verdict, VpeReasonCode.PORTRAIT_NOT_SUPPORTED)
    assert finding.severity is VpeSeverity.BLOCK
    assert "405x720" in finding.message
    assert "32%" in finding.message


def test_off_spec_landscape_still_only_warns_where_the_model_resizes(
    fixtures,
):
    resized = validate(
        _probe(fixtures, "still_1080p.png"),
        VpeCapabilityId.VIDEO_TEXTURES,
    )
    strict = validate(
        _probe(fixtures, "still_1080p.png"),
        VpeCapabilityId.VIDEO_TRANSFORM,
    )
    assert not resized.is_blocked
    finding = _finding(resized, VpeReasonCode.IMAGE_RESIZED_INTERNALLY)
    assert "1920x1080" in finding.message
    assert "1280x720" in finding.message
    assert strict.is_blocked
    assert strict.has(VpeReasonCode.FRAME_SIZE_UNSUPPORTED)


def test_reference_images_may_be_any_size_for_omni_cine(fixtures):
    for name in ("still_portrait.jpg", "still_1080p.png"):
        verdict = validate(_probe(fixtures, name), VpeCapabilityId.OMNI_CINE)
        assert verdict.severity is VpeSeverity.OK, name


def test_a_single_png_is_not_a_frame_sequence(fixtures):
    verdict = validate(
        _probe(fixtures, "still_720p.png"),
        VpeCapabilityId.UPSCALE,
    )
    finding = _finding(verdict, VpeReasonCode.MEDIA_KIND_UNSUPPORTED)
    assert "glob gcsUri" in finding.message
    assert "sequence" in finding.remedy


def test_video_is_blocked_where_a_capability_has_no_video_slot(fixtures):
    verdict = validate(
        _probe(fixtures, "landscape_192.mp4"),
        VpeCapabilityId.VIDEO_TEXTURES,
    )
    finding = _finding(verdict, VpeReasonCode.MEDIA_KIND_UNSUPPORTED)
    assert "takes still images" in finding.message


def test_eight_second_audio_passes_dialogue_driven(fixtures):
    verdict = validate(
        _probe(fixtures, "speech_8s.wav"),
        VpeCapabilityId.DIALOGUE_DRIVEN,
    )
    assert verdict.severity is VpeSeverity.OK


def test_long_audio_warns_that_the_tail_is_dropped(fixtures):
    verdict = validate(
        _probe(fixtures, "speech_12s.wav"),
        VpeCapabilityId.DIALOGUE_DRIVEN,
    )
    finding = _finding(verdict, VpeReasonCode.AUDIO_LENGTH_MISMATCH)
    # Documented as truncated rather than rejected, so this cannot block.
    assert finding.severity is VpeSeverity.WARN
    assert "12s" in finding.message
    assert "exactly 8s" in finding.message
    assert "dropped" in finding.message


def test_short_audio_warns_that_silence_is_added(fixtures):
    verdict = validate(
        _probe(fixtures, "speech_5s.wav"),
        VpeCapabilityId.DIALOGUE_DRIVEN,
    )
    finding = _finding(verdict, VpeReasonCode.AUDIO_LENGTH_MISMATCH)
    assert "padded with silence" in finding.message


def test_aac_audio_warns_that_it_has_no_documented_mime_type(fixtures):
    verdict = validate(
        _probe(fixtures, "speech_8s.m4a"),
        VpeCapabilityId.DIALOGUE_DRIVEN,
    )
    finding = _finding(verdict, VpeReasonCode.MIME_TYPE_UNDOCUMENTED)
    assert finding.severity is VpeSeverity.WARN
    assert "audio/wav" in finding.message


def test_audio_is_blocked_for_a_model_that_cannot_take_it(fixtures):
    verdict = validate(
        _probe(fixtures, "speech_8s.wav"),
        VpeCapabilityId.OMNI_CINE,
    )
    finding = _finding(verdict, VpeReasonCode.MEDIA_KIND_UNSUPPORTED)
    assert "Audio input is explicitly unsupported" in finding.message


def test_generated_clip_with_audio_warns_about_the_lost_track(fixtures):
    verdict = validate(
        _probe(fixtures, "landscape_120_audio.mp4"),
        VpeCapabilityId.UPSCALE,
    )
    finding = _finding(verdict, VpeReasonCode.SOURCE_AUDIO_NOT_PRESERVED)
    assert finding.severity is VpeSeverity.WARN
    assert "Re-mux" in finding.remedy


# --- validate, on hand-built measurements -------------------------------


def _video_probe(**overrides) -> VpeMediaProbe:
    """Builds an otherwise in-spec 1280x720 24 fps 192-frame measurement.

    Args:
        **overrides: Fields to change.

    Returns:
        The measurement.
    """
    defaults = {
        "path": "/tmp/synthetic.mp4",
        "kind": VpeMediaKind.VIDEO,
        "container": "mov,mp4,m4a,3gp,3g2,mj2",
        "size_bytes": 4 * 1024 * 1024,
        "width": 1280,
        "height": 720,
        "fps": Fraction(24, 1),
        "nominal_fps": Fraction(24, 1),
        "frame_count": 192,
        "container_seconds": 8.0,
        "video_codec": "h264",
        "mime_type": "video/mp4",
    }
    return VpeMediaProbe(**{**defaults, **overrides})


def test_hand_built_baseline_is_in_spec():
    verdict = validate(_video_probe(), VpeCapabilityId.PERF_ESTIMATION)
    assert verdict.findings == ()


def test_derived_frame_count_is_flagged_as_derived():
    verdict = validate(
        _video_probe(frame_count_exact=False),
        VpeCapabilityId.PERF_ESTIMATION,
    )
    finding = _finding(verdict, VpeReasonCode.FRAME_COUNT_ESTIMATED)
    assert finding.severity is VpeSeverity.WARN
    assert "~192 frames" in finding.measured


def test_variable_frame_rate_is_flagged_even_when_the_average_is_24():
    verdict = validate(
        _video_probe(nominal_fps=Fraction(48, 1)),
        VpeCapabilityId.PERF_ESTIMATION,
    )
    finding = _finding(verdict, VpeReasonCode.VARIABLE_FRAME_RATE)
    assert finding.severity is VpeSeverity.WARN
    assert "48 fps" in finding.message


def test_variable_frame_rate_is_named_inside_an_fps_block():
    verdict = validate(
        _video_probe(fps=Fraction(5760, 239), nominal_fps=Fraction(24, 1)),
        VpeCapabilityId.PERF_ESTIMATION,
    )
    finding = _finding(verdict, VpeReasonCode.FPS_MISMATCH)
    assert "declared base rate is 24 fps" in finding.message


def test_file_size_cap_is_enforced_where_one_is_documented():
    limit = 50 * 1024 * 1024
    under = validate(
        _video_probe(size_bytes=limit),
        VpeCapabilityId.OMNI_CINE,
    )
    over = validate(
        _video_probe(size_bytes=limit + 1),
        VpeCapabilityId.OMNI_CINE,
    )
    assert not under.is_blocked
    finding = _finding(over, VpeReasonCode.FILE_TOO_LARGE)
    assert "50.0 MiB" in finding.message


def test_pillarbox_arithmetic_is_computed_not_hardcoded():
    verdict = validate(
        _video_probe(width=1000, height=2000),
        VpeCapabilityId.PERF_ESTIMATION,
    )
    finding = _finding(verdict, VpeReasonCode.PORTRAIT_NOT_SUPPORTED)
    # 1000x2000 fits 1280x720 as 360x720, leaving 920 px of bars.
    assert "360x720" in finding.message
    assert "920 px" in finding.message
    assert "28%" in finding.message


def test_portrait_suppresses_the_redundant_frame_size_finding():
    verdict = validate(
        _video_probe(width=720, height=1280),
        VpeCapabilityId.VIDEO_TRANSFORM,
    )
    assert verdict.has(VpeReasonCode.PORTRAIT_NOT_SUPPORTED)
    assert not verdict.has(VpeReasonCode.FRAME_SIZE_UNSUPPORTED)


def test_unknown_mime_type_is_blocked():
    verdict = validate(
        _video_probe(
            video_codec="vp9", container="matroska,webm", mime_type=None
        ),
        VpeCapabilityId.UPSCALE,
    )
    finding = _finding(verdict, VpeReasonCode.MIME_TYPE_UNSUPPORTED)
    assert "vp9 in matroska,webm" in finding.message


# --- verdict plumbing ----------------------------------------------------


def test_capability_may_be_given_as_an_entry_an_enum_or_a_string():
    probe = _video_probe(frame_count=240)
    entry = get_capability(VpeCapabilityId.UPSCALE)
    verdicts = [
        validate(probe, entry),
        validate(probe, VpeCapabilityId.UPSCALE),
        validate(probe, "upscale"),
    ]
    assert {verdict.codes for verdict in verdicts} == {
        (VpeReasonCode.FRAME_COUNT_TOO_HIGH,),
    }


def test_unknown_capability_is_rejected():
    with pytest.raises(ValueError, match="Unknown VPE capability"):
        validate(_video_probe(), "definitely-not-a-capability")


def test_verdict_splits_blocking_from_advisory_findings():
    verdict = validate(
        _video_probe(frame_count=240, has_audio=True, audio_codec="aac"),
        VpeCapabilityId.UPSCALE,
    )
    assert verdict.is_blocked
    assert verdict.severity is VpeSeverity.BLOCK
    assert len(verdict.blocking) == 1
    assert len(verdict.warnings) == 1
    assert verdict.blocking[0].code is VpeReasonCode.FRAME_COUNT_TOO_HIGH
    assert verdict.findings[0] is verdict.blocking[0]
    assert verdict.summary().startswith("This clip is 10s (240 frames)")
    assert verdict.summary().endswith("(+1 more)")


def test_severity_ranks_are_ordered():
    assert VpeSeverity.BLOCK.rank > VpeSeverity.WARN.rank
    assert VpeSeverity.WARN.rank > VpeSeverity.OK.rank
