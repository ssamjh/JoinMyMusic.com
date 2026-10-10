"""Hot-swap through real FFmpeg: a processor replaced by one with different makeup gain.

The stream is silence, then a paced 440 Hz tone that starts before the swap. The encoder's
monitor output (after the safety limiter) is what gets checked: the level must change by the
makeup difference, the stream must have no gap, and the tone onset gives the processing
latency. Skipped when FFmpeg is not installed.
"""

from __future__ import annotations

import array
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PROCESSING = ROOT / "config" / "processing.ffmpeg"
sys.path.insert(0, str(SCRIPTS))
import bridge as B  # noqa: E402
import run as production  # noqa: E402

FFMPEG = shutil.which("ffmpeg")
SAMPLE_RATE = 44_100
BLOCK = B.BLOCK_FRAMES
ONSET = SAMPLE_RATE * 3 // 10  # input frame where the tone starts, before the swap
SWAP_AT = 3.0  # seconds after the start: the replacement is offered here
END_AT = 7.5  # seconds after the start: the stream stops here


def tone_block(index: int) -> bytes:
    """One 10 ms block of s16le stereo: silence, then a 440 Hz tone at -18 dBFS from ONSET on."""
    values = array.array("h")
    for frame in range(index * BLOCK, (index + 1) * BLOCK):
        sample = 0
        if frame >= ONSET:
            sample = int(0.12 * 32767 * math.sin(2 * math.pi * 440 * frame / SAMPLE_RATE))
        values.append(sample)
        values.append(sample)
    if sys.byteorder == "big":
        values.byteswap()
    return values.tobytes()


def level_db(samples: list[int], start: int, end: int) -> float:
    span = samples[start:end]
    mean_square = sum(value * value for value in span) / max(1, len(span))
    return 10 * math.log10(max(mean_square / 32768.0 ** 2, 1e-12))


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class HotSwapFFmpegTest(unittest.TestCase):
    def test_swap_changes_the_level_without_a_gap_and_reports_latency(self):
        with tempfile.TemporaryDirectory(prefix="jmm-hotswap-") as temp:
            work = Path(temp)
            source = PROCESSING.read_text(encoding="utf-8")
            second_source = source.replace("volume@makeup=8dB", "volume@makeup=2dB")
            self.assertNotEqual(second_source, source, "the makeup gain to change is missing from processing.ffmpeg")
            env = {
                "FFMPEG_BIN": FFMPEG,
                "RTMP_URL": str(work / "stream.flv"),
                "ICECAST_URL": str(work / "stream.aac"),
                "FFMPEG_LOGLEVEL": "error",
            }
            with patch.dict(os.environ, env):
                monitor_read, monitor_write = os.pipe()
                encoder_argv = production.encoder_args(monitor_fd=monitor_write)
                first_argv = production.processor_args(production.processing_graph_text(source))
                second_argv = production.processor_args(production.processing_graph_text(second_source))
            encoder = subprocess.Popen(
                encoder_argv,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(monitor_write,),
                env=dict(os.environ, **env),
            )
            os.close(monitor_write)
            liquidsoap_read, liquidsoap_write = os.pipe()
            failures: list[str] = []
            bridge = B.Bridge(
                liquidsoap_read,
                encoder.stdin.fileno(),
                on_failure=lambda processor, reason: failures.append(f"{processor.label}: {reason}"),
            )
            monitor = bytearray()
            stop_feed = threading.Event()

            def drain_monitor() -> None:
                while True:
                    chunk = os.read(monitor_read, 65536)
                    if not chunk:
                        return
                    monitor.extend(chunk)

            def feed() -> None:
                started = time.monotonic()
                index = 0
                while not stop_feed.is_set():
                    delay = started + index * 0.01 - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    try:
                        os.write(liquidsoap_write, tone_block(index))
                    except OSError:
                        return
                    index += 1

            drain = threading.Thread(target=drain_monitor, daemon=True)
            feeder = threading.Thread(target=feed, daemon=True)
            drain.start()
            feeder.start()
            first = B.Processor(first_argv, env=dict(os.environ, **env), label="before")
            second = None
            try:
                bridge.start()
                bridge.offer(first, warmup_frames=0, crossfade_frames=0)
                bridge.wait_outcome(first, timeout=20)
                started = time.monotonic()
                time.sleep(max(0.0, SWAP_AT - (time.monotonic() - started)))
                second = B.Processor(second_argv, env=dict(os.environ, **env), label="after")
                bridge.wait_retired(5)
                bridge.offer(second, warmup_frames=SAMPLE_RATE, crossfade_frames=B.CROSSFADE_FRAMES)
                bridge.wait_outcome(second, timeout=30)
                time.sleep(max(0.0, END_AT - (time.monotonic() - started)))
            finally:
                stop_feed.set()
                feeder.join(timeout=3)
                bridge.stop()
                for processor in (first, second):
                    if processor is not None:
                        processor.stop()
                time.sleep(0.2)
                os.close(liquidsoap_write)
                encoder.stdin.close()
                encoder.wait(timeout=10)
                drain.join(timeout=5)
            stderr = encoder.stderr.read().decode(errors="replace")
            self.assertEqual(failures, [], "the swap reported a failure")
            self.assertEqual(encoder.returncode, 0, stderr[-500:])

            values = array.array("h")
            values.frombytes(bytes(monitor[: len(monitor) // 4 * 4]))
            left = list(values[0::2])
            before = level_db(left, int(1.5 * SAMPLE_RATE), int(2.8 * SAMPLE_RATE))
            after = level_db(left, int(5.0 * SAMPLE_RATE), int(6.8 * SAMPLE_RATE))
            print(f"\nlevel before {before:.2f} dBFS, after {after:.2f} dBFS, change {after - before:.2f} dB")
            self.assertLess(after - before, -2.0, "the makeup change did not reach the encoder output")
            self.assertGreater(after - before, -12.0, "the level dropped further than the makeup change explains")

            # No gap: the 40 ms RMS never collapses while the tone plays, across the swap.
            span_start, span_end = int(1.0 * SAMPLE_RATE), int(6.8 * SAMPLE_RATE)
            step = 4 * BLOCK
            rms = [level_db(left, i, i + step) for i in range(span_start, span_end - step, step)]
            median = sorted(rms)[len(rms) // 2]
            self.assertGreater(min(rms), median - 12.0, f"a gap of about {median - min(rms):.1f} dB in the stream")
            run = longest = 0
            for value in left[span_start:span_end]:
                run = run + 1 if abs(value) <= 2 else 0
                longest = max(longest, run)
            self.assertLess(longest, 5, "the stream went silent for several frames")

            # Processing latency: the tone onset in the monitor output against its input frame,
            # plus the bridge's emit delay. Onsets are measured on the output before the swap.
            onset_out = next(i for i, value in enumerate(left) if abs(value) > 0.1 * 32767)
            latency = onset_out - (ONSET + B.EMIT_DELAY_FRAMES)
            print(f"processing latency before the swap: {latency} frames ({latency / SAMPLE_RATE * 1000:.1f} ms)")
            self.assertGreaterEqual(latency, 0)
            self.assertLess(latency, SAMPLE_RATE // 5, "processing latency above 200 ms")


if __name__ == "__main__":
    unittest.main()
