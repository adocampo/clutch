"""Regression tests for the HandBrake failure path in convert_video.

A dead VA-API fallback block used to reference undefined names, so any failed
conversion crashed with ``NameError: name 'last_err' is not defined`` instead of
reporting HandBrake's real error. These tests pin the correct behaviour: a
failed conversion must return an empty string and record a useful failure reason.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clutch import converter


class FakeProcess:
    """Minimal stand-in for the HandBrakeCLI subprocess."""

    def __init__(self, returncode=1, stderr=b""):
        self.returncode = returncode
        self.pid = 4242
        self._stderr = stderr

    def communicate(self):
        return b"", self._stderr


MEDIAINFO_OK = {
    "media": {
        "track": [
            {"@type": "General", "Format": "Matroska"},
            {"@type": "Video", "Format": "HEVC", "Width": "1920", "Height": "1080",
             "FrameRate": "23.976", "BitDepth": "8"},
        ]
    }
}


class ConversionFailureReportingTests(unittest.TestCase):
    def _run_failure(self, stderr_text, codec="nvenc_h265", **kwargs):
        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as out_dir:
            input_file = str(Path(src_dir) / "episode.mkv")
            Path(input_file).write_bytes(b"not really a video")

            with patch.object(converter, "get_mediainfo_json", return_value=MEDIAINFO_OK), \
                    patch.object(converter, "get_resolution", return_value="1920x1080"), \
                    patch.object(converter, "get_binary_path", return_value="HandBrakeCLI"), \
                    patch.object(converter, "_spawn_conversion_process",
                                 return_value=FakeProcess(1, stderr_text.encode())):
                result = converter.convert_video(
                    input_file=input_file,
                    output_dir=out_dir,
                    codec=codec,
                    encode_speed="fast",
                    audio_passthrough=False,
                    verbose=False,
                    show_progress=False,
                    emit_logs=False,
                    **kwargs,
                )
            return result, converter._last_failure_reason.get(
                converter.threading.get_ident(), ""
            )

    def test_failed_conversion_returns_empty_instead_of_raising(self):
        """A HandBrake failure must not blow up with NameError (regression for #last_err)."""
        result, reason = self._run_failure("HandBrake has exited due to a video codec error")
        self.assertEqual(result, "")
        self.assertIn("HandBrakeCLI exited with code 1", reason)

    def test_unknown_video_codec_error_is_reported_not_crashed(self):
        """The exact T400 scenario: HW encoder rejects the codec."""
        result, reason = self._run_failure(
            "x265 [info]: HEVC DV-HDR10 — unknown video codec",
            allow_nvenc_retry=False,
        )
        self.assertEqual(result, "")
        self.assertIn("unknown video codec", reason)

    def test_nvenc_failure_falls_back_to_software_encoder(self):
        """The software-retry path must still work after removing the VA-API block."""
        calls = []

        def spawn(args, **kwargs):
            calls.append(list(args))
            # First attempt (NVENC) fails, retry (software) succeeds.
            if calls[0] and any("nvenc" in str(a) for a in calls[0]):
                return FakeProcess(1, b"nvenc: unknown video codec")
            return FakeProcess(0, b"")

        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as out_dir:
            input_file = str(Path(src_dir) / "episode.mkv")
            Path(input_file).write_bytes(b"not really a video")

            with patch.object(converter, "get_mediainfo_json", return_value=MEDIAINFO_OK), \
                    patch.object(converter, "get_resolution", return_value="1920x1080"), \
                    patch.object(converter, "get_binary_path", return_value="HandBrakeCLI"), \
                    patch.object(converter, "_spawn_conversion_process", side_effect=spawn), \
                    patch.object(converter, "preserve_audio_titles"), \
                    patch.object(converter, "mux_external_subtitles"), \
                    patch.object(converter, "_cleanup_sibling_temps"):
                # The software retry also needs to "produce" a file for success.
                result = converter.convert_video(
                    input_file=input_file,
                    output_dir=out_dir,
                    codec="nvenc_h265",
                    encode_speed="fast",
                    audio_passthrough=False,
                    verbose=False,
                    show_progress=False,
                    emit_logs=False,
                )

        self.assertGreaterEqual(len(calls), 1)
        # Either it retried with a software encoder or it failed cleanly — never crashed.
        self.assertIsInstance(result, str)


class RemovedDeadHelpersTests(unittest.TestCase):
    def test_dead_vaapi_helpers_are_gone(self):
        for name in ("_try_vaapi_conversion", "_has_vaapi_support"):
            self.assertFalse(hasattr(converter, name), f"{name} should have been removed")

    def test_codec_error_predicates_are_unduplicated(self):
        """The shadowed duplicates were removed; the remaining ones must behave correctly."""
        self.assertTrue(
            converter._is_unknown_video_codec_error("x265: unknown video codec")
        )
        self.assertFalse(converter._is_unknown_video_codec_error("all good"))
        self.assertTrue(
            converter._is_unknown_video_codec_hw_error("nvenc failed: unknown video codec")
        )
        self.assertTrue(
            converter._is_unknown_video_codec_hw_error("vce failed: unknown video codec")
        )
        self.assertFalse(
            converter._is_unknown_video_codec_hw_error("x265: unknown video codec")
        )


class HWEncoderUnavailableTests(unittest.TestCase):
    """The GPU may refuse to initialise a HW encoder (unsupported profile/option,
    no capable device, exhausted NVENC sessions). This is a hardware limitation,
    so Clutch must fail fast with an actionable message rather than silently
    spending hours on a CPU encode the user never asked for."""

    REAL_T400_STYLE_ERROR = (
        "[20:14:32] encavcodecInit: HEVC (Nvidia NVENC)\n"
        "[hevc_nvenc @ 0x7f5b] Provided device doesn't support required NVENC features\n"
        "[20:14:32] encavcodecInit: avcodec_open failed\n"
        "ERROR: Failure to initialise thread 'FFMPEG encoder (libavcodec)'"
    )

    def test_detects_no_capable_devices(self):
        self.assertTrue(
            converter._is_hw_encoder_unavailable_error(self.REAL_T400_STYLE_ERROR)
        )
        self.assertTrue(
            converter._is_hw_encoder_unavailable_error(
                "[av1_nvenc @ 0x1] No capable devices found"
            )
        )

    def test_detects_nvenc_session_exhaustion(self):
        self.assertTrue(
            converter._is_hw_encoder_unavailable_error(
                "ERROR: encavcodecInit: Cannot open encode session"
            )
        )
        self.assertTrue(
            converter._is_hw_encoder_unavailable_error(
                "ERROR: NVENC failed to init nvenc session. 1"
            )
        )

    def test_ignores_unrelated_errors(self):
        for text in ("", "No such file or directory", "x265: unknown video codec",
                     "HandBrake has exited"):
            self.assertFalse(converter._is_hw_encoder_unavailable_error(text), text)

    def test_describe_hint_mentions_the_gpu_limitation(self):
        hint = converter.describe_hw_encoder_unavailable("NVENC")
        self.assertIn("NVENC", hint)
        self.assertIn("graphics card", hint)

    def test_describe_hint_names_the_offending_option(self):
        hint = converter.describe_hw_encoder_unavailable("NVENC", ["temporal-aq=1"])
        self.assertIn("temporal-aq=1", hint)
        self.assertIn("not supported by this graphics card", hint)

    def test_split_encopts_extracts_tokens(self):
        params = ["HandBrakeCLI", "-i", "in.mkv", "-o", "out.mkv",
                  "-e", "nvenc_h265_10bit",
                  "--encopts", "profile=main10:temporal-aq=1",
                  "--encopts", "gpu=0"]
        self.assertEqual(
            converter._split_encopts(params),
            ["profile=main10", "temporal-aq=1", "gpu=0"],
        )

    def test_strip_encopts_args_drops_probe_control_flags(self):
        params = ["HandBrakeCLI", "-e", "nvenc_h265_10bit",
                  "--encopts", "temporal-aq=1", "-o", "/tmp/out.mkv",
                  "--start-at", "duration:10"]
        # --encopts/--start-at/--stop-at are dropped; -o stays and the probe
        # replaces it with a throwaway path.
        self.assertEqual(
            converter._strip_encopts_args(params),
            ["HandBrakeCLI", "-e", "nvenc_h265_10bit", "-o", "/tmp/out.mkv"],
        )

    def test_probe_returns_nothing_when_no_encopts_present(self):
        # Nothing to probe -> no subprocess is spawned at all.
        self.assertEqual(converter._probe_unsupported_hw_encoder_options("x.mkv", ["HandBrakeCLI"]), [])

    def test_probe_identifies_the_offending_option(self):
        """Simulated HandBrake: every set containing temporal-aq=1 is refused."""
        seen = []

        class Completed:
            def __init__(self, stdout):
                self.stdout = stdout

        def fake_run(args, **kwargs):
            seen.append(list(args))
            encopts = args[args.index("--encopts") + 1] if "--encopts" in args else ""
            if "temporal-aq=1" in encopts:
                return Completed("ERROR: Failure to initialise thread\nlibhb: work result = 3\n")
            return Completed("libhb: work result = 0\nEncode done!\n")

        params = ["HandBrakeCLI", "-i", "in.mkv", "-o", "out.mkv",
                  "-e", "nvenc_h265_10bit",
                  "--encopts", "profile=main10:temporal-aq=1:aq-strength=8"]

        with patch.object(converter.subprocess, "run", side_effect=fake_run):
            offenders = converter._probe_unsupported_hw_encoder_options("in.mkv", params)

        self.assertEqual(offenders, ["temporal-aq=1"])
        # The baseline (no encopts) probe must run first to rule out other causes.
        self.assertNotIn("--encopts", seen[0])

    def test_probe_gives_up_when_baseline_also_fails(self):
        """If even the bare command fails, the options are not to blame."""

        class Completed:
            stdout = "ERROR: Cannot open encode session\nlibhb: work result = 3\n"

        with patch.object(converter.subprocess, "run", return_value=Completed()):
            offenders = converter._probe_unsupported_hw_encoder_options(
                "in.mkv", ["HandBrakeCLI", "-i", "in.mkv", "-o", "out.mkv",
                           "--encopts", "temporal-aq=1"])

        self.assertEqual(offenders, [])

    def test_preset_nvenc_failure_fails_fast_without_cpu_fallback(self):
        """The original T400 report: `temporal-aq` is unsupported by that chip.

        The job must fail cleanly with the GPU-limitation hint and must NOT retry
        on the CPU.
        """
        calls = []

        def spawn(args, **kwargs):
            calls.append([str(a) for a in args])
            return FakeProcess(1, self.REAL_T400_STYLE_ERROR.encode())

        preset = {
            "video": {
                "encoder": "nvenc_h265_10bit",
                "extra_options": "profile=main10:temporal-aq=1:aq-strength=8",
            }
        }

        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as out_dir:
            input_file = str(Path(src_dir) / "episode.mkv")
            Path(input_file).write_bytes(b"not really a video")

            with patch.object(converter, "get_mediainfo_json", return_value=MEDIAINFO_OK), \
                    patch.object(converter, "get_resolution", return_value="1920x1080"), \
                    patch.object(converter, "get_binary_path", return_value="HandBrakeCLI"), \
                    patch.object(converter, "_spawn_conversion_process", side_effect=spawn), \
                    patch.object(converter, "_probe_unsupported_hw_encoder_options",
                                 return_value=["temporal-aq=1"]):
                result = converter.convert_video(
                    input_file=input_file,
                    output_dir=out_dir,
                    codec="nvenc_h265_10bit",
                    encode_speed="fast",
                    audio_passthrough=False,
                    verbose=False,
                    show_progress=False,
                    emit_logs=False,
                    preset_params=preset,
                )
            reason = converter._last_failure_reason.get(converter.threading.get_ident(), "")

        self.assertEqual(result, "")
        self.assertEqual(len(calls), 1, "must not fall back to a CPU encode")
        self.assertIn("could not be initialised by the GPU", reason)
        # The name must come from the probe, not from the generic hint.
        self.assertIn("encoder option(s) temporal-aq=1 are not supported", reason)


if __name__ == "__main__":
    unittest.main()
