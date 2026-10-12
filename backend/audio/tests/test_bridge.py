"""Tests for scripts/bridge.py: hot-swaps, failure handling and the Liquidsoap side never blocking.

The processors are small Python scripts (s16le in, f32le out, with an observable gain), so
the tests need no FFmpeg. Liquidsoap is replaced by a feeder that paces a known sample
pattern at real time. Frame p of the encoder output must equal the input frame p
multiplied by the gain in force for that frame, which catches gaps, duplicates and shifts.
"""

from __future__ import annotations

import array
import os
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bridge as B  # noqa: E402

PATTERN_PERIOD = 30_000
DRY = 10 ** (B.DRY_MAKEUP_DB / 20)

FAKE_PROCESSOR = r'''
import array, os, sys, time

gain = float(os.environ.get("FAKE_GAIN", "1"))
fail_start = os.environ.get("FAKE_FAIL_START") == "1"
fail_after = int(os.environ.get("FAKE_FAIL_AFTER", "0"))
stall_after = int(os.environ.get("FAKE_STALL_AFTER", "0"))
if fail_start:
    sys.stderr.write("refusing to start\n")
    sys.exit(3)
frames = 0
carry = b""
while True:
    if stall_after and frames >= stall_after:
        time.sleep(3600)  # stop reading and writing, as a wedged FFmpeg would
    data = os.read(0, 4096)
    if not data:
        break
    buf = carry + data
    usable = len(buf) - len(buf) % 4
    carry = buf[usable:]
    samples = array.array("h")
    samples.frombytes(buf[:usable])
    if sys.byteorder == "big":
        samples.byteswap()
    frames += len(samples) // 2
    if fail_after and frames >= fail_after:
        sys.stderr.write("simulated crash\n")
        sys.exit(4)
    out = array.array("f", [value / 32768.0 * gain for value in samples])
    if sys.byteorder == "big":
        out.byteswap()
    view = memoryview(out.tobytes())
    while view:
        view = view[os.write(1, view):]
'''


def pattern_block(index: int) -> bytes:
    """One 10 ms block of s16le stereo; frame k holds (k mod PATTERN_PERIOD) - 15000 on both channels."""
    values = array.array("h")
    for frame in range(index * B.BLOCK_FRAMES, (index + 1) * B.BLOCK_FRAMES):
        value = frame % PATTERN_PERIOD - 15000
        values.append(value)
        values.append(value)
    if sys.byteorder == "big":
        values.byteswap()
    return values.tobytes()


def expected_input(frame: int) -> float:
    return (frame % PATTERN_PERIOD - 15000) / 32768.0


class Harness:
    """Paced Liquidsoap stand-in, a bridge, and an encoder that records every f32 frame."""

    def __init__(self, script: Path) -> None:
        self.script = script
        self.failures: list[tuple[str, str]] = []
        self.processors: list[B.Processor] = []
        self.failure_event = threading.Event()
        self.liq_read, self.liq_write = os.pipe()
        self.enc_read, self.enc_write = os.pipe()
        self.bridge = B.Bridge(self.liq_read, self.enc_write, on_failure=self._on_failure)
        self._stop_feed = threading.Event()
        self.feed_lag = 0.0
        self.blocks_written = 0
        self._recorded = bytearray()
        self._threads: list[threading.Thread] = []

    def _on_failure(self, processor: B.Processor, reason: str) -> None:
        self.failures.append((processor.label, reason))
        self.failure_event.set()

    def processor(self, *, gain: float = 1.0, fail_start: bool = False, fail_after: int = 0,
                  stall_after: int = 0, label: str = "fake") -> B.Processor:
        env = dict(os.environ)
        env.update({
            "FAKE_GAIN": repr(gain),
            "FAKE_FAIL_START": "1" if fail_start else "0",
            "FAKE_FAIL_AFTER": str(fail_after),
            "FAKE_STALL_AFTER": str(stall_after),
        })
        processor = B.Processor([sys.executable, str(self.script)], env=env, label=label)
        self.processors.append(processor)
        return processor

    def start(self) -> None:
        feeder = threading.Thread(target=self._feed, name="fake-liquidsoap", daemon=True)
        recorder = threading.Thread(target=self._record, name="fake-encoder", daemon=True)
        self._threads = [feeder, recorder]
        feeder.start()
        recorder.start()
        self.bridge.start()

    def _feed(self) -> None:
        started = time.monotonic()
        index = 0
        while not self._stop_feed.is_set():
            scheduled = started + index * 0.01
            delay = scheduled - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            lag = time.monotonic() - scheduled
            self.feed_lag = max(self.feed_lag, lag)
            try:
                os.write(self.liq_write, pattern_block(index))
            except OSError:
                return
            index += 1
            self.blocks_written = index

    def _record(self) -> None:
        while True:
            data = os.read(self.enc_read, 65536)
            if not data:
                return
            self._recorded.extend(data)

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)

    def stop(self) -> None:
        self._stop_feed.set()
        if self._threads:
            self._threads[0].join(timeout=2)
        self.bridge.stop()
        time.sleep(0.2)
        for fd in (self.liq_write, self.enc_write):
            try:
                os.close(fd)
            except OSError:
                pass
        if self._threads:
            self._threads[1].join(timeout=2)
        for processor in self.processors:
            processor.stop()

    def output(self) -> list[float]:
        """Left-channel f32 values, one per frame."""
        values = array.array("f")
        values.frombytes(bytes(self._recorded[: len(self._recorded) // 8 * 8]))
        return list(values[0::2])


def gains_for(output: list[float]) -> list[float | None]:
    """Output divided by the input, indexed by input frame; None where the input is zero.

    The encoder's first EMIT_DELAY frames are silence, so output frame q is input frame q - delay.
    """
    delay = B.EMIT_DELAY_FRAMES
    assert all(value == 0.0 for value in output[:delay]), "the stream must start with the emit delay of silence"
    result: list[float | None] = []
    for frame in range(len(output) - delay):
        source = expected_input(frame)
        value = output[frame + delay]
        result.append(None if abs(source) < 0.01 else value / source)
    return result


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="jmm-bridge-")
        cls.script = Path(cls.temp.name) / "fake_processor.py"
        cls.script.write_text(FAKE_PROCESSOR, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def make(self) -> Harness:
        harness = Harness(self.script)
        self.addCleanup(harness.stop)
        return harness

    def assert_silent_before(self, harness: Harness, processor: B.Processor) -> None:
        """Before a processor takes over the encoder carries silence, not unprocessed audio."""
        output = harness.output()
        delay = B.EMIT_DELAY_FRAMES
        self.assertTrue(all(value == 0.0 for value in output[delay:delay + processor.start_tag]),
                        "audio appeared before the first processor started")

    def assert_gain(self, gains: list[float | None], start: int, end: int, expected: float, tolerance: float = 1e-3):
        for frame in range(start, min(end, len(gains))):
            if gains[frame] is None:
                continue
            self.assertAlmostEqual(gains[frame], expected, delta=tolerance, msg=f"frame {frame}")

    def test_swap_is_continuous_and_crossfades_over_20_ms(self):
        harness = self.make()
        harness.start()
        first = harness.processor(gain=1.0, label="first")
        harness.bridge.offer(first, warmup_frames=0, crossfade_frames=0)
        harness.bridge.wait_outcome(first, timeout=5)
        harness.wait(1.0)
        second = harness.processor(gain=0.5, label="second")
        harness.bridge.wait_retired(5)
        harness.bridge.offer(second, warmup_frames=B.SAMPLE_RATE // 4, crossfade_frames=B.CROSSFADE_FRAMES)
        harness.bridge.wait_outcome(second, timeout=5)
        harness.wait(1.0)
        harness.stop()

        gains = gains_for(harness.output())
        self.assertGreater(len(gains), 2 * B.SAMPLE_RATE, "too few frames reached the encoder")
        self.assertEqual(harness.failures, [], "a healthy swap must not report failures")
        self.assert_silent_before(harness, first)
        begin = first.start_tag
        start = next(i for i in range(begin, len(gains)) if gains[i] is not None and abs(gains[i] - 1.0) > 1e-3)
        end = start + B.CROSSFADE_FRAMES
        self.assert_gain(gains, begin, start - 2, 1.0)
        self.assert_gain(gains, end + 2, len(gains), 0.5)
        ramp = [gains[i] for i in range(start - 2, end + 2) if gains[i] is not None]
        self.assertTrue(all(a >= b - 1e-6 for a, b in zip(ramp, ramp[1:])), "crossfade is not monotonic")
        self.assertGreater(min(ramp), 0.49, "crossfade overshoots the new gain")
        self.assertLess(harness.feed_lag, 0.1, "Liquidsoap reads fell behind real time during the swap")

    def test_candidate_that_fails_to_start_leaves_the_active_processor_running(self):
        harness = self.make()
        harness.start()
        first = harness.processor(gain=1.0, label="first")
        harness.bridge.offer(first, warmup_frames=0, crossfade_frames=0)
        harness.bridge.wait_outcome(first, timeout=5)
        harness.wait(0.5)
        broken = harness.processor(fail_start=True, label="broken")
        harness.bridge.offer(broken, warmup_frames=B.SAMPLE_RATE // 5, crossfade_frames=B.CROSSFADE_FRAMES)
        with self.assertRaises(B.SwapFailed) as failed:
            harness.bridge.wait_outcome(broken, timeout=5)
        self.assertIn("exited with status 3", str(failed.exception))
        harness.wait(0.5)
        harness.stop()

        gains = gains_for(harness.output())
        self.assert_gain(gains, first.start_tag, len(gains), 1.0)
        self.assertIn(("broken", str(failed.exception)), harness.failures)

    def test_candidate_that_exits_during_warm_up_is_dropped(self):
        harness = self.make()
        harness.start()
        first = harness.processor(gain=1.0, label="first")
        harness.bridge.offer(first, warmup_frames=0, crossfade_frames=0)
        harness.bridge.wait_outcome(first, timeout=5)
        harness.wait(0.3)
        crashing = harness.processor(fail_after=2_000, label="crashing")
        harness.bridge.offer(crashing, warmup_frames=B.SAMPLE_RATE, crossfade_frames=B.CROSSFADE_FRAMES)
        with self.assertRaises(B.SwapFailed):
            harness.bridge.wait_outcome(crashing, timeout=5)
        harness.wait(0.3)
        harness.stop()
        gains = gains_for(harness.output())
        self.assert_gain(gains, first.start_tag, len(gains), 1.0)

    def test_active_crash_plays_dry_audio_then_recovers_with_a_crossfade(self):
        harness = self.make()
        harness.start()
        first = harness.processor(gain=1.0, fail_after=B.SAMPLE_RATE, label="active")
        harness.bridge.offer(first, warmup_frames=0, crossfade_frames=0)
        harness.bridge.wait_outcome(first, timeout=5)
        self.assertTrue(harness.failure_event.wait(4), "the crash was not reported")
        self.assertFalse(harness.bridge.active_is_processor(), "the bridge should fall back to dry audio")
        replacement = harness.processor(gain=1.0, label="replacement")
        harness.bridge.wait_retired(5)
        harness.bridge.offer(replacement, warmup_frames=B.SAMPLE_RATE // 5, crossfade_frames=B.CROSSFADE_FRAMES)
        harness.bridge.wait_outcome(replacement, timeout=5)
        harness.wait(0.5)
        harness.stop()

        gains = gains_for(harness.output())
        dry_frames = sum(1 for g in gains if g is not None and abs(g - DRY) < 0.01)
        self.assertGreater(dry_frames, B.SAMPLE_RATE // 5, "no dry audio played while the processor was down")
        self.assertTrue(harness.bridge.active_is_processor(), "the replacement did not take over")
        final_start = len(gains) - B.SAMPLE_RATE // 4
        self.assert_gain(gains, final_start, len(gains), 1.0)
        self.assertEqual([label for label, _ in harness.failures], ["active"])

    def test_stalled_active_processor_is_replaced_without_stopping_liquidsoap(self):
        harness = self.make()
        harness.start()
        first = harness.processor(gain=1.0, stall_after=B.SAMPLE_RATE // 2, label="stalled")
        harness.bridge.offer(first, warmup_frames=0, crossfade_frames=0)
        harness.bridge.wait_outcome(first, timeout=5)
        self.assertTrue(harness.failure_event.wait(5), "a stalled processor was never reported")
        replacement = harness.processor(gain=1.0, label="replacement")
        harness.bridge.wait_retired(5)
        harness.bridge.offer(replacement, warmup_frames=B.SAMPLE_RATE // 5, crossfade_frames=B.CROSSFADE_FRAMES)
        harness.bridge.wait_outcome(replacement, timeout=5)
        harness.wait(0.3)
        harness.stop()
        self.assertLess(harness.feed_lag, 0.1, "a stalled processor blocked Liquidsoap's reads")
        self.assertEqual(harness.failures[0][0], "stalled")

    def test_stuck_candidate_times_out_and_never_blocks_liquidsoap(self):
        harness = self.make()
        harness.start()
        first = harness.processor(gain=1.0, label="first")
        harness.bridge.offer(first, warmup_frames=0, crossfade_frames=0)
        harness.bridge.wait_outcome(first, timeout=5)
        harness.wait(0.3)
        stuck = harness.processor(stall_after=1, label="stuck")
        harness.bridge.offer(stuck, warmup_frames=B.SAMPLE_RATE // 5, crossfade_frames=B.CROSSFADE_FRAMES)
        with self.assertRaises(B.SwapFailed):
            harness.bridge.wait_outcome(stuck, timeout=1.5)
        harness.wait(0.5)
        harness.stop()
        self.assertLess(harness.feed_lag, 0.1, "a stuck candidate blocked Liquidsoap's reads")
        gains = gains_for(harness.output())
        self.assert_gain(gains, first.start_tag, len(gains), 1.0)
        self.assertTrue(harness.bridge.active_is_processor())

    def test_second_offer_while_one_is_pending_is_refused(self):
        harness = self.make()
        first = harness.processor(label="a")
        second = harness.processor(label="b")
        harness.bridge.offer(first, warmup_frames=0, crossfade_frames=0)
        with self.assertRaises(B.BridgeBusy):
            harness.bridge.offer(second, warmup_frames=0, crossfade_frames=0)

    def test_crossfade_must_be_whole_blocks(self):
        harness = self.make()
        with self.assertRaises(ValueError):
            harness.bridge.offer(harness.processor(label="odd"), crossfade_frames=100)


class SampleMathTest(unittest.TestCase):
    def test_dry_path_applies_the_link_makeup_in_normalised_float(self):
        block = array.array("h", [16384, -16384] * B.BLOCK_FRAMES).tobytes()
        values = array.array("f")
        values.frombytes(B.dry_block(block))
        self.assertAlmostEqual(values[0], 0.5 * DRY, places=5)
        self.assertAlmostEqual(values[1], -0.5 * DRY, places=5)
        self.assertEqual(len(values), 2 * B.BLOCK_FRAMES)

    def test_dry_block_is_bit_identical_to_the_generator_it_replaced(self):
        def reference(block: bytes) -> bytes:
            # The implementation before the list comprehension, kept as the reference.
            samples = array.array("h")
            samples.frombytes(block)
            if not B._LITTLE_ENDIAN:
                samples.byteswap()
            return B._float_bytes(array.array("f", (value * B._DRY_SCALE for value in samples)))

        every_value = array.array("h", range(-32768, 32768)).tobytes()
        self.assertEqual(B.dry_block(every_value), reference(every_value))
        blocks = [array.array("h", [-32768, 32767, 0, -1, 1, 12345] * 147).tobytes(), bytes(B.IN_BLOCK_BYTES)]
        for seed in range(20):
            state = seed * 7919 + 1
            values = []
            for _ in range(2 * B.BLOCK_FRAMES):
                state = (state * 1103515245 + 12345) & 0x7FFFFFFF
                values.append((state >> 8) % 65536 - 32768)
            blocks.append(array.array("h", values).tobytes())
        for block in blocks:
            self.assertEqual(B.dry_block(block), reference(block))
        with unittest.mock.patch.object(B, "_LITTLE_ENDIAN", False):  # the big-endian branch
            for block in blocks:
                self.assertEqual(B.dry_block(block), reference(block))

    def test_crossfade_mix_is_linear_across_the_window(self):
        old = array.array("f", [1.0] * (2 * B.BLOCK_FRAMES)).tobytes()
        new = array.array("f", [0.0] * (2 * B.BLOCK_FRAMES)).tobytes()
        length = 2 * B.BLOCK_FRAMES
        first = array.array("f")
        first.frombytes(B.mix_blocks(old, new, 0, length))
        self.assertAlmostEqual(first[0], 1 - 0.5 / length, places=5)
        second = array.array("f")
        second.frombytes(B.mix_blocks(old, new, B.BLOCK_FRAMES, length))
        self.assertAlmostEqual(second[-2], 0.5 / length, places=5)


if __name__ == "__main__":
    unittest.main()
