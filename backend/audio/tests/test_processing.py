"""Checks for the radio processing path.

Liquidsoap (config/processing.liq) is tested as a passthrough with a fixed -12 dB input
trim through its real FIFO and raw PCM output. The DSP is the FFmpeg graph in config/processing.ffmpeg, run here through
scripts/run.py's production filtergraph (including the safety limiter).

These tests run the shipped processing script through its real FIFO and raw PCM
output. They are integration checks, not a second copy of the DSP configuration.
Run them in the project image (or a Linux environment with Liquidsoap/FFmpeg):

    python3 -m unittest tests.test_processing
"""

from __future__ import annotations

import array
import importlib.util
import math
import os
import random
import re
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts" / "run.py"
PROCESSING_SCRIPT = Path(os.environ.get("RADIO_LIQ_PATH", ROOT / "config" / "processing.liq"))
SAMPLE_RATE = 44_100
CHANNELS = 2
BLOCK_FRAMES = SAMPLE_RATE // 50  # Match the supervisor's 20 ms pacing.
BLOCK_BYTES = BLOCK_FRAMES * CHANNELS * 2
LIQUIDSOAP = os.environ.get("LIQUIDSOAP_BIN") or shutil.which("liquidsoap")
# processing.liq: Spotify input trim, VBAN input trim, and the headroom on its 16-bit
# output, which the FFmpeg graph adds back as its first stage.
SPOTIFY_TRIM_DB = -12.0
VBAN_TRIM_DB = -6.0
LINK_HEADROOM_DB = -12.0
LINK_GAIN = 10 ** (LINK_HEADROOM_DB / 20)
FFMPEG = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")


def tone(
    seconds: float, frequency: float, left: float, right: float | None = None, rate: int = SAMPLE_RATE
) -> bytes:
    """Make stereo signed-16 LE sine PCM (at the production sample rate by default)."""
    right = left if right is None else right
    frames = round(seconds * rate)
    samples = array.array("h")
    for frame in range(frames):
        phase = 2.0 * math.pi * frequency * frame / rate
        samples.append(round(max(-1.0, min(1.0, left * math.sin(phase))) * 32767))
        samples.append(round(max(-1.0, min(1.0, right * math.sin(phase))) * 32767))
    if os.sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def silence(seconds: float) -> bytes:
    return bytes(round(seconds * SAMPLE_RATE) * CHANNELS * 2)


def low_noise(seconds: float, amplitude: float = 0.001) -> bytes:
    rng = random.Random(20261009)
    samples = array.array("h")
    for _ in range(round(seconds * SAMPLE_RATE * CHANNELS)):
        samples.append(round(rng.uniform(-amplitude, amplitude) * 32767))
    if os.sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


def pcm_samples(raw: bytes) -> array.array:
    usable = len(raw) - len(raw) % 2
    samples = array.array("h")
    samples.frombytes(raw[:usable])
    if os.sys.byteorder != "little":
        samples.byteswap()
    return samples


def channel_rms(samples: array.array, start_frame: int, frame_count: int, channel: int) -> float:
    start = max(0, start_frame) * CHANNELS + channel
    stop = min(len(samples) // CHANNELS, start_frame + frame_count) * CHANNELS + channel
    values = samples[start:stop:CHANNELS]
    if not values:
        return 0.0
    return math.sqrt(sum(value * value for value in values) / len(values)) / 32768.0


def rms_db(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-12))


class LiquidsoapProcessingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.name != "posix" or not hasattr(os, "mkfifo"):
            raise unittest.SkipTest("the production FIFO processor requires Linux/POSIX")
        if not LIQUIDSOAP:
            raise unittest.SkipTest("Liquidsoap is unavailable; run these tests in the project image")
        if not PROCESSING_SCRIPT.is_file():
            raise unittest.SkipTest(f"production Liquidsoap script not found: {PROCESSING_SCRIPT}")

    def run_processor(
        self,
        source_pcm: bytes,
        pace: bool = True,
        vban_enabled: bool = False,
        vban_pcm: bytes | None = None,
        agc: bool = False,
    ) -> tuple[array.array, float]:
        """Run the shipped script, feeding its configured FIFO(s) and capturing raw stdout.

        The input AGC is bypassed unless agc is true, so level tests see exact gains.

        With vban_enabled the VBAN receiver FIFO is configured; vban_pcm (stereo s16le at
        44.1 kHz) is then written to it at real-time pace, as vban_receptor would.
        """
        with tempfile.TemporaryDirectory(prefix="jmm-radio-dsp-") as temp:
            # The image runs Liquidsoap as its unprivileged service account.
            # Keep this isolated test directory traversable so a containerized
            # processor with a different non-root UID can open the bound FIFO.
            os.chmod(temp, 0o755)
            fifo = Path(temp) / "input.pcm"
            os.mkfifo(fifo, 0o666)
            env = os.environ.copy()
            env["RADIO_INPUT_FIFO"] = str(fifo)
            env["RADIO_LIQ_PATH"] = str(PROCESSING_SCRIPT)
            env.pop("VBAN_INPUT_FIFO", None)
            env["JMM_AGC_BYPASS"] = "0" if agc else "1"
            vban_fifo = Path(temp) / "vban-in.pcm"
            if vban_enabled:
                os.mkfifo(vban_fifo, 0o666)
                env["VBAN_INPUT_FIFO"] = str(vban_fifo)
            proc = subprocess.Popen(
                [LIQUIDSOAP, str(PROCESSING_SCRIPT)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
                bufsize=0,
            )
            output = bytearray()
            errors = bytearray()

            def drain(stream, target: bytearray) -> None:
                assert stream is not None
                while True:
                    chunk = stream.read(16_384)
                    if not chunk:
                        return
                    target.extend(chunk)

            stdout_thread = threading.Thread(target=drain, args=(proc.stdout, output), daemon=True)
            stderr_thread = threading.Thread(target=drain, args=(proc.stderr, errors), daemon=True)
            stdout_thread.start()
            stderr_thread.start()

            def feed_vban() -> None:
                assert vban_pcm is not None
                vban_writer = None
                deadline = time.monotonic() + 8.0
                while vban_writer is None and time.monotonic() < deadline:
                    try:
                        vban_writer = os.open(vban_fifo, os.O_WRONLY | os.O_NONBLOCK)
                    except OSError as exc:
                        if exc.errno not in (6, 11):  # ENXIO / EAGAIN
                            raise
                        time.sleep(0.025)
                if vban_writer is None:
                    return
                block = SAMPLE_RATE // 50 * CHANNELS * 2
                tick = time.monotonic()
                try:
                    for start in range(0, len(vban_pcm), block):
                        view = memoryview(vban_pcm)[start : start + block]
                        while view:
                            try:
                                view = view[os.write(vban_writer, view) :]
                            except BlockingIOError:
                                select.select([], [vban_writer], [], 0.1)
                        tick += 0.02
                        time.sleep(max(0.0, tick - time.monotonic()))
                except BrokenPipeError:
                    pass
                finally:
                    os.close(vban_writer)

            vban_thread = None
            if vban_enabled and vban_pcm is not None:
                vban_thread = threading.Thread(target=feed_vban, daemon=True)
                vban_thread.start()

            writer = None
            try:
                # The raw-audio source launches `cat FIFO` asynchronously. Wait
                # for its read end rather than blocking indefinitely at open().
                deadline = time.monotonic() + 8.0
                while writer is None and time.monotonic() < deadline:
                    if proc.poll() is not None:
                        break
                    try:
                        writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                    except OSError as exc:
                        if exc.errno not in (6, 11):  # ENXIO / EAGAIN
                            raise
                        time.sleep(0.025)
                if writer is None:
                    return_code = proc.poll()
                    self.fail(
                        f"Liquidsoap did not open its FIFO input (exit={return_code}); "
                        "stdout: " + output.decode("utf-8", "replace")[-2000:]
                        + "\nstderr: " + errors.decode("utf-8", "replace")[-2000:]
                    )

                sent_at = time.monotonic()
                offset = 0
                tick = sent_at
                while offset < len(source_pcm):
                    if proc.poll() is not None:
                        self.fail(
                            f"Liquidsoap exited while processing ({proc.returncode}); stderr: "
                            + errors.decode("utf-8", "replace")[-2000:]
                            + "\nstdout: " + output.decode("utf-8", "replace")[-2000:]
                        )
                    end = min(len(source_pcm), offset + BLOCK_BYTES)
                    view = memoryview(source_pcm)[offset:end]
                    while view:
                        try:
                            written = os.write(writer, view)
                            view = view[written:]
                        except BlockingIOError:
                            select.select([], [writer], [], 0.1)
                    offset = end
                    if pace:
                        tick += BLOCK_FRAMES / SAMPLE_RATE
                        delay = tick - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
                # Allow the bounded source/normalizer lookahead to flush its tail.
                time.sleep(0.35)
                duration = time.monotonic() - sent_at
            finally:
                if writer is not None:
                    os.close(writer)
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=2.0)
                stdout_thread.join(timeout=2.0)
                stderr_thread.join(timeout=2.0)
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()

            if proc.returncode not in (0, -signal.SIGTERM, 128 + signal.SIGTERM):
                self.fail(
                    f"Liquidsoap ended with status {proc.returncode}; stderr: "
                    + errors.decode("utf-8", "replace")[-3000:]
                )
            self.assertGreater(len(output), 0, "Liquidsoap produced no PCM")
            self.assertEqual(len(output) % 4, 0, "output is not interleaved stereo S16LE")
            return pcm_samples(bytes(output)), duration

    @staticmethod
    def output_offset(samples: array.array, input_start_seconds: float) -> int:
        """Estimate fixed pipeline delay from a tone after a known silent lead-in."""
        first_frame = None
        for frame in range(len(samples) // CHANNELS):
            if max(abs(samples[frame * CHANNELS]), abs(samples[frame * CHANNELS + 1])) > 24:
                first_frame = frame
                break
        if first_frame is None:
            return 0
        return first_frame - round(input_start_seconds * SAMPLE_RATE)

    def test_liquidsoap_is_a_passthrough_with_input_trim(self):
        lead = 0.3
        source = silence(lead) + tone(1.5, 440.0, 0.25) + silence(0.3)
        output, _ = self.run_processor(source)
        offset = self.output_offset(output, lead)
        self.assertGreaterEqual(offset, 0)
        self.assertLess(offset, round(0.15 * SAMPLE_RATE), "Liquidsoap added more than 150 ms of latency")
        start_in = round((lead + 0.5) * SAMPLE_RATE)
        span = round(0.5 * SAMPLE_RATE)
        out_db = rms_db(channel_rms(output, offset + start_in, span, 0))
        in_db = rms_db(channel_rms(pcm_samples(source), start_in, span, 0))
        self.assertLess(abs(out_db - (in_db + SPOTIFY_TRIM_DB + LINK_HEADROOM_DB)), 0.5, "the Liquidsoap stage gain is wrong")

    def test_liquidsoap_outputs_silence_for_silence(self):
        output, _ = self.run_processor(silence(1.0))
        self.assertEqual(max((abs(value) for value in output), default=0), 0)

    def test_agc_lifts_quiet_input_and_keeps_silence_silent(self):
        # -24 dBFS peak tone: -39 dBFS RMS after the -12 dB trim, above the -45 dB freeze threshold.
        source = tone(8.0, 440.0, 0.063) + silence(1.5)
        output, _ = self.run_processor(source, agc=True)
        offset = max(0, self.output_offset(output, 0.0))
        in_db = rms_db(channel_rms(pcm_samples(source), round(6.0 * SAMPLE_RATE), SAMPLE_RATE, 0)) + SPOTIFY_TRIM_DB + LINK_HEADROOM_DB
        out_db = rms_db(channel_rms(output, offset + round(6.0 * SAMPLE_RATE), SAMPLE_RATE, 0))
        self.assertGreaterEqual(out_db - in_db, 8.0, "the input AGC did not lift a quiet input")
        tail = channel_rms(output, offset + round(8.6 * SAMPLE_RATE), round(0.6 * SAMPLE_RATE), 0)
        self.assertEqual(tail, 0.0, "digital silence was not kept silent")

    def test_agc_does_not_fade_the_end_of_speech_and_holds_gain_through_a_pause(self):
        # Syllable-rate bursts, a pause below the freeze threshold, then speech again.
        def phrase(seconds: float) -> bytes:
            out = bytearray()
            for index in range(round(seconds * 4)):
                out += tone(0.17, 300.0 + 40 * (index % 3), 0.3) + silence(0.08)
            return bytes(out)

        source = phrase(6.0) + silence(3.0) + phrase(2.0)
        output, _ = self.run_processor(source, agc=True)
        offset = max(0, self.output_offset(output, 0.0))

        def level(start: float, seconds: float) -> float:
            return rms_db(channel_rms(output, offset + round(start * SAMPLE_RATE), round(seconds * SAMPLE_RATE), 0))

        middle, last = level(3.0, 2.0), level(5.5, 0.5)
        self.assertGreater(last, middle - 1.5, f"end of speech faded: middle {middle:.1f} dB, last {last:.1f} dB")
        resumed = level(9.0, 0.5)
        self.assertGreater(resumed, last - 1.5, f"gain not held through the pause: {last:.1f} -> {resumed:.1f} dB")

    def test_enabled_vban_input_without_a_sender_passes_spotify_through(self):
        lead = 0.3
        source = silence(lead) + tone(1.5, 440.0, 0.25) + silence(0.3)
        output, _ = self.run_processor(source, vban_enabled=True)
        offset = self.output_offset(output, lead)
        start_in = round((lead + 0.5) * SAMPLE_RATE)
        span = round(0.5 * SAMPLE_RATE)
        out_db = rms_db(channel_rms(output, offset + start_in, span, 0))
        in_db = rms_db(channel_rms(pcm_samples(source), start_in, span, 0))
        self.assertLess(abs(out_db - (in_db + SPOTIFY_TRIM_DB + LINK_HEADROOM_DB)), 0.5, "an idle VBAN input changed the Spotify level")

    def test_vban_input_is_mixed_with_its_trim(self):
        # Spotify is silent, so the output is the VBAN input alone,
        # with the -6 dB VBAN trim (the Spotify trim does not apply to it).
        vban = tone(3.0, 1000.0, 0.25)
        output, _ = self.run_processor(silence(2.5), vban_enabled=True, vban_pcm=vban)
        frames = len(output) // CHANNELS
        loud = [
            rms_db(channel_rms(output, start, SAMPLE_RATE // 10, 0))
            for start in range(0, frames - SAMPLE_RATE // 10, SAMPLE_RATE // 10)
        ]
        expected = rms_db(0.25 / math.sqrt(2.0)) + VBAN_TRIM_DB + LINK_HEADROOM_DB
        steady = [value for value in loud if abs(value - expected) < 0.5]
        self.assertGreaterEqual(len(steady), 10, f"VBAN input not mixed at -6 dB: {loud}")

    def test_vban_input_and_spotify_are_summed(self):
        lead = 0.3
        spotify = silence(lead) + tone(2.0, 440.0, 0.5) + silence(0.3)
        vban = tone(3.0, 1000.0, 0.25)
        output, _ = self.run_processor(spotify, vban_enabled=True, vban_pcm=vban)
        spotify_only, _ = self.run_processor(spotify)
        offset = self.output_offset(spotify_only, lead)
        start = offset + round((lead + 0.7) * SAMPLE_RATE)
        span = round(0.5 * SAMPLE_RATE)
        both = channel_rms(output, start, span, 0)
        trimmed = channel_rms(spotify_only, start, span, 0)
        # Uncorrelated tones add in power.
        vban_level = 0.25 / math.sqrt(2.0) * 10 ** ((VBAN_TRIM_DB + LINK_HEADROOM_DB) / 20)
        expected = math.sqrt(trimmed**2 + vban_level**2)
        self.assertLess(abs(rms_db(both) - rms_db(expected)), 1.0, "Spotify and VBAN were not summed")


def load_runner():
    spec = importlib.util.spec_from_file_location("jmm_run", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mixed_tones(seconds: float, frequencies: tuple[float, ...], amplitude: float) -> bytes:
    """Dense stereo test programme: several partials with a slightly different right channel."""
    frames = round(seconds * SAMPLE_RATE)
    samples = array.array("h")
    for frame in range(frames):
        left = sum(math.sin(2.0 * math.pi * f * frame / SAMPLE_RATE) for f in frequencies)
        right = sum(math.sin(2.0 * math.pi * f * frame / SAMPLE_RATE + 0.7) for f in frequencies)
        samples.append(round(max(-1.0, min(1.0, amplitude * left)) * 32767))
        samples.append(round(max(-1.0, min(1.0, amplitude * right)) * 32767))
    if os.sys.byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


class FFmpegProcessingTest(unittest.TestCase):
    """Checks on the production FFmpeg graph (config/processing.ffmpeg plus the run.py tail)."""

    @classmethod
    def setUpClass(cls) -> None:
        if not FFMPEG:
            raise unittest.SkipTest("FFmpeg is unavailable; run these tests in the project image")
        cls.runner = load_runner()
        cls.graph = cls.runner.audio_filtergraph(False)

    def run_chain(self, pcm: bytes) -> array.array:
        """Run the graph on PCM at processing level; it arrives with Liquidsoap's link headroom."""
        samples = pcm_samples(pcm)
        pcm = array.array("h", (round(value * LINK_GAIN) for value in samples)).tobytes()
        result = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "s16le", "-ar", str(SAMPLE_RATE),
             "-ac", str(CHANNELS), "-i", "pipe:0", "-filter_complex", self.graph, "-map", "[stream]",
             "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "pipe:1"],
            input=pcm, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return pcm_samples(result.stdout)

    def ebu_summary(self, pcm: bytes) -> tuple[float, float]:
        result = subprocess.run(
            [FFMPEG, "-hide_banner", "-nostdin", "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
             "-i", "pipe:0", "-af", "ebur128=peak=true", "-f", "null", "-"],
            input=pcm, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False)
        text = result.stderr.decode("utf-8", "replace")
        summary = text[text.rfind("Summary:"):]
        loudness = float(re.search(r"I:\s+(-?[\d.]+)\s+LUFS", summary).group(1))
        true_peak = float(re.search(r"True peak:\s+Peak:\s+(-?[\d.]+)\s+dBFS", summary).group(1))
        return loudness, true_peak

    def test_graph_contract_and_output_tail(self):
        graph = load_runner().load_processing_graph()
        self.assertTrue(graph.startswith("[0:a:0]"))
        self.assertIn("[proc]", graph)
        self.assertNotIn("[stream]", graph, "the processing graph must not own the output split")
        self.assertTrue(self.runner.audio_filtergraph(True).endswith("asplit=2[stream][vban]"))
        with patch.dict(os.environ, {"RADIO_FILTER": "anull"}):
            self.assertIn("[proc]anull,alimiter=limit=0.95", self.runner.audio_filtergraph(False))

    def test_dense_programme_hits_loudness_window_and_true_peak_ceiling(self):
        source = mixed_tones(12.0, (60.0, 220.0, 880.0, 3100.0, 8200.0), 0.12)
        output = self.run_chain(source)
        loudness, true_peak = self.ebu_summary(output.tobytes())
        self.assertGreaterEqual(loudness, -12.0, f"output too quiet: {loudness:.1f} LUFS")
        self.assertLessEqual(loudness, -8.0, f"output too loud: {loudness:.1f} LUFS")
        self.assertLessEqual(true_peak, -1.0, f"true peak {true_peak:.2f} dBTP exceeds -1.0 dBTP")
        self.assertGreaterEqual(true_peak - loudness, 4.0, "output is clipped or over-compressed")
        self.assertLessEqual(max(abs(value) for value in output), 0.95 * 32767 + 1,
                             "PCM exceeds the 0.95 safety ceiling")

    def test_digital_silence_stays_silent_and_quiet_tail_is_not_lifted(self):
        self.assertEqual(max((abs(v) for v in self.run_chain(silence(5.0))), default=0), 0)
        tail = low_noise(4.0, 0.0005)
        output = self.run_chain(tail)
        in_db = rms_db(channel_rms(pcm_samples(tail), 0, len(tail) // 4, 0))
        out_db = rms_db(channel_rms(output, 0, len(output) // 2, 0))
        self.assertLessEqual(out_db, in_db + 1.0, "a fade-out below the gate was pumped up")

    def test_quiet_section_is_compressed_but_not_to_full_level(self):
        # The input AGC is in Liquidsoap; this graph's compressors still narrow the gap.
        loud = tone(4.0, 440.0, 0.5)
        quiet = tone(6.0, 440.0, 0.056)
        source = loud + quiet
        output = self.run_chain(source)
        quiet_in = rms_db(channel_rms(pcm_samples(source), round(7.0 * SAMPLE_RATE), round(2.5 * SAMPLE_RATE), 0))
        quiet_out = rms_db(channel_rms(output, round(7.0 * SAMPLE_RATE), round(2.5 * SAMPLE_RATE), 0))
        loud_out = rms_db(channel_rms(output, round(2.5 * SAMPLE_RATE), round(1.5 * SAMPLE_RATE), 0))
        self.assertGreaterEqual(quiet_out - quiet_in, 3.0, "quiet section was not lifted")
        self.assertGreaterEqual(loud_out - quiet_out, 3.0, "quiet section was lifted to full programme level")

    def test_stereo_image_is_preserved(self):
        centered = tone(3.0, 440.0, 0.2)
        output = self.run_chain(centered)
        left = rms_db(channel_rms(output, round(1.0 * SAMPLE_RATE), round(1.5 * SAMPLE_RATE), 0))
        right = rms_db(channel_rms(output, round(1.0 * SAMPLE_RATE), round(1.5 * SAMPLE_RATE), 1))
        self.assertLess(abs(left - right), 0.35, "a centred input was moved off centre")
        left_only = tone(3.0, 440.0, 0.2, 0.0)
        output = self.run_chain(left_only)
        left = channel_rms(output, round(1.0 * SAMPLE_RATE), round(1.5 * SAMPLE_RATE), 0)
        right = channel_rms(output, round(1.0 * SAMPLE_RATE), round(1.5 * SAMPLE_RATE), 1)
        self.assertLess(right, left * 0.1, "left-only input leaked into the right channel")

    def test_live_latency_is_under_100_ms(self):
        """Feed the graph at real-time pace and time a loud burst from input to output.

        Guards against look-ahead filters (such as dynaudnorm, about 3.2 s) in the graph.
        """
        proc = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "s16le", "-ar", str(SAMPLE_RATE),
             "-ac", str(CHANNELS), "-probesize", "32", "-analyzeduration", "0", "-i", "pipe:0",
             "-filter_complex", self.graph, "-map", "[stream]", "-f", "s16le", "-flush_packets", "1", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        block = SAMPLE_RATE // 100  # 10 ms
        burst_at, total = 3.0, 8.0
        sent = {}

        def feed() -> None:
            start = time.monotonic()
            for index in range(round(total * 100)):
                t = index * block / SAMPLE_RATE
                delay = start + t - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                amplitude = (0.6 if t >= burst_at else 0.02) * LINK_GAIN
                rng = random.Random(index)
                samples = array.array("h", (round(rng.uniform(-amplitude, amplitude) * 32767)
                                            for _ in range(block * CHANNELS)))
                if t >= burst_at and "burst" not in sent:
                    sent["burst"] = time.monotonic()
                proc.stdin.write(samples.tobytes())
            proc.stdin.close()

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        seen = None
        try:
            while True:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                if seen is None and "burst" in sent:
                    values = array.array("h", chunk[: len(chunk) // 2 * 2])
                    if values and max(abs(value) for value in values) > 0.3 * 32767:
                        seen = time.monotonic()
        finally:
            feeder.join(timeout=total + 5)
            proc.wait(timeout=10)
        self.assertIsNotNone(seen, "the burst never reached the output")
        latency_ms = (seen - sent["burst"]) * 1000.0
        self.assertLess(latency_ms, 100.0, f"live processing latency {latency_ms:.0f} ms (target < 100 ms)")

    def test_ffmpeg_stage_latency_is_bounded(self):
        lead = 0.5
        source = silence(lead) + tone(0.01, 2000.0, 0.5) + silence(1.0)
        output = self.run_chain(source)
        threshold = 0.1 * 32767
        in_onset = round(lead * SAMPLE_RATE)
        out_onset = next(i for i in range(len(output) // 2) if abs(output[2 * i]) > threshold)
        latency = out_onset - in_onset
        self.assertGreaterEqual(latency, 0)
        self.assertLessEqual(latency / SAMPLE_RATE, 0.15, "FFmpeg processing latency exceeds 150 ms")


if __name__ == "__main__":
    unittest.main()
