"""Hot-swappable radio processing between Liquidsoap and the encoder.

Stream layout:

    Liquidsoap (s16le stereo 44.1 kHz on stdout)
      -> bridge
      -> processor (FFmpeg: s16le in, f32le out; replaceable)
      -> bridge
      -> encoder (FFmpeg: f32le in; RADIO_FILTER, limiter, AAC/VBAN/monitor; long-lived)

Liquidsoap paces the chain at real time. The bridge reads it in 10 ms blocks and gives
each block to the active processor and to any processor being brought in. Every block
has a tag, its absolute frame index on the input timeline; a processor's output frame j
is tagged as the input frame it was started at plus j, which assumes one output frame
per input frame. A render thread writes tags to the encoder in order, each as soon as its
source has produced it. Deadlines are wall-clock times from the block's arrival, because
Liquidsoap delivers audio in bursts: a burst advances the input by many blocks at once.
The encoder input starts with EMIT_DELAY_FRAMES of silence.

A new processor first warms up on the live input, then the bridge crossfades from the
active source to it over CROSSFADE_FRAMES and stops the old one. A processor that fails
to start or exits during warm-up is discarded and the active one keeps running. If the
active processor has not produced a block LATE_SECONDS after its input arrived, that
block is "dry" (the input with the same 12 dB link makeup the processing graph applies).
A processor that fails, or produces nothing for STALL_SECONDS, is replaced by dry audio
and reported through on_failure so the owner can start a replacement.
The encoder is never starved, and nothing here waits on a processor from the input loop.

Threads: one input loop (Liquidsoap reads), one render loop, one encoder writer,
and for each processor a stdin writer, a stdout reader and a stderr drain. Stopping a
processor runs in its own thread.
"""

from __future__ import annotations

import fcntl
import os
import queue
import subprocess
import sys
import threading
import time
from array import array
from collections import deque
from typing import Callable

SAMPLE_RATE = 44_100
CHANNELS = 2
BLOCK_FRAMES = SAMPLE_RATE // 100  # 10 ms
IN_BLOCK_BYTES = BLOCK_FRAMES * CHANNELS * 2  # s16le
OUT_FRAME_BYTES = CHANNELS * 4  # f32le
OUT_BLOCK_BYTES = BLOCK_FRAMES * OUT_FRAME_BYTES
# Silence at the start of the encoder's input, so output tag t is encoder frame t + this.
EMIT_DELAY_FRAMES = 10 * BLOCK_FRAMES  # 100 ms
# A block not produced this long after its input arrived is played dry.
LATE_SECONDS = 0.25
# FFmpeg's raw PCM demuxer reads 1024-frame packets, so a processor cannot produce a block
# until up to 1024 more frames have arrived after it. The deadline starts from those.
DEMUX_SLACK_FRAMES = 3 * BLOCK_FRAMES
WARMUP_FRAMES = SAMPLE_RATE  # 1 s of live input before a replacement takes over
CROSSFADE_FRAMES = 2 * BLOCK_FRAMES  # 20 ms, a whole number of blocks
STALL_SECONDS = 1.0  # this long without output fails the active processor
HISTORY_BLOCKS = 300  # 3 s of input kept for the dry path
PROCESSOR_QUEUE_BLOCKS = 50  # 0.5 s of input queued for one processor
ENCODER_QUEUE_BLOCKS = 50
ENCODER_PUT_TIMEOUT = 2.0
PIPE_BYTES = 8192  # small kernel buffers keep the latency of each pipe low
DRY_MAKEUP_DB = 12.0  # matches the link headroom that processing.ffmpeg removes first
COMMITTED = "committed"

SILENCE = "silence"
DRY = "dry"
ZERO_BLOCK = bytes(OUT_BLOCK_BYTES)
_DRY_SCALE = 10 ** (DRY_MAKEUP_DB / 20) / 32768.0
_LITTLE_ENDIAN = sys.byteorder == "little"


class BridgeBusy(RuntimeError):
    """Another processor is already waiting to take over."""


class SwapFailed(RuntimeError):
    """A processor did not take over; the previous one keeps running."""


def log(message: str) -> None:
    print(f"bridge: {message}", file=sys.stderr, flush=True)


def shrink_pipe(fd: int) -> None:
    """Reduce a pipe's kernel buffer to cut latency; keep the default if refused."""
    try:
        fcntl.fcntl(fd, getattr(fcntl, "F_SETPIPE_SZ", 1031), PIPE_BYTES)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Sample helpers. Blocks are interleaved stereo; f32 values are normalised like FFmpeg's.
# ---------------------------------------------------------------------------


def _floats(data: bytes) -> array:
    values = array("f")
    values.frombytes(data)
    if not _LITTLE_ENDIAN:
        values.byteswap()
    return values


def _float_bytes(values: array) -> bytes:
    if not _LITTLE_ENDIAN:
        values.byteswap()
    return values.tobytes()


def dry_block(block: bytes) -> bytes:
    """One 10 ms s16le block as f32le with the link makeup applied."""
    samples = array("h")
    samples.frombytes(block)
    if not _LITTLE_ENDIAN:
        samples.byteswap()
    return _float_bytes(array("f", (value * _DRY_SCALE for value in samples)))


def mix_blocks(outgoing: bytes, incoming: bytes, offset: int, length: int) -> bytes:
    """Linear crossfade of one f32le block. offset is the position of its first frame in the crossfade."""
    old = _floats(outgoing)
    new = _floats(incoming)
    mixed = array("f", old)
    for frame in range(BLOCK_FRAMES):
        gain = (offset + frame + 0.5) / length
        left = 2 * frame
        mixed[left] = old[left] + (new[left] - old[left]) * gain
        mixed[left + 1] = old[left + 1] + (new[left + 1] - old[left + 1]) * gain
    return _float_bytes(mixed)


def _read_exact(fd: int, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        data = os.read(fd, remaining)
        if not data:
            raise RuntimeError("Liquidsoap closed its audio output")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Processor: one replaceable FFmpeg process
# ---------------------------------------------------------------------------


class Processor:
    """An FFmpeg processing process: s16le blocks in on stdin, f32le frames out on stdout.

    The process is started when constructed. Its threads start with register(), which
    the bridge calls when the processor is brought in. Until then nothing is fed to it.
    """

    def __init__(self, args: list[str], env: dict[str, str] | None = None, label: str = "processor") -> None:
        self.label = label
        self.warmup_frames = WARMUP_FRAMES
        self.crossfade_frames = CROSSFADE_FRAMES
        self.outcome: str | None = None  # COMMITTED, or the reason it was dropped
        self.start_tag: int | None = None
        self.stopping = False
        self.notify: Callable[[], None] | None = None  # called when output arrives or the processor fails
        self._process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=env,
        )
        shrink_pipe(self._process.stdin.fileno())
        shrink_pipe(self._process.stdout.fileno())
        self._lock = threading.Lock()
        self._inbox: queue.Queue = queue.Queue(maxsize=PROCESSOR_QUEUE_BLOCKS)
        self._stderr: deque[str] = deque(maxlen=6)
        self._error: str | None = None
        self._buffer = bytearray()  # f32le frames, the first one tagged _buffer_tag
        self._buffer_tag = 0
        self._end_tag = 0  # the tag after the last complete frame received
        self._partial = b""
        threading.Thread(target=self._drain_stderr, name=f"{label}-stderr", daemon=True).start()

    def register(self, start_tag: int) -> None:
        with self._lock:
            self.start_tag = start_tag
            self._buffer_tag = start_tag
            self._end_tag = start_tag
        threading.Thread(target=self._write_loop, name=f"{self.label}-stdin", daemon=True).start()
        threading.Thread(target=self._read_loop, name=f"{self.label}-stdout", daemon=True).start()

    def feed(self, block: bytes) -> None:
        """Queue one input block. Never waits; a processor that falls too far behind is failed."""
        try:
            self._inbox.put_nowait(block)
        except queue.Full:
            self._fail("it stopped reading its input")

    def failure(self) -> str | None:
        with self._lock:
            error = self._error
        code = None if self.stopping else self._process.poll()
        if code is not None:
            error = f"exited with status {code}"
        if error is None:
            return None
        tail = self.stderr_tail()
        return f"{error}: {tail}" if tail else error

    def stderr_tail(self) -> str:
        return " | ".join(self._stderr)

    def covers(self, tag: int, frames: int) -> bool:
        with self._lock:
            return self._buffer_tag <= tag and tag + frames <= self._end_tag

    def block(self, tag: int, frames: int) -> bytes | None:
        with self._lock:
            if tag < self._buffer_tag or tag + frames > self._end_tag:
                return None
            start = (tag - self._buffer_tag) * OUT_FRAME_BYTES
            return bytes(self._buffer[start:start + frames * OUT_FRAME_BYTES])

    def trim(self, tag: int) -> None:
        """Forget frames before tag; they are no longer needed."""
        with self._lock:
            count = min(tag - self._buffer_tag, self._end_tag - self._buffer_tag)
            if count > 0:
                del self._buffer[: count * OUT_FRAME_BYTES]
                self._buffer_tag += count

    def stop(self, grace: float = 1.0) -> None:
        """Terminate the process, killing it if it ignores SIGTERM. Safe to call more than once.

        The pipes are closed by the threads that use them. A processor that was never
        registered has no threads, so its pipes are closed here.
        """
        self.stopping = True
        try:
            self._inbox.put_nowait(None)
        except queue.Full:
            pass
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(grace)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        if self.start_tag is None:  # stderr belongs to the drain thread, which closes it at EOF
            for stream in (self._process.stdin, self._process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass

    def _fail(self, reason: str) -> None:
        with self._lock:
            if self._error is None:
                self._error = reason
        self._wake()

    def _wake(self) -> None:
        notify = self.notify
        if notify is not None:
            notify()

    def _write_loop(self) -> None:
        stdin = self._process.stdin
        try:
            while True:
                block = self._inbox.get()
                if block is None:
                    return
                view = memoryview(block)
                try:
                    while view:
                        view = view[os.write(stdin.fileno(), view):]
                except OSError as exc:
                    if not self.stopping:
                        self._fail(f"input write failed: {exc}")
                    return
        finally:
            stdin.close()  # EOF tells the processor that no more input is coming

    def _read_loop(self) -> None:
        stdout = self._process.stdout
        try:
            while True:
                try:
                    chunk = os.read(stdout.fileno(), 16_384)
                except OSError as exc:
                    if not self.stopping:
                        self._fail(f"output read failed: {exc}")
                    return
                if not chunk:
                    if not self.stopping:
                        self._fail("output ended")
                    return
                with self._lock:
                    data = self._partial + chunk
                    usable = len(data) - len(data) % OUT_FRAME_BYTES
                    self._buffer += data[:usable]
                    self._end_tag += usable // OUT_FRAME_BYTES
                    self._partial = data[usable:]
                self._wake()
        finally:
            stdout.close()

    def _drain_stderr(self) -> None:
        stderr = self._process.stderr
        try:
            for raw in iter(stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._stderr.append(line)
                    log(f"{self.label}: {line}")
        finally:
            stderr.close()


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class _Crossfade:
    __slots__ = ("start", "length", "outgoing", "incoming")

    def __init__(self, start: int, length: int, outgoing: object, incoming: Processor) -> None:
        self.start = start
        self.length = length
        self.outgoing = outgoing
        self.incoming = incoming


class Bridge:
    """Reads Liquidsoap, drives the processors and writes the encoder's input.

    on_failure(processor, reason) is called, outside the lock, when the active processor
    fails (its outcome is COMMITTED) or a processor being brought in is dropped.
    """

    def __init__(
        self,
        liquidsoap_fd: int,
        encoder_fd: int,
        on_failure: Callable[[Processor, str], None] | None = None,
        emit_delay_frames: int = EMIT_DELAY_FRAMES,
        late_seconds: float = LATE_SECONDS,
        stall_seconds: float = STALL_SECONDS,
    ) -> None:
        self._in_fd = liquidsoap_fd
        self._enc_fd = encoder_fd
        self.on_failure = on_failure
        self._emit_delay = emit_delay_frames
        self._late_seconds = late_seconds
        self._stall_seconds = stall_seconds
        self._lock = threading.RLock()  # re-entrant: a processor can wake the bridge from feed()
        self._cond = threading.Condition(self._lock)
        self._frames_in = 0
        self._active: object = SILENCE  # SILENCE, DRY or a Processor
        self._pending: Processor | None = None
        self._incoming: Processor | None = None
        self._xfade: _Crossfade | None = None
        self._history: deque[tuple[int, bytes, float]] = deque(maxlen=HISTORY_BLOCKS)  # tag, block, arrival
        self._late_since: float | None = None
        self._encoder_queue: queue.Queue = queue.Queue(maxsize=ENCODER_QUEUE_BLOCKS)
        self._fatal: BaseException | None = None
        self._stopping = False
        self._retiring: list[threading.Thread] = []

    def start(self) -> None:
        for target, name in (
            (self._input_loop, "bridge-input"),
            (self._render_loop, "bridge-render"),
            (self._encoder_loop, "bridge-encoder"),
        ):
            threading.Thread(target=target, name=name, daemon=True).start()

    def offer(self, processor: Processor, warmup_frames: int = WARMUP_FRAMES,
              crossfade_frames: int = CROSSFADE_FRAMES) -> None:
        """Bring in a processor. It takes over after warmup_frames of input, through a crossfade of crossfade_frames (0 = switch at once)."""
        if warmup_frames < 0 or crossfade_frames % BLOCK_FRAMES or crossfade_frames < 0:
            raise ValueError("crossfade must be a whole number of 10 ms blocks; warm-up cannot be negative")
        with self._lock:
            if self._pending is not None or self._incoming is not None:
                raise BridgeBusy("another processor is already being brought in")
            processor.warmup_frames = warmup_frames
            processor.crossfade_frames = crossfade_frames
            processor.outcome = None
            processor.notify = self._wake
            self._pending = processor
            self._cond.notify_all()

    def wait_outcome(self, processor: Processor, timeout: float) -> None:
        """Return once processor has taken over. Raise SwapFailed if it is dropped or does not take over in time."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                if processor.outcome == COMMITTED:
                    return
                if processor.outcome is not None:
                    raise SwapFailed(processor.outcome)
                if self._fatal is not None:
                    raise SwapFailed(f"bridge stopped: {self._fatal}")
                crossfading = self._xfade is not None and self._xfade.incoming is processor
                if not crossfading:
                    reason = processor.failure()
                    if reason is None and time.monotonic() >= deadline:
                        reason = "it did not take over before the timeout"
                    if reason is not None:
                        self._cancel(processor, reason)
                        break
                self._cond.wait(0.05)
        self._report(processor, reason)
        raise SwapFailed(reason)

    def wait_retired(self, timeout: float) -> None:
        """Wait for stopped processors to exit, so their ports and resources are free."""
        deadline = time.monotonic() + timeout
        with self._lock:
            threads = list(self._retiring)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            self._retiring = [thread for thread in self._retiring if thread.is_alive()]

    def active_is_processor(self) -> bool:
        with self._lock:
            return isinstance(self._active, Processor)

    def check(self) -> None:
        """Raise the error that stopped the bridge, if any."""
        if self._fatal is not None:
            raise RuntimeError(str(self._fatal)) from self._fatal

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            processors = [self._active, self._incoming, self._pending]
        try:
            self._encoder_queue.put_nowait(None)
        except queue.Full:
            pass
        for processor in processors:
            if isinstance(processor, Processor):
                processor.stop()

    # -- lock held by the caller --------------------------------------------

    def _cancel(self, processor: Processor, reason: str) -> None:
        if self._pending is processor:
            self._pending = None
        if self._incoming is processor:
            self._incoming = None
        processor.outcome = reason
        self._retire(processor)
        self._cond.notify_all()

    def _retire(self, processor: object) -> None:
        if not isinstance(processor, Processor) or processor.stopping:
            return
        processor.stopping = True
        thread = threading.Thread(target=processor.stop, name="processor-stop", daemon=True)
        thread.start()
        self._retiring = [item for item in self._retiring if item.is_alive()] + [thread]

    def _commit(self, new: Processor) -> None:
        old = self._active
        self._active = new
        self._incoming = None
        self._xfade = None
        self._late_since = None
        new.outcome = COMMITTED
        self._retire(old)
        self._cond.notify_all()

    def _discard(self, processor: Processor, reason: str, events: list) -> None:
        if self._incoming is processor:
            self._incoming = None
        processor.outcome = reason
        self._retire(processor)
        events.append((processor, reason))
        self._cond.notify_all()

    def _fail_active(self, processor: Processor, reason: str, events: list) -> None:
        self._active = DRY
        self._late_since = None
        processor.outcome = COMMITTED  # it was the active processor; on_failure keys off this
        self._retire(processor)
        events.append((processor, reason))
        self._cond.notify_all()

    def _dry_block(self, tag: int) -> bytes | None:
        if tag < 0:
            return ZERO_BLOCK
        if not self._history:
            return None
        first = self._history[0][0]
        index = (tag - first) // BLOCK_FRAMES
        if tag < first or index >= len(self._history):
            return None
        return dry_block(self._history[index][1])

    def _source_block(self, source: object, tag: int) -> bytes | None:
        if source == SILENCE:
            return ZERO_BLOCK
        if source == DRY:
            return self._dry_block(tag)
        return source.block(tag, BLOCK_FRAMES)  # type: ignore[union-attr]

    def _source_covers(self, source: object, tag: int) -> bool:
        if source == SILENCE:
            return True
        if source == DRY:
            return tag < 0 or (bool(self._history) and self._history[0][0] <= tag < self._frames_in)
        return source.covers(tag, BLOCK_FRAMES)  # type: ignore[union-attr]

    def _check_failures(self, events: list) -> None:
        active = self._active
        if isinstance(active, Processor):
            reason = active.failure()
            if reason is not None:
                self._fail_active(active, reason, events)
        incoming = self._incoming
        if incoming is not None and self._xfade is None:
            reason = incoming.failure()
            if reason is not None:
                self._discard(incoming, reason, events)

    def _maybe_start(self, tag: int, events: list) -> None:
        incoming = self._incoming
        if incoming is None or incoming.start_tag is None or tag < incoming.start_tag + incoming.warmup_frames:
            return
        if not incoming.covers(tag, BLOCK_FRAMES):
            return
        if incoming.crossfade_frames == 0:
            self._commit(incoming)
        elif self._source_covers(self._active, tag):
            self._xfade = _Crossfade(tag, incoming.crossfade_frames, self._active, incoming)

    def _render_crossfade(self, tag: int, events: list) -> bytes:
        xfade = self._xfade
        assert xfade is not None
        incoming = xfade.incoming
        offset = tag - xfade.start
        new_block = None
        reason = incoming.failure()
        if reason is None:
            new_block = self._source_block(incoming, tag)
            if new_block is None:
                reason = "it fell behind during the crossfade"
        if reason is not None:
            self._xfade = None
            self._discard(incoming, reason, events)
            return self._render_active(tag, events)
        old_block = self._source_block(xfade.outgoing, tag)
        output = new_block if old_block is None else mix_blocks(old_block, new_block, offset, xfade.length)
        if offset + BLOCK_FRAMES >= xfade.length:
            self._commit(incoming)
        return output

    def _render_active(self, tag: int, events: list) -> bytes:
        active = self._active
        block = self._source_block(active, tag)
        if isinstance(active, Processor):
            if block is None:
                now = time.monotonic()
                if self._late_since is None:
                    self._late_since = now
                    log(f"{active.label} is late; playing dry audio until it catches up")
                if now - self._late_since >= self._stall_seconds:
                    self._fail_active(active, f"it produced no audio for {self._stall_seconds:.1f} s", events)
                block = self._dry_block(tag)
            else:
                self._late_since = None
            active.trim(tag)
        if isinstance(self._incoming, Processor):
            self._incoming.trim(tag)
        return block if block is not None else ZERO_BLOCK

    def _render(self, tag: int, events: list) -> bytes:
        """The output block for emission tag. Caller holds the lock."""
        if self._xfade is None:
            self._check_failures(events)
            if self._incoming is not None:
                self._maybe_start(tag, events)
        if self._xfade is not None:
            return self._render_crossfade(tag, events)
        return self._render_active(tag, events)

    # -- threads -----------------------------------------------------------

    def _input_loop(self) -> None:
        try:
            while True:
                block = _read_exact(self._in_fd, IN_BLOCK_BYTES)
                with self._lock:
                    if self._stopping:
                        return
                    tag = self._frames_in
                    self._frames_in += BLOCK_FRAMES
                    self._history.append((tag, block, time.monotonic()))
                    for processor in (self._active, self._incoming):
                        if isinstance(processor, Processor):
                            processor.feed(block)
                    if self._pending is not None:
                        pending, self._pending = self._pending, None
                        pending.register(tag)
                        self._incoming = pending
                        pending.feed(block)
                    self._cond.notify_all()
        except BaseException as exc:  # Liquidsoap closed
            self._set_fatal(exc)

    def _wake(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def _ready(self, tag: int) -> bool:
        """Whether the sources the render of tag needs have produced it. Caller holds the lock."""
        xfade = self._xfade
        if xfade is not None:
            return xfade.incoming.covers(tag, BLOCK_FRAMES) and self._source_covers(xfade.outgoing, tag)
        incoming = self._incoming
        if (incoming is not None and incoming.start_tag is not None
                and tag >= incoming.start_tag + incoming.warmup_frames
                and not incoming.covers(tag, BLOCK_FRAMES)):
            return False  # give a warmed-up processor until the deadline to take over here
        return self._source_covers(self._active, tag)

    def _arrival(self, tag: int) -> float | None:
        if not self._history or tag < self._history[0][0]:
            return None
        index = (tag - self._history[0][0]) // BLOCK_FRAMES
        return self._history[index][2] if index < len(self._history) else None

    def _render_loop(self) -> None:
        """Write tags to the encoder in order, each once its sources have it or its deadline passes."""
        tag = -self._emit_delay
        try:
            while True:
                events: list = []
                with self._cond:
                    while True:
                        if self._stopping or self._fatal is not None:
                            return
                        if tag < 0:
                            break
                        if tag + BLOCK_FRAMES <= self._frames_in:
                            if self._ready(tag):
                                break
                            remaining = 0.05
                            if tag + DEMUX_SLACK_FRAMES + BLOCK_FRAMES <= self._frames_in:
                                arrival = self._arrival(tag + DEMUX_SLACK_FRAMES)
                                remaining = 0.0 if arrival is None else arrival + self._late_seconds - time.monotonic()
                                if remaining <= 0:
                                    break
                            self._cond.wait(min(remaining, 0.05))
                        else:
                            self._cond.wait(0.05)
                    output = self._render(tag, events)
                for processor, reason in events:
                    self._report(processor, reason)
                self._enqueue(output)
                tag += BLOCK_FRAMES
        except BaseException as exc:  # the encoder stopped reading
            self._set_fatal(exc)

    def _report(self, processor: Processor, reason: str) -> None:
        log(f"{processor.label} dropped: {reason}")
        if self.on_failure is not None:
            try:
                self.on_failure(processor, reason)
            except Exception as exc:  # the owner's callback must not stop the stream
                log(f"failure handler error: {exc}")

    def _enqueue(self, block: bytes) -> None:
        try:
            self._encoder_queue.put(block, timeout=ENCODER_PUT_TIMEOUT)
        except queue.Full:
            raise RuntimeError("the encoder is not reading its input") from None

    def _encoder_loop(self) -> None:
        try:
            while True:
                block = self._encoder_queue.get()
                if block is None:
                    return
                view = memoryview(block)
                while view:
                    view = view[os.write(self._enc_fd, view):]
        except OSError as exc:
            if not self._stopping:
                self._set_fatal(RuntimeError(f"the encoder stopped reading its input: {exc}"))

    def _set_fatal(self, exc: BaseException) -> None:
        with self._lock:
            if self._fatal is None and not self._stopping:
                self._fatal = exc
            self._cond.notify_all()
