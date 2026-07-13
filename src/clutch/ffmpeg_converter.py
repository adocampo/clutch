"""FFmpeg VA-API encoding backend.

This module provides a complete ffmpeg-based video conversion path using
VA-API hardware acceleration. It is activated automatically when HandBrake
VCE fails (or is unavailable) but VA-API hardware encoding works.

Supported encoders: ``hevc_vaapi``, ``h264_vaapi``, ``av1_vaapi``.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional

from clutch.output import debug, info, warning, error, success, skip
from clutch.mediainfo import get_mediainfo_json, get_resolution, get_audio_info

# ---------------------------------------------------------------------------
# VA-API availability detection
# ---------------------------------------------------------------------------

_vaapi_available_cache: Optional[bool] = None
_vaapi_encoders_cache: Optional[Dict[str, bool]] = None
_VAAPI_RENDER_NODE = "/dev/dri/renderD128"


def _find_render_node() -> str:
    """Return the first available DRI render node, preferring renderD128."""
    if os.path.exists(_VAAPI_RENDER_NODE):
        return _VAAPI_RENDER_NODE
    import glob as _glob
    for node in sorted(_glob.glob("/dev/dri/renderD*")):
        return node
    return ""


def _probe_vaapi_encoders() -> Dict[str, bool]:
    """Probe which VA-API encoders ffmpeg exposes."""
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return {}

    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    encoders: Dict[str, bool] = {}
    for name in ("hevc_vaapi", "h264_vaapi", "av1_vaapi"):
        encoders[name] = bool(re.search(rf'\b{name}\b', output))
    return encoders


def _vaapi_smoke_test(render_node: str) -> bool:
    """Run a minimal VA-API encode to verify the encoder actually works."""
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return False

    try:
        with tempfile.NamedTemporaryFile(suffix=".mkv", delete=False) as tmp:
            tmp_path = tmp.name

        result = subprocess.run(
            [
                ffmpeg_path, "-y",
                "-vaapi_device", render_node,
                "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=24",
                "-vf", "format=nv12,hwupload",
                "-c:v", "hevc_vaapi",
                "-frames:v", "5",
                tmp_path,
            ],
            capture_output=True, text=True, timeout=15, check=False,
        )
        ok = result.returncode == 0 and os.path.getsize(tmp_path) > 0
        debug(f"VA-API smoke test: exit_code={result.returncode}, ok={ok}")
        return ok
    except Exception as exc:
        debug(f"VA-API smoke test exception: {exc}")
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def is_vaapi_available() -> bool:
    """Return whether VA-API encoding is functional on this system.

    Checks: render node exists, ffmpeg has vaapi encoders, smoke-test passes.
    Result is cached for the process lifetime.
    """
    global _vaapi_available_cache
    if _vaapi_available_cache is not None:
        return _vaapi_available_cache

    render_node = _find_render_node()
    if not render_node:
        debug("VA-API: no render node found under /dev/dri/")
        _vaapi_available_cache = False
        return False

    encoders = get_vaapi_encoders()
    if not any(encoders.values()):
        debug("VA-API: ffmpeg has no vaapi encoders")
        _vaapi_available_cache = False
        return False

    _vaapi_available_cache = _vaapi_smoke_test(render_node)
    if not _vaapi_available_cache:
        warning("VA-API encoders detected but smoke-test failed. VA-API disabled.")
    else:
        debug(f"VA-API available: render_node={render_node}, encoders={encoders}")
    return _vaapi_available_cache


def get_vaapi_encoders() -> Dict[str, bool]:
    """Return which VA-API encoders are available (cached)."""
    global _vaapi_encoders_cache
    if _vaapi_encoders_cache is None:
        _vaapi_encoders_cache = _probe_vaapi_encoders()
    return dict(_vaapi_encoders_cache)


def reset_vaapi_cache():
    """Clear the VA-API availability cache (useful for testing)."""
    global _vaapi_available_cache, _vaapi_encoders_cache
    _vaapi_available_cache = None
    _vaapi_encoders_cache = None


# ---------------------------------------------------------------------------
# Encoder / codec mapping
# ---------------------------------------------------------------------------

# Maps clutch codec names to ffmpeg VA-API encoder names
VAAPI_ENCODER_MAP: Dict[str, str] = {
    "vaapi_h265": "hevc_vaapi",
    "vaapi_hevc": "hevc_vaapi",
    "vaapi_h264": "h264_vaapi",
    "vaapi_av1": "av1_vaapi",
}


def is_vaapi_codec(codec: str) -> bool:
    """Return whether a codec name refers to a VA-API encoder."""
    return str(codec or "").strip().lower() in VAAPI_ENCODER_MAP


def resolve_vaapi_encoder(codec: str) -> str:
    """Map a clutch vaapi codec name to the ffmpeg encoder name."""
    return VAAPI_ENCODER_MAP.get(str(codec or "").strip().lower(), "hevc_vaapi")


# ---------------------------------------------------------------------------
# VideoToolbox (Apple Silicon) detection and mapping
# ---------------------------------------------------------------------------

_vt_available_cache: Optional[bool] = None
_vt_encoders_cache: Optional[Dict[str, bool]] = None

# Maps clutch codec names to ffmpeg VideoToolbox encoder names
VT_ENCODER_MAP: Dict[str, str] = {
    "vt_h265": "hevc_videotoolbox",
    "vt_hevc": "hevc_videotoolbox",
    "vt_h264": "h264_videotoolbox",
}


def _is_macos() -> bool:
    """Return whether we're running on macOS."""
    import platform
    return platform.system() == "Darwin"


def _is_apple_silicon() -> bool:
    """Return whether we're running on Apple Silicon (arm64 macOS)."""
    import platform
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def _probe_vt_encoders() -> Dict[str, bool]:
    """Probe which VideoToolbox encoders ffmpeg exposes."""
    if not _is_macos():
        return {}

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return {}

    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}

    output = (result.stdout or "") + "\n" + (result.stderr or "")
    encoders: Dict[str, bool] = {}
    for name in ("hevc_videotoolbox", "h264_videotoolbox"):
        encoders[name] = bool(re.search(rf'\b{name}\b', output))
    return encoders


def _vt_smoke_test() -> bool:
    """Run a minimal VideoToolbox encode to verify the encoder works."""
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return False

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name

        result = subprocess.run(
            [
                ffmpeg_path, "-y", "-nostdin",
                "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=24",
                "-c:v", "hevc_videotoolbox",
                "-frames:v", "5",
                tmp_path,
            ],
            capture_output=True, text=True, timeout=15, check=False,
        )
        ok = result.returncode == 0 and os.path.getsize(tmp_path) > 0
        debug(f"VideoToolbox smoke test: exit_code={result.returncode}, ok={ok}")
        return ok
    except Exception as exc:
        debug(f"VideoToolbox smoke test exception: {exc}")
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def is_vt_available() -> bool:
    """Return whether VideoToolbox encoding is functional on this system.

    Checks: macOS platform, ffmpeg has videotoolbox encoders, smoke-test passes.
    Result is cached for the process lifetime.
    """
    global _vt_available_cache
    if _vt_available_cache is not None:
        return _vt_available_cache

    if not _is_macos():
        _vt_available_cache = False
        return False

    encoders = get_vt_encoders()
    if not any(encoders.values()):
        debug("VideoToolbox: ffmpeg has no videotoolbox encoders")
        _vt_available_cache = False
        return False

    _vt_available_cache = _vt_smoke_test()
    if not _vt_available_cache:
        warning("VideoToolbox encoders detected but smoke-test failed.")
    else:
        debug(f"VideoToolbox available: encoders={encoders}")
    return _vt_available_cache


def get_vt_encoders() -> Dict[str, bool]:
    """Return which VideoToolbox encoders are available (cached)."""
    global _vt_encoders_cache
    if _vt_encoders_cache is None:
        _vt_encoders_cache = _probe_vt_encoders()
    return dict(_vt_encoders_cache)


def is_vt_codec(codec: str) -> bool:
    """Return whether a codec name refers to a VideoToolbox encoder."""
    return str(codec or "").strip().lower() in VT_ENCODER_MAP


def resolve_vt_encoder(codec: str) -> str:
    """Map a clutch vt codec name to the ffmpeg encoder name."""
    return VT_ENCODER_MAP.get(str(codec or "").strip().lower(), "hevc_videotoolbox")


# ---------------------------------------------------------------------------
# Source analysis helpers
# ---------------------------------------------------------------------------

def _get_source_info(input_file: str, media_info_data: Optional[dict] = None) -> dict:
    """Extract source video properties needed for ffmpeg command construction."""
    if media_info_data is None:
        media_info_data = get_mediainfo_json(input_file)

    result = {
        "bit_depth": 8,
        "is_hdr": False,
        "width": 0,
        "height": 0,
        "duration_seconds": 0.0,
    }

    tracks = (media_info_data or {}).get("media", {}).get("track", [])
    for t in tracks:
        if t.get("@type") == "Video":
            try:
                result["bit_depth"] = int(t.get("BitDepth", 8))
            except (TypeError, ValueError):
                result["bit_depth"] = 8
            try:
                result["width"] = int(t.get("Width", 0))
            except (TypeError, ValueError):
                pass
            try:
                result["height"] = int(t.get("Height", 0))
            except (TypeError, ValueError):
                pass

            # HDR detection: check transfer characteristics and colour primaries
            transfer = str(t.get("transfer_characteristics", "") or t.get("TransferCharacteristics", "")).lower()
            primaries = str(t.get("colour_primaries", "") or t.get("ColorPrimaries", "")).lower()
            if "pq" in transfer or "smpte 2084" in transfer or "bt.2020" in primaries or "smpte st 2084" in transfer:
                result["is_hdr"] = True
            break

    for t in tracks:
        if t.get("@type") == "General":
            try:
                dur = float(t.get("Duration", 0))
                result["duration_seconds"] = dur
            except (TypeError, ValueError):
                pass
            break

    return result


# ---------------------------------------------------------------------------
# FFmpeg command building
# ---------------------------------------------------------------------------

# Quality mapping: VA-API uses QP (quantization parameter) instead of CRF.
# Lower QP = better quality. Rough CRF→QP mapping for HEVC:
#   CRF 18 ≈ QP 18, CRF 22 ≈ QP 22, CRF 28 ≈ QP 28
# The mapping is roughly 1:1 for HEVC; for H.264 it's also close.
DEFAULT_QP = 22


def build_ffmpeg_command(
    input_file: str,
    output_file: str,
    *,
    encoder: str = "hevc_vaapi",
    qp: int = DEFAULT_QP,
    max_width: int = 0,
    max_height: int = 0,
    audio_mode: str = "encode",
    audio_encoder: str = "libopus",
    audio_bitrate: str = "",
    subtitle_mode: str = "all",
    source_info: Optional[dict] = None,
    render_node: str = "",
    start_at_seconds: float = 0.0,
    preset_params: Optional[dict] = None,
) -> List[str]:
    """Build the complete ffmpeg command for VA-API encoding.

    Returns the full argument list including ``ffmpeg``.
    """
    if not render_node:
        render_node = _find_render_node() or _VAAPI_RENDER_NODE

    is_10bit = False
    if source_info:
        is_10bit = source_info.get("bit_depth", 8) > 8 or source_info.get("is_hdr", False)

    # Pixel format for hwupload
    pix_fmt = "p010" if is_10bit else "nv12"

    cmd: List[str] = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-y",  # overwrite output
        "-nostdin",  # disable interactive commands
        "-vaapi_device", render_node,
        # Hardware-accelerated decode: decode on GPU, output vaapi surfaces
        "-hwaccel", "vaapi",
        "-hwaccel_output_format", "vaapi",
    ]

    # Seek before input for efficiency
    if start_at_seconds > 0:
        cmd.extend(["-ss", f"{start_at_seconds:.3f}"])

    cmd.extend(["-i", input_file])

    # Explicit stream mapping: video first, then audio (added in _build_audio_args)
    cmd.extend(["-map", "0:v"])

    # Video filter chain — with hwaccel decode, frames arrive as vaapi surfaces
    # so we only need scale_vaapi (no format+hwupload needed for decode→encode on GPU)
    vf_parts = []

    # Scale filter (if resolution limit is set)
    # With hwaccel vaapi decode, frames arrive as vaapi surfaces — no hwupload needed.
    # Only scale_vaapi is needed for resizing; for passthrough, no video filter at all.
    if max_width > 0 or max_height > 0:
        w = str(max_width) if max_width > 0 else "-1"
        h = str(max_height) if max_height > 0 else "-1"
        vf_parts.append(f"scale_vaapi=w={w}:h={h}:format={pix_fmt}")
    else:
        # No scaling needed — just ensure the right pixel format for the encoder
        vf_parts.append(f"scale_vaapi=format={pix_fmt}")

    cmd.extend(["-vf", ",".join(vf_parts)])

    # Video encoder
    cmd.extend(["-c:v", encoder])
    cmd.extend(["-qp", str(qp)])

    # Apply preset params overrides if provided
    if preset_params and isinstance(preset_params.get("video"), dict):
        video_cfg = preset_params["video"]
        quality_mode = str(video_cfg.get("quality_mode", "crf")).strip()
        quality_value = video_cfg.get("quality_value")
        try:
            qv = int(float(quality_value)) if quality_value is not None else None
        except (TypeError, ValueError):
            qv = None
        if qv is not None and qv > 0:
            if quality_mode == "abr":
                # Remove the -qp and use bitrate instead
                cmd = [a for i, a in enumerate(cmd) if not (cmd[max(0, i - 1):i + 1] == ["-qp", str(qp)])]
                # Rebuild without -qp
                idx = _find_arg_index(cmd, "-qp")
                if idx >= 0:
                    cmd.pop(idx)  # remove -qp
                    cmd.pop(idx)  # remove value
                cmd.extend(["-b:v", f"{qv}k"])
            else:
                # Replace the default QP with the preset value
                idx = _find_arg_index(cmd, "-qp")
                if idx >= 0 and idx + 1 < len(cmd):
                    cmd[idx + 1] = str(qv)

    # Audio handling
    if preset_params and isinstance(preset_params.get("audio"), dict):
        audio_cfg = preset_params["audio"]
        audio_mode = str(audio_cfg.get("mode", "encode")).strip()
        audio_encoder = str(audio_cfg.get("encoder", "opus")).strip()
        audio_br = int(audio_cfg.get("bitrate", 0) or 0)
        if audio_br > 0:
            audio_bitrate = str(audio_br)

    _build_audio_args(cmd, input_file, audio_mode, audio_encoder, audio_bitrate, source_info)

    # Subtitle handling
    if preset_params and isinstance(preset_params.get("subtitles"), dict):
        subtitle_mode = str(preset_params["subtitles"].get("mode", "all")).strip()

    _build_subtitle_args(cmd, subtitle_mode)

    # Container: Matroska
    cmd.extend(["-f", "matroska"])

    # Chapter markers (copy by default)
    cmd.extend(["-map_chapters", "0"])

    cmd.append(output_file)
    return cmd


# VideoToolbox quality: uses -q:v (1-100, higher = better quality) or -b:v for bitrate.
# Rough CRF equivalence: CRF 18 ≈ q 75, CRF 22 ≈ q 65, CRF 28 ≈ q 50
DEFAULT_VT_QUALITY = 65


def build_ffmpeg_vt_command(
    input_file: str,
    output_file: str,
    *,
    encoder: str = "hevc_videotoolbox",
    quality: int = DEFAULT_VT_QUALITY,
    max_width: int = 0,
    max_height: int = 0,
    audio_mode: str = "encode",
    audio_encoder: str = "libopus",
    audio_bitrate: str = "",
    subtitle_mode: str = "all",
    source_info: Optional[dict] = None,
    start_at_seconds: float = 0.0,
    preset_params: Optional[dict] = None,
) -> List[str]:
    """Build the complete ffmpeg command for VideoToolbox encoding (Apple Silicon).

    Returns the full argument list including ``ffmpeg``.
    """
    is_10bit = False
    if source_info:
        is_10bit = source_info.get("bit_depth", 8) > 8 or source_info.get("is_hdr", False)

    cmd: List[str] = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-y",  # overwrite output
        "-nostdin",  # disable interactive commands
        # Hardware-accelerated decode via VideoToolbox
        "-hwaccel", "videotoolbox",
    ]

    # Seek before input for efficiency
    if start_at_seconds > 0:
        cmd.extend(["-ss", f"{start_at_seconds:.3f}"])

    cmd.extend(["-i", input_file])

    # Explicit stream mapping
    cmd.extend(["-map", "0:v"])

    # Video scaling (if resolution limit is set)
    if max_width > 0 or max_height > 0:
        w = str(max_width) if max_width > 0 else "-1"
        h = str(max_height) if max_height > 0 else "-1"
        cmd.extend(["-vf", f"scale={w}:{h}"])

    # Video encoder
    cmd.extend(["-c:v", encoder])

    # VideoToolbox quality: -q:v (1-100 scale, higher = better)
    cmd.extend(["-q:v", str(quality)])

    # Enable 10-bit output for HDR sources
    if is_10bit and "hevc" in encoder:
        cmd.extend(["-tag:v", "hvc1"])
        # VT on Apple Silicon supports 10-bit HEVC natively via profile
        cmd.extend(["-profile:v", "main10"])

    # Allow hardware-accelerated encoding
    cmd.extend(["-allow_sw", "1"])  # fallback to software if HW fails

    # Apply preset params overrides if provided
    if preset_params and isinstance(preset_params.get("video"), dict):
        video_cfg = preset_params["video"]
        quality_mode = str(video_cfg.get("quality_mode", "crf")).strip()
        quality_value = video_cfg.get("quality_value")
        try:
            qv = int(float(quality_value)) if quality_value is not None else None
        except (TypeError, ValueError):
            qv = None
        if qv is not None and qv > 0:
            if quality_mode == "abr":
                # Remove -q:v and use bitrate
                idx = _find_arg_index(cmd, "-q:v")
                if idx >= 0:
                    cmd.pop(idx)  # remove -q:v
                    cmd.pop(idx)  # remove value
                cmd.extend(["-b:v", f"{qv}k"])
            else:
                # Map CRF-style value to VT quality scale
                # CRF 18→75, CRF 22→65, CRF 28→50, CRF 30→45
                vt_q = max(1, min(100, 100 - int(qv * 1.8)))
                idx = _find_arg_index(cmd, "-q:v")
                if idx >= 0 and idx + 1 < len(cmd):
                    cmd[idx + 1] = str(vt_q)

    # Audio handling
    if preset_params and isinstance(preset_params.get("audio"), dict):
        audio_cfg = preset_params["audio"]
        audio_mode = str(audio_cfg.get("mode", "encode")).strip()
        audio_encoder = str(audio_cfg.get("encoder", "opus")).strip()
        audio_br = int(audio_cfg.get("bitrate", 0) or 0)
        if audio_br > 0:
            audio_bitrate = str(audio_br)

    _build_audio_args(cmd, input_file, audio_mode, audio_encoder, audio_bitrate, source_info)

    # Subtitle handling
    if preset_params and isinstance(preset_params.get("subtitles"), dict):
        subtitle_mode = str(preset_params["subtitles"].get("mode", "all")).strip()

    _build_subtitle_args(cmd, subtitle_mode)

    # Container: Matroska
    cmd.extend(["-f", "matroska"])

    # Chapter markers (copy by default)
    cmd.extend(["-map_chapters", "0"])

    cmd.append(output_file)
    return cmd


def _find_arg_index(args: List[str], flag: str) -> int:
    """Find the index of a flag in the argument list."""
    try:
        return args.index(flag)
    except ValueError:
        return -1


def _build_audio_args(
    cmd: List[str],
    input_file: str,
    mode: str,
    encoder: str,
    bitrate: str,
    source_info: Optional[dict],
):
    """Append audio encoding arguments to the ffmpeg command."""
    # Map all audio streams
    cmd.extend(["-map", "0:a"])

    # Map the ffmpeg audio encoder name
    ffmpeg_audio_encoder = _map_audio_encoder(encoder)

    if mode == "passthrough":
        cmd.extend(["-c:a", "copy"])
    elif mode == "copy_with_fallback":
        # ffmpeg doesn't have a direct "copy with fallback" mode.
        # We use copy; if the muxer rejects it, the user re-runs with encode.
        cmd.extend(["-c:a", "copy"])
    else:
        # Encode mode
        cmd.extend(["-c:a", ffmpeg_audio_encoder])

        # Normalize channel layouts for libopus: ffmpeg 7+/8 rejects non-standard
        # layouts like 5.1(side) even with mapping_family=1. The aformat filter
        # remaps them to standard 5.1 (with back channels) before encoding.
        if ffmpeg_audio_encoder == "libopus":
            cmd.extend(["-af", "aformat=channel_layouts=7.1|5.1|stereo|mono"])
            cmd.extend(["-mapping_family", "1"])

        if not bitrate:
            # Auto bitrate based on source channels
            audio_info = get_audio_info(input_file)
            if audio_info:
                # Use highest channel count to set bitrate
                max_channels = max(int(t.get("Channels", 2)) for t in audio_info)
                if max_channels <= 2:
                    bitrate = "128k"
                elif max_channels <= 6:
                    bitrate = "256k"
                else:
                    bitrate = "320k"
            else:
                bitrate = "128k"

        if not bitrate.endswith("k"):
            bitrate = f"{bitrate}k"
        cmd.extend(["-b:a", bitrate])


def _map_audio_encoder(encoder: str) -> str:
    """Map clutch audio encoder name to ffmpeg encoder name."""
    mapping = {
        "opus": "libopus",
        "aac": "aac",
        "ac3": "ac3",
        "eac3": "eac3",
        "flac": "flac",
        "mp3": "libmp3lame",
    }
    return mapping.get(encoder.lower(), "libopus")


def _build_subtitle_args(cmd: List[str], mode: str):
    """Append subtitle handling arguments to the ffmpeg command."""
    if mode == "all":
        cmd.extend(["-map", "0:s?", "-c:s", "copy"])
    elif mode == "first":
        cmd.extend(["-map", "0:s:0?", "-c:s", "copy"])
    # "none" → no subtitle mapping


# ---------------------------------------------------------------------------
# Progress parsing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)")
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+)\.(\d+)")
_FPS_RE = re.compile(r"fps=\s*([\d.]+)")
_SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")


def _parse_time_to_seconds(hours: str, minutes: str, seconds: str, centis: str) -> float:
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(centis) / 100.0


def parse_ffmpeg_progress(line: str, duration_seconds: float) -> Optional[float]:
    """Parse an ffmpeg stderr line and return progress percentage (0–100), or None."""
    m = _TIME_RE.search(line)
    if not m:
        return None
    current = _parse_time_to_seconds(*m.groups())
    if duration_seconds <= 0:
        return None
    return min(100.0, (current / duration_seconds) * 100.0)


def parse_ffmpeg_duration(line: str) -> Optional[float]:
    """Parse the Duration line from ffmpeg output. Returns seconds or None."""
    m = _DURATION_RE.search(line)
    if not m:
        return None
    return _parse_time_to_seconds(*m.groups())


# ---------------------------------------------------------------------------
# Conversion entry point
# ---------------------------------------------------------------------------

def convert_video_ffmpeg(
    input_file: str,
    output_dir: str,
    codec: str,
    *,
    encode_speed: str = "normal",
    audio_passthrough: bool = False,
    verbose: bool = False,
    resolution_override: str = "",
    show_progress: bool = True,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    emit_logs: bool = True,
    progress_log_path: str = "",
    detach_when: Optional[Callable[[], bool]] = None,
    runtime_callback: Optional[Callable[[dict], None]] = None,
    output_base_dir: str = "",
    preset_params: Optional[dict] = None,
    start_at_seconds: float = 0.0,
) -> str:
    """Convert a video file using ffmpeg with hardware acceleration (VA-API or VideoToolbox).

    Returns the path to the output file on success, or "" on failure.
    This function mirrors the interface of converter.convert_video() for the
    subset of parameters that apply to ffmpeg-based encoding.
    """
    from clutch.converter import (
        _get_conversion_state,
        _update_conversion_state,
        _is_conversion_interrupted,
        _clear_conversion_interrupt,
        _set_failure_reason,
        _remove_temp_and_log,
        build_output_subdir,
        generate_unique_filename,
        preserve_audio_titles,
        mux_external_subtitles,
        _cleanup_sibling_temps,
        ConversionDetached,
    )

    thread_id = threading.get_ident()
    media_info_data = get_mediainfo_json(input_file)
    source_info = _get_source_info(input_file, media_info_data)

    # Determine resolution
    if resolution_override:
        resolution = resolution_override
    else:
        resolution = get_resolution(input_file, data=media_info_data)
        if not resolution:
            _set_failure_reason(f"Could not determine video resolution for {os.path.basename(input_file)}.")
            return ""

    # Parse resolution for scaling limits
    max_width = 0
    max_height = 0
    if preset_params and isinstance(preset_params.get("video"), dict):
        max_width = int(preset_params["video"].get("max_width", 0) or 0)
        max_height = int(preset_params["video"].get("max_height", 0) or 0)

    # Determine quality
    qp = DEFAULT_QP
    vt_quality = DEFAULT_VT_QUALITY
    if preset_params and isinstance(preset_params.get("video"), dict):
        qv = preset_params["video"].get("quality_value")
        try:
            qp = int(float(qv)) if qv is not None and float(qv) > 0 else DEFAULT_QP
            # Map CRF-style value to VT quality scale
            vt_quality = max(1, min(100, 100 - int(qp * 1.8)))
        except (TypeError, ValueError):
            pass

    # Detect backend: VideoToolbox or VA-API
    _use_vt = is_vt_codec(codec)

    # Resolve ffmpeg encoder name
    if _use_vt:
        ffmpeg_encoder = resolve_vt_encoder(codec)
        backend_label = "ffmpeg VideoToolbox"
    else:
        ffmpeg_encoder = resolve_vaapi_encoder(codec)
        backend_label = "ffmpeg VA-API"

    # Audio settings
    audio_mode = "passthrough" if audio_passthrough else "encode"
    audio_encoder = "opus"
    audio_bitrate = ""
    subtitle_mode = "all"

    # Output path
    output_subdir = build_output_subdir(input_file, output_dir, base_dir=output_base_dir)
    os.makedirs(output_subdir, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(input_file))[0]

    if output_dir:
        final_output = generate_unique_filename(base_name, "mkv", output_subdir)
    else:
        final_output = generate_unique_filename(f"{base_name}_converted", "mkv", output_subdir)

    # Temp file
    with tempfile.NamedTemporaryFile(
        dir=output_subdir, prefix=f"{base_name}.tmp.mkv.", delete=False
    ) as tf:
        temp_filepath = tf.name

    _update_conversion_state(
        thread_id,
        temp_file=temp_filepath,
        process=None,
        pid=None,
        interrupted=False,
        paused=False,
        paused_at=None,
        paused_seconds=0.0,
    )

    # Build ffmpeg command
    if _use_vt:
        ffmpeg_cmd = build_ffmpeg_vt_command(
            input_file,
            temp_filepath,
            encoder=ffmpeg_encoder,
            quality=vt_quality,
            max_width=max_width,
            max_height=max_height,
            audio_mode=audio_mode,
            audio_encoder=audio_encoder,
            audio_bitrate=audio_bitrate,
            subtitle_mode=subtitle_mode,
            source_info=source_info,
            start_at_seconds=start_at_seconds,
            preset_params=preset_params,
        )
    else:
        ffmpeg_cmd = build_ffmpeg_command(
            input_file,
            temp_filepath,
            encoder=ffmpeg_encoder,
            qp=qp,
            max_width=max_width,
            max_height=max_height,
            audio_mode=audio_mode,
            audio_encoder=audio_encoder,
            audio_bitrate=audio_bitrate,
            subtitle_mode=subtitle_mode,
            source_info=source_info,
            start_at_seconds=start_at_seconds,
            preset_params=preset_params,
        )

    debug(f"{backend_label} command: {' '.join(str(a) for a in ffmpeg_cmd)}")

    last_progress = 0.0
    duration = source_info.get("duration_seconds", 0.0)

    def report_progress(percent: float, detail: str):
        nonlocal last_progress
        clamped = max(0.0, min(percent, 100.0))
        if clamped < last_progress:
            return
        last_progress = clamped
        if progress_callback is not None:
            progress_callback(clamped, detail)

    report_progress(0.0, f"Starting {backend_label} conversion.")

    if _is_conversion_interrupted(thread_id):
        _clear_conversion_interrupt(thread_id)
        _remove_temp_and_log(temp_filepath)
        _update_conversion_state(thread_id, temp_file=None, process=None)
        if emit_logs:
            skip(f"Conversion skipped: {os.path.basename(input_file)}")
        return ""

    try:
        process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True if os.name != "nt" else False,
        )
        _update_conversion_state(thread_id, process=process, pid=process.pid)

        if runtime_callback is not None:
            runtime_callback({
                "process_id": process.pid,
                "temp_file": temp_filepath,
                "log_file": progress_log_path or f"{temp_filepath}.progress.log",
                "final_output_file": final_output,
                "backend": "ffmpeg_vt" if _use_vt else "ffmpeg_vaapi",
            })

        if emit_logs:
            info(f"Converting ({backend_label}): {os.path.basename(input_file)}")

        # Read stderr for progress (ffmpeg writes progress to stderr)
        # Note: ffmpeg uses \r (carriage return) for progress lines, not \n.
        # We must read raw bytes and split on both \r and \n.
        conversion_succeeded = False
        error_lines: List[str] = []

        def _iter_ffmpeg_lines(stream):
            """Yield lines from ffmpeg stderr, splitting on both \\r and \\n."""
            buf = b""
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    if buf:
                        yield buf.decode("utf-8", errors="replace")
                    break
                buf += chunk
                while b"\r" in buf or b"\n" in buf:
                    # Find earliest separator
                    idx_r = buf.find(b"\r")
                    idx_n = buf.find(b"\n")
                    if idx_r == -1:
                        idx = idx_n
                    elif idx_n == -1:
                        idx = idx_r
                    else:
                        idx = min(idx_r, idx_n)
                    line = buf[:idx].decode("utf-8", errors="replace")
                    # Skip \r\n as single separator
                    if idx < len(buf) - 1 and buf[idx:idx+2] == b"\r\n":
                        buf = buf[idx+2:]
                    else:
                        buf = buf[idx+1:]
                    if line:
                        yield line

        if verbose:
            # In verbose mode, print everything
            for line in _iter_ffmpeg_lines(process.stderr):
                if line:
                    print(line)
                    pct = parse_ffmpeg_progress(line, duration)
                    if pct is not None:
                        report_progress(pct, line)
                    if duration <= 0:
                        d = parse_ffmpeg_duration(line)
                        if d:
                            duration = d
            process.wait()
            conversion_succeeded = process.returncode == 0
        elif show_progress or progress_callback:
            from tqdm import tqdm
            pbar = None
            if show_progress:
                print(f"Converting (ffmpeg VA-API): {os.path.basename(input_file)}")
                pbar = tqdm(
                    total=100,
                    dynamic_ncols=True,
                    leave=False,
                    bar_format="{percentage:3.0f}%|{bar}| [{elapsed}<{remaining}]",
                )

            try:
                for line in _iter_ffmpeg_lines(process.stderr):
                    if _is_conversion_interrupted(thread_id):
                        break

                    if detach_when is not None and detach_when():
                        from clutch.converter import request_current_conversion_pause
                        request_current_conversion_pause(thread_id)
                        raise ConversionDetached("Conversion detached from the service worker.")

                    if not line:
                        continue

                    # Parse duration from the initial output
                    if duration <= 0:
                        d = parse_ffmpeg_duration(line)
                        if d:
                            duration = d

                    pct = parse_ffmpeg_progress(line, duration)
                    if pct is not None:
                        if pbar:
                            increment = pct - pbar.n
                            if increment > 0:
                                pbar.update(increment)
                        report_progress(pct, line)
                    else:
                        stripped = line.strip()
                        if stripped and not stripped.startswith("frame="):
                            error_lines.append(stripped)

                process.wait()
                conversion_succeeded = process.returncode == 0
            finally:
                if pbar:
                    elapsed_text = tqdm.format_interval(pbar.format_dict["elapsed"])
                    pbar.close()
                else:
                    elapsed_text = None
        else:
            _, stderr_data = process.communicate()
            conversion_succeeded = process.returncode == 0
            if stderr_data:
                error_lines = stderr_data.decode("utf-8", errors="replace").strip().splitlines()[-30:]
            elapsed_text = None

        _update_conversion_state(thread_id, process=None, pid=None)

        # Check interruption
        if _is_conversion_interrupted(thread_id):
            _clear_conversion_interrupt(thread_id)
            _remove_temp_and_log(temp_filepath)
            _update_conversion_state(
                thread_id, temp_file=None, process=None, pid=None,
                paused=False, paused_at=None, paused_seconds=0.0,
            )
            if emit_logs:
                skip(f"Conversion skipped: {os.path.basename(input_file)}")
            report_progress(last_progress, "Conversion skipped.")
            return ""

        if conversion_succeeded:
            # Move temp to final output
            shutil.move(temp_filepath, final_output)
            _remove_temp_and_log(temp_filepath)

            # Validate non-empty output
            try:
                output_size = os.path.getsize(final_output)
            except OSError:
                output_size = 0
            if output_size == 0:
                try:
                    os.remove(final_output)
                except OSError:
                    pass
                _update_conversion_state(
                    thread_id, temp_file=None, process=None, pid=None,
                    paused=False, paused_at=None, paused_seconds=0.0,
                )
                if emit_logs:
                    error(f"Conversion produced empty file: {os.path.basename(input_file)}")
                _set_failure_reason(f"{backend_label} conversion produced empty file.")
                return ""

            _update_conversion_state(
                thread_id, temp_file=None, process=None, pid=None,
                paused=False, paused_at=None, paused_seconds=0.0,
            )

            if emit_logs:
                if show_progress and elapsed_text:
                    success(f"Conversion successful [{elapsed_text}]")
                else:
                    success(f"Conversion successful: {os.path.basename(final_output)}")

            # Post-processing: preserve audio titles, mux external subs, cleanup
            preserve_audio_titles(input_file, final_output, emit_logs=emit_logs)
            mux_external_subtitles(input_file, final_output, emit_logs=emit_logs)
            _cleanup_sibling_temps(final_output, base_name, os.path.dirname(final_output))
            report_progress(100.0, "Conversion successful.")
            return final_output
        else:
            _remove_temp_and_log(temp_filepath)
            _update_conversion_state(
                thread_id, temp_file=None, process=None, pid=None,
                paused=False, paused_at=None, paused_seconds=0.0,
            )
            error_detail = "\n".join(error_lines[-30:]) if error_lines else ""
            if emit_logs:
                error(f"{backend_label} conversion failed: {os.path.basename(input_file)}")
                if error_detail:
                    error(f"ffmpeg output:\n{error_detail}")
            _set_failure_reason(f"ffmpeg exited with code {process.returncode}." + (f" Detail: {error_detail[:500]}" if error_detail else ""))
            report_progress(last_progress, "Conversion failed.")
            return ""

    except ConversionDetached:
        if process is not None:
            _update_conversion_state(thread_id, process=None, pid=process.pid)
        raise
    except FileNotFoundError:
        if emit_logs:
            error("ffmpeg not found.")
        _remove_temp_and_log(temp_filepath)
        _update_conversion_state(
            thread_id, temp_file=None, process=None, pid=None,
            paused=False, paused_at=None, paused_seconds=0.0,
        )
        _set_failure_reason("ffmpeg binary not found.")
        return ""
    except Exception as exc:
        if emit_logs:
            error(f"Error during ffmpeg conversion: {exc}")
        _remove_temp_and_log(temp_filepath)
        _update_conversion_state(
            thread_id, temp_file=None, process=None, pid=None,
            paused=False, paused_at=None, paused_seconds=0.0,
        )
        _set_failure_reason(f"ffmpeg conversion error: {exc}")
        return ""
