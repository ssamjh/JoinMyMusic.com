"""Live control, level meters and the processing graph for the audio container.

The supervisor (run.py) calls prepare() once at startup. It returns a Session with the
write end of the encoder's monitor pipe and a Monitor whose threads drain that pipe, and,
when CONTROL_PORT is set, the HTTP control API and the live parameter control. The API
is reached only over the Compose network: the API container's admin panel draws the
monitor page and proxies these calls. A
GraphController owns the running processor: it validates graphs, replaces the processor
through the bridge (bridge.py), watches processing.ffmpeg and restarts a failed processor.
Live control needs the azmq filter and pyzmq; without them meters and graph editing still
work, but parameter changes and the processing bypass are refused. Control failures log and
degrade. The monitor pipe is always drained so the encoder never blocks on it.
"""

from __future__ import annotations

import array
import importlib
import json
import math
import operator
import os
import re
import socket
import socketserver
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from bridge import COMMITTED, CROSSFADE_FRAMES, WARMUP_FRAMES, BridgeBusy, Processor, SwapFailed

DEFAULT_CONTROL_PORT = 8090
DEFAULT_ZMQ_PORT = 5555
MAX_BODY_BYTES = 4096
MAX_GRAPH_BODY_BYTES = 65_536  # room for an edited graph with its comments
MAX_GRAPH_BYTES = 60_000
COMMAND_TIMEOUT_MS = 1000
SAMPLE_RATE = 44_100
COLUMN_FRAMES = SAMPLE_RATE // 100  # 10 ms columns
COLUMN_BYTES = COLUMN_FRAMES * 2 * 2  # stereo s16le
RING_COLUMNS = 200  # 2 s of history per side
EVENT_COLUMN_CAP = 50
EVENT_INTERVAL = 0.1
WATCH_SECONDS = 5.0  # analysis runs only while a meter client has asked this recently
IDLE_CHUNKS = 8  # chunks kept per side while nobody watches
GRAPH_INPUT = "[0:a:0]"
GRAPH_OUTPUT = "[proc]"
BYPASS_WET = "volume@jmm_wet"
BYPASS_DRY = "volume@jmm_dry"

# name@label=options, up to the next separator. Unnamed filters never match.
NAMED_FILTER = re.compile(r"(?<![\w@])([A-Za-z_]\w*)@([A-Za-z_]\w*)=([^,;\[\]\s]*)")
# One option line in `ffmpeg -h filter=NAME`: name, <type>, 11-character flags, text.
HELP_OPTION = re.compile(r"^  (\S+)\s+<[^>]+>\s+([.A-Za-z]{11})\s", re.MULTILINE)
NUMBER = re.compile(r"([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)(dB)?")


def log(message: str) -> None:
    print(f"control: {message}", file=sys.stderr, flush=True)


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class TransportError(Exception):
    """FFmpeg did not answer a ZMQ command in time."""


# ---------------------------------------------------------------------------
# Filtergraph parsing and rewriting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OptionInfo:
    name: str
    live: bool  # FFmpeg's runtime flag: the option can change while the graph runs


OptionLookup = Callable[[str], list[OptionInfo]]


@dataclass
class Param:
    name: str
    value: float  # linear for dB-written options (such as volume), otherwise as written
    default: float
    live: bool
    db: bool = False  # the graph wrote this value in dB; export writes it back in dB


@dataclass
class NamedFilter:
    id: str
    filter: str
    label: str
    params: list[Param] = field(default_factory=list)


def parse_help_options(text: str) -> list[OptionInfo]:
    return [OptionInfo(match.group(1), "T" in match.group(2)) for match in HELP_OPTION.finditer(text)]


def parse_number(raw: str) -> tuple[float, str] | None:
    """A plain or dB number as (value, unit); anything else (names, flags) is None."""
    match = NUMBER.fullmatch(raw)
    if match is None:
        return None
    value = float(match.group(1))
    if not math.isfinite(value):
        return None
    return value, match.group(2) or ""


def stored_value(number: tuple[float, str]) -> float:
    """The value the monitor uses: dB values become linear gain (rounded to 6 places)."""
    value, unit = number
    return round(10 ** (value / 20), 6) if unit == "dB" else value


def format_db(linear: float) -> str:
    """Linear gain as dB with up to two decimals and no trailing zeros."""
    if linear <= 0:
        return format_wire(linear)
    text = f"{20 * math.log10(linear):.2f}".rstrip("0").rstrip(".")
    return ("0" if text in ("-0", "") else text) + "dB"


def split_args(args: str) -> list[tuple[str | None, str]]:
    """Split 'k=v:k=v' into (key, value) pairs; a bare value has key None."""
    pairs: list[tuple[str | None, str]] = []
    for part in args.split(":"):
        if not part:
            continue
        key, separator, value = part.partition("=")
        pairs.append((key, value) if separator else (None, key))
    return pairs


def resolve_key(key: str | None, infos: list[OptionInfo]) -> str | None:
    """A bare value sets the filter's first option, as FFmpeg does."""
    if key is None:
        return infos[0].name if infos else None
    return key


def parse_named_filters(graph: str, options: OptionLookup) -> list[NamedFilter]:
    """Named filters in graph order with their numeric options. Non-numeric options are left out."""
    filters: list[NamedFilter] = []
    seen: set[str] = set()
    for match in NAMED_FILTER.finditer(graph):
        name, label, args = match.groups()
        identifier = f"{name}@{label}"
        if identifier in seen:
            raise ValueError(f"filter name {identifier} is used more than once")
        seen.add(identifier)
        infos = options(name)
        params: list[Param] = []
        for raw_key, raw_value in split_args(args):
            key = resolve_key(raw_key, infos)
            number = parse_number(raw_value)
            if key is None or number is None:
                continue
            value = stored_value(number)
            live = any(info.name == key and info.live for info in infos)
            params.append(Param(key, value, value, live, db=number[1] == "dB"))
        filters.append(NamedFilter(identifier, name, label, params))
    return filters


def format_wire(value: float) -> str:
    return f"{value:.10g}"


def _rewrite_filter(match: re.Match[str], options: OptionLookup, values: dict[tuple[str, str], float]) -> str:
    name, label, args = match.groups()
    identifier = f"{name}@{label}"
    infos = options(name)
    pieces: list[str] = []
    for part in args.split(":"):
        key, separator, raw = part.partition("=")
        if not separator:
            key, raw = None, part
        resolved = resolve_key(key, infos)
        number = parse_number(raw)
        current = values.get((identifier, resolved)) if resolved is not None else None
        if current is not None and number is not None and current != stored_value(number):
            raw = format_db(current) if number[1] == "dB" else format_wire(current)
        pieces.append(f"{key}={raw}" if separator else raw)
    return f"{identifier}={':'.join(pieces)}"


def substitute_values(source: str, options: OptionLookup, values: dict[tuple[str, str], float]) -> str:
    """The processing file with changed live values written into the named filters. Comments and layout stay."""
    lines: list[str] = []
    for line in source.splitlines():
        if line.lstrip().startswith("#"):
            lines.append(line)
        else:
            lines.append(NAMED_FILTER.sub(lambda m: _rewrite_filter(m, options, values), line))
    return "\n".join(lines) + "\n"


def bypass_graph(processing: str, zmq_port: int, bypass: bool = False) -> str:
    """Wrap the processing graph in a bypassable wet path with a dry path beside it.

    The azmq filter receives commands on 127.0.0.1:zmq_port. The wet path is the
    processing graph; the dry path adds back the 12 dB link headroom that Liquidsoap
    applies upstream. Both paths feed the processor's float output. The wet path starts
    at volume 1 and the dry path at 0 unless bypass is set, so the default output matches
    the unwrapped graph.
    """
    graph = processing.strip()
    if not graph.startswith(GRAPH_INPUT) or not graph.endswith(GRAPH_OUTPUT):
        raise ValueError("processing graph must read [0:a:0] and end with [proc]")
    body = graph[len(GRAPH_INPUT):-len(GRAPH_OUTPUT)] + "[jmm_wetout]"
    wet, dry = ("0", "1") if bypass else ("1", "0")
    return (
        f"[0:a:0]azmq=bind_address=tcp\\\\://127.0.0.1\\\\:{zmq_port},asplit=2[jmm_wet][jmm_dry];"
        f"[jmm_wet]{body};"
        f"[jmm_wetout]{BYPASS_WET}={wet}[jmm_w];"
        f"[jmm_dry]volume=12dB,{BYPASS_DRY}={dry}[jmm_d];"
        "[jmm_w][jmm_d]amix=inputs=2:normalize=0[proc]"
    )


# ---------------------------------------------------------------------------
# FFmpeg probes
# ---------------------------------------------------------------------------


def ffmpeg_filters(ffmpeg: str) -> str:
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-filters"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=10, text=True, errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout


def has_filter(ffmpeg: str, name: str) -> bool:
    return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", ffmpeg_filters(ffmpeg), re.MULTILINE) is not None


class FilterHelp:
    """Option lists from `ffmpeg -h filter=NAME`, probed once per filter name."""

    def __init__(self, ffmpeg: str) -> None:
        self._ffmpeg = ffmpeg
        self._cache: dict[str, list[OptionInfo]] = {}

    def options(self, name: str) -> list[OptionInfo]:
        if name not in self._cache:
            self._cache[name] = self._probe(name)
        return self._cache[name]

    def _probe(self, name: str) -> list[OptionInfo]:
        try:
            result = subprocess.run(
                [self._ffmpeg, "-hide_banner", "-h", f"filter={name}"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=10, text=True, errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            return []
        return parse_help_options(result.stdout)


def preflight_error(ffmpeg: str, graph: str) -> str | None:
    """Run a processor graph briefly on silence. Returns None if it starts, otherwise FFmpeg's message.

    This catches graph errors and ZMQ bind errors before the graph replaces the running one.
    """
    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-t", "0.2", "-i", "anullsrc=r=44100:cl=stereo",
        "-filter_complex", graph,
        "-map", "[proc]", "-c:a", "pcm_f32le", "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"FFmpeg preflight could not run: {exc}"
    if result.returncode != 0:
        return "FFmpeg rejected the graph: " + result.stderr.decode("utf-8", "replace").strip()[-300:]
    return None


# ---------------------------------------------------------------------------
# Live parameter control
# ---------------------------------------------------------------------------


class ZmqTransport:
    """REQ client for azmq. A timeout closes the socket, since a REQ socket cannot recover in place."""

    def __init__(self, port: int) -> None:
        self._zmq = importlib.import_module("zmq")
        self._port = port
        self._context = self._zmq.Context()
        self._socket = None

    def request(self, text: str) -> str:
        zmq = self._zmq
        if self._socket is None:
            socket = self._context.socket(zmq.REQ)
            socket.setsockopt(zmq.LINGER, 0)
            socket.setsockopt(zmq.RCVTIMEO, COMMAND_TIMEOUT_MS)
            socket.setsockopt(zmq.SNDTIMEO, COMMAND_TIMEOUT_MS)
            socket.connect(f"tcp://127.0.0.1:{self._port}")
            self._socket = socket
        try:
            self._socket.send_string(text)
            return self._socket.recv_string()
        except (zmq.ZMQError, UnicodeDecodeError) as exc:
            self._socket.close(linger=0)
            self._socket = None
            raise TransportError(str(exc)) from exc

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close(linger=0)
            self._socket = None
        self._context.term()


def liquidsoap_socket_path() -> str:
    """Liquidsoap's server socket, used to bypass the input AGC (see config/processing.liq)."""
    return os.environ.get("LIQUIDSOAP_SOCKET", "/run/audio/liquidsoap.sock")


AGC_GAIN_TIMEOUT = 0.25  # seconds; the meter stream reads the AGC gain on every event
AGC_GAIN_MAX_AGE = 0.1  # seconds a reading is reused, so several meter clients share one


class LiquidsoapControl:
    """Sets interactive variables in Liquidsoap and reads its levels through its server socket."""

    def __init__(self, path: str) -> None:
        self._path = path

    def set_bool(self, name: str, value: bool) -> None:
        # No spaces around '=': Liquidsoap 2.1 rejects "name = value" despite its help text.
        text = self.command(f"var.set {name}={'true' if value else 'false'}")
        if "set" not in text.lower():
            raise ApiError(409, f"Liquidsoap refused {name}: {text or 'no reply'}")

    def vban_level_db(self) -> float | None:
        """The VBAN input's RMS level in dBFS, or None when the receiver is off."""
        return self._float("vban_level")

    def agc_gain_db(self) -> float | None:
        """The input AGC's gain in dB, or None when the AGC is compiled out."""
        return self._float("agc_gain", timeout=AGC_GAIN_TIMEOUT)

    def _float(self, command: str, timeout: float | None = None) -> float | None:
        text = self.command(command, timeout)
        try:
            return float(text)
        except ValueError:
            return None  # an unknown command

    def command(self, line: str, timeout: float | None = None) -> str:
        command = f"{line}\n".encode("ascii")
        reply = b""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(COMMAND_TIMEOUT_MS / 1000 if timeout is None else timeout)
                conn.connect(self._path)
                conn.sendall(command)
                while not reply.rstrip().endswith(b"END"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    reply += chunk
        except OSError as exc:
            raise ApiError(503, f"Liquidsoap did not answer: {exc}") from exc
        return reply.decode("utf-8", "replace").replace("END", "").strip()


class LiveControl:
    """Runtime values of the named filters and the bypass state, shared by the HTTP threads."""

    def __init__(self, filters: list[NamedFilter], source: str, options: OptionLookup) -> None:
        self._filters = filters
        self._by_id = {item.id: item for item in filters}
        self._source = source
        self._options = options
        self._transport = None
        self._lock = threading.Lock()
        self._bypass = False
        self._agc_bypass = False
        self._liquidsoap = LiquidsoapControl(liquidsoap_socket_path())
        self._agc_lock = threading.Lock()
        self._agc_gain: float | None = None
        self._agc_read_at = 0.0
        self._values = {(item.id, param.name): param.value for item in filters for param in item.params}

    @property
    def available(self) -> bool:
        return self._transport is not None

    def attach(self, transport: object) -> None:
        self._transport = transport

    @property
    def bypass(self) -> bool:
        with self._lock:
            return self._bypass

    def rebind(self, filters: list[NamedFilter], source: str, transport: object | None) -> None:
        """Point control at a newly running graph: its filters, its source, and its ZMQ transport.

        Values reset to the new graph's values. The bypass state carries over, and is sent to
        the new processor in case it changed while the processor was starting.
        """
        with self._lock:
            old = self._transport
            self._filters = filters
            self._by_id = {item.id: item for item in filters}
            self._source = source
            self._values = {(item.id, param.name): param.value for item in filters for param in item.params}
            self._transport = transport
            if self._bypass and transport is not None:
                self._send_quietly(f"{BYPASS_WET} volume 0")
                self._send_quietly(f"{BYPASS_DRY} volume 1")
        if old is not None and old is not transport:
            old.close()

    def state(self) -> dict:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> dict:
        return {
            "control": self.available,
            "bypass": self._bypass,
            "agc_bypass": self._agc_bypass,
            "filters": [
                {
                    "id": item.id,
                    "filter": item.filter,
                    "label": item.label,
                    "params": [self._param_json(item.id, param) for param in item.params],
                }
                for item in self._filters
            ],
        }

    def _param_json(self, identifier: str, param: Param) -> dict:
        return {
            "name": param.name,
            "value": self._values[(identifier, param.name)],
            "default": param.default,
            "live": param.live,
        }

    def export_text(self) -> str:
        with self._lock:
            values = dict(self._values)
        return substitute_values(self._source, self._options, values)

    def set_param(self, target: str, name: str, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ApiError(400, "value must be a finite number")
        with self._lock:
            self._live_param(target, name)
            number = float(value)
            self._send(f"{target} {name} {format_wire(number)}")
            self._values[(target, name)] = number
            return number

    def _live_param(self, target: str, name: str) -> Param:
        item = self._by_id.get(target)
        if item is None:
            raise ApiError(400, f"unknown filter: {target}")
        param = next((candidate for candidate in item.params if candidate.name == name), None)
        if param is None:
            raise ApiError(400, f"unknown option for {target}: {name}")
        if not param.live:
            raise ApiError(400, f"{name} on {target} cannot be changed while FFmpeg runs")
        return param

    def set_bypass(self, bypass: bool) -> dict:
        with self._lock:
            self._set_bypass_locked(bypass)
            return self._state_locked()

    def _set_bypass_locked(self, bypass: bool) -> None:
        wet, dry = ("0", "1") if bypass else ("1", "0")
        self._send(f"{BYPASS_WET} volume {wet}")
        try:
            self._send(f"{BYPASS_DRY} volume {dry}")
        except ApiError:
            self._send_quietly(f"{BYPASS_WET} volume {'1' if bypass else '0'}")
            raise
        self._bypass = bypass

    def vban_level_db(self) -> float | None:
        return self._liquidsoap.vban_level_db()

    def agc_gain_db(self) -> float | None:
        """The AGC gain, read at most every AGC_GAIN_MAX_AGE. While another thread is reading
        it, returns the last reading instead of waiting."""
        if time.monotonic() - self._agc_read_at < AGC_GAIN_MAX_AGE:
            return self._agc_gain
        if not self._agc_lock.acquire(blocking=False):
            return self._agc_gain
        try:
            try:
                self._agc_gain = self._liquidsoap.agc_gain_db()
            except ApiError:
                self._agc_gain = None
            self._agc_read_at = time.monotonic()
            return self._agc_gain
        finally:
            self._agc_lock.release()

    def set_agc_bypass(self, bypass: bool) -> dict:
        """Bypass the input AGC, which runs in Liquidsoap before the processor."""
        with self._lock:
            self._liquidsoap.set_bool("agc_bypass", bypass)
            self._agc_bypass = bypass
            return self._state_locked()

    def reset(self) -> dict:
        with self._lock:
            for item in self._filters:
                for param in item.params:
                    key = (item.id, param.name)
                    if param.live and self._values[key] != param.default:
                        self._send(f"{item.id} {param.name} {format_wire(param.default)}")
                        self._values[key] = param.default
            self._set_bypass_locked(False)
            if self._agc_bypass:
                self._liquidsoap.set_bool("agc_bypass", False)
                self._agc_bypass = False
            return self._state_locked()

    def _send(self, command: str) -> None:
        if self._transport is None:
            raise ApiError(503, "live control is unavailable")
        try:
            reply = self._transport.request(command)
        except TransportError as exc:
            raise ApiError(503, f"FFmpeg did not answer: {exc}") from exc
        code, _, message = reply.partition(" ")
        try:
            status = int(code)
        except ValueError:
            raise ApiError(409, f"unexpected reply to '{command}': {reply[:80]}") from None
        if status != 0:
            raise ApiError(409, f"FFmpeg rejected '{command}': {message.strip() or status}")

    def _send_quietly(self, command: str) -> None:
        try:
            self._send(command)
        except ApiError:
            pass


# ---------------------------------------------------------------------------
# Level meters
# ---------------------------------------------------------------------------


def _peak_rms(channel: array.array) -> tuple[float, float]:
    peak = max(max(channel), -min(channel)) / 32768.0
    rms = math.sqrt(sum(map(operator.mul, channel, channel)) / len(channel)) / 32768.0
    return round(min(peak, 1.0), 4), round(min(rms, 1.0), 4)


def measure(samples: array.array) -> list[float]:
    """One 10 ms stereo column as [peak L, peak R, RMS L, RMS R], each 0..1."""
    peak_left, rms_left = _peak_rms(samples[0::2])
    peak_right, rms_right = _peak_rms(samples[1::2])
    return [peak_left, peak_right, rms_left, rms_right]


def monitor_input_fifo_path() -> str:
    """FIFO carrying Liquidsoap's copy of the input mix (Spotify and VBAN) before the AGC."""
    return os.environ.get("MONITOR_INPUT_FIFO", "/run/audio/monitor-input.pcm")


class Monitor:
    """Meters for the input (the mix before the AGC) and output (after the safety limiter).

    feed_input() and the drain thread only append to bounded deques, so they never wait
    on the analysis thread. When the analysis falls behind, the oldest data is dropped.
    The analysis only runs while a meter client is connected; otherwise the audio is discarded.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self._input: deque[bytes] = deque(maxlen=256)
        self._output: deque[bytes] = deque(maxlen=256)
        self._lock = threading.Lock()
        self._rings = {"in": deque(maxlen=RING_COLUMNS), "out": deque(maxlen=RING_COLUMNS)}
        self._seq = {"in": 0, "out": 0}
        self._watched_until = 0.0

    def feed_input(self, block: bytes | bytearray) -> None:
        if self.enabled:
            self._input.append(bytes(block))

    def start_input(self, fd: int) -> None:
        """Read Liquidsoap's input copy from fd. Never stops reading: Liquidsoap would block on it."""
        threading.Thread(target=self._drain_input, args=(fd,), name="monitor-input", daemon=True).start()

    def _drain_input(self, fd: int) -> None:
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError as exc:
                log(f"input monitor read failed: {exc}")
                time.sleep(0.5)
                continue
            if chunk:
                self.feed_input(chunk)

    def start(self, read_fd: int) -> None:
        threading.Thread(target=self._drain, args=(read_fd,), name="monitor-drain", daemon=True).start()
        if self.enabled:
            threading.Thread(target=self._analyze, name="monitor-analyze", daemon=True).start()

    def _drain(self, fd: int) -> None:
        """Read FFmpeg's monitor output until EOF. Never exits early: FFmpeg would block on a full pipe."""
        warned = False
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError as exc:
                if not warned:
                    log(f"monitor pipe read failed: {exc}")
                    warned = True
                time.sleep(0.5)
                continue
            if not chunk:
                return
            if self.enabled:
                self._output.append(chunk)

    def _analyze(self) -> None:
        queues = {"in": self._input, "out": self._output}
        pending = {"in": bytearray(), "out": bytearray()}
        while True:
            if time.monotonic() > self._watched_until:
                for queue in queues.values():
                    while len(queue) > IDLE_CHUNKS:
                        queue.popleft()
                time.sleep(0.1)
                continue
            busy = False
            for side, queue in queues.items():
                while True:
                    try:
                        pending[side] += queue.popleft()
                    except IndexError:
                        break
                    busy = True
                try:
                    self._measure_side(side, pending[side])
                except Exception as exc:  # meters are optional; keep the thread alive
                    log(f"meter analysis failed: {exc}")
                    pending[side].clear()
            if not busy:
                time.sleep(0.005)

    def _measure_side(self, side: str, buffer: bytearray) -> None:
        usable = len(buffer) - len(buffer) % COLUMN_BYTES
        if not usable:
            return
        columns = []
        for offset in range(0, usable, COLUMN_BYTES):
            samples = array.array("h")
            samples.frombytes(buffer[offset:offset + COLUMN_BYTES])
            if sys.byteorder == "big":
                samples.byteswap()
            columns.append(measure(samples))
        del buffer[:usable]
        with self._lock:
            for column in columns:
                self._seq[side] += 1
                self._rings[side].append((self._seq[side], column))

    def snapshot(self, cursor: dict[str, int]) -> dict[str, list[list[float]]]:
        """Columns newer than cursor on each side, newest EVENT_COLUMN_CAP at most. Advances cursor."""
        result: dict[str, list[list[float]]] = {}
        self._watched_until = time.monotonic() + WATCH_SECONDS
        with self._lock:
            for side, ring in self._rings.items():
                newer = [(seq, column) for seq, column in ring if seq > cursor[side]]
                if newer:
                    cursor[side] = newer[-1][0]
                result[side] = [column for _, column in newer[-EVENT_COLUMN_CAP:]]
        return result


# ---------------------------------------------------------------------------
# HTTP interface
# ---------------------------------------------------------------------------


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], live: LiveControl, monitor: Monitor,
                 graphs: GraphController | None = None) -> None:
        self.live = live
        self.monitor = monitor
        self.graphs = graphs
        super().__init__(address, ControlHandler)

    def server_bind(self) -> None:
        # HTTPServer resolves the host name with getfqdn(), which can stall on DNS. Skip it.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


class ControlHandler(BaseHTTPRequestHandler):
    server_version = "JMMControl"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - base class signature
        return

    @property
    def live(self) -> LiveControl:
        return self.server.live  # type: ignore[attr-defined]

    @property
    def monitor(self) -> Monitor:
        return self.server.monitor  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/state":
                state = self.live.state()
                graphs = self.server.graphs  # type: ignore[attr-defined]
                state["swap"] = graphs.swap_state() if graphs is not None else None
                self._json(200, state)
            elif path == "/api/graph":
                self._json(200, self._graphs().state())
            elif path == "/api/export":
                self._send(200, "text/plain; charset=utf-8", self.live.export_text().encode("utf-8"))
            elif path == "/api/meters":
                self._meters()
            elif path == "/api/source":
                # Read by the API to tell when the stream is the VBAN input alone.
                self._json(200, {"vban_level_db": self.live.vban_level_db()})
            else:
                self._json(404, {"error": "not found"})
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception as exc:
            log(f"request {path} failed: {exc}")
            self._safe_error()

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/bypass":
                body = self._read_json()
                bypass = body.get("bypass")
                if not isinstance(bypass, bool):
                    raise ApiError(400, "bypass must be true or false")
                self._json(200, self.live.set_bypass(bypass))
            elif path == "/api/agc":
                body = self._read_json()
                bypass = body.get("bypass")
                if not isinstance(bypass, bool):
                    raise ApiError(400, "bypass must be true or false")
                self._json(200, self.live.set_agc_bypass(bypass))
            elif path == "/api/param":
                body = self._read_json()
                target, name = body.get("target"), body.get("name")
                if not isinstance(target, str) or not isinstance(name, str):
                    raise ApiError(400, "target and name must be strings")
                value = self.live.set_param(target, name, body.get("value"))
                self._json(200, {"ok": True, "value": value})
            elif path == "/api/reset":
                self._read_json()
                self._json(200, self.live.reset())
            elif path == "/api/graph":
                body = self._read_json(limit=MAX_GRAPH_BODY_BYTES)
                text = body.get("text")
                if not isinstance(text, str) or not text.strip():
                    raise ApiError(400, "text must be the processing graph as a non-empty string")
                self._json(200, self._graphs().apply(text))
            elif path == "/api/graph/reload":
                self._read_json()
                self._json(200, self._graphs().reload())
            else:
                self._json(404, {"error": "not found"})
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception as exc:
            log(f"request {path} failed: {exc}")
            self._safe_error()

    def _graphs(self) -> GraphController:
        if self.server.graphs is None:  # type: ignore[attr-defined]
            raise ApiError(404, "processing graph control is not available")
        return self.server.graphs  # type: ignore[attr-defined]

    def _read_json(self, limit: int = MAX_BODY_BYTES) -> dict:
        media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise ApiError(415, "Content-Type must be application/json")
        length = self.headers.get("Content-Length", "0")
        if not length.isdigit():
            raise ApiError(400, "Content-Length must be a number")
        if int(length) > limit:
            raise ApiError(413, "request body is too large")
        raw = self.rfile.read(int(length))
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw)
        except ValueError:
            raise ApiError(400, "body must be JSON") from None
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        return body

    def _json(self, status: int, payload: object) -> None:
        self._send(status, "application/json", json.dumps(payload).encode("utf-8"))

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _safe_error(self) -> None:
        try:
            self._json(500, {"error": "internal error"})
        except OSError:
            pass

    def _meters(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        cursor = {"in": 0, "out": 0}
        while True:
            columns = self.monitor.snapshot(cursor)
            if columns["in"] or columns["out"]:
                event = {**columns, "agc": self.live.agc_gain_db()}
                chunk = f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
            else:
                chunk = ": keep-alive\n\n"
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()
            time.sleep(EVENT_INTERVAL)


# ---------------------------------------------------------------------------
# Processing graph: validation, hot-swaps, file watching and recovery
# ---------------------------------------------------------------------------

RECOVER_DELAY = 1.0  # seconds between attempts to restart a failed processor
SWAP_MARGIN = 10.0  # seconds a processor may take to take over, beyond its warm-up
RETIRE_WAIT = 5.0  # seconds to wait for a stopped processor before starting another


def processing_graph_text(source: str) -> str:
    """The processing graph from processing.ffmpeg text, comment lines removed. Raises ValueError."""
    lines = [line.strip() for line in source.splitlines()]
    graph = "\n".join(line for line in lines if line and not line.startswith("#"))
    if not graph.startswith(GRAPH_INPUT) or GRAPH_OUTPUT not in graph:
        raise ValueError("the graph must read [0:a:0] and end with the [proc] label")
    return graph


def _same_text(first: str, second: str) -> bool:
    return first.replace("\r\n", "\n").rstrip() == second.replace("\r\n", "\n").rstrip()


def _file_key(path: Path | None) -> tuple[int, int] | None:
    if path is None:
        return None
    try:
        status = path.stat()
    except OSError:
        return None
    return status.st_mtime_ns, status.st_size


def _zmq_ready(ffmpeg: str) -> bool:
    try:
        importlib.import_module("zmq")
    except ImportError:
        return False
    return has_filter(ffmpeg, "azmq")


def _port_setting(name: str, default: int) -> int | None:
    raw = os.environ.get(name, str(default)).strip()
    if raw in ("", "0"):
        return None
    try:
        port = int(raw)
    except ValueError:
        log(f"{name} must be a port number; ignoring {raw!r}")
        return None
    if not 1 <= port <= 65535:
        log(f"{name} must be between 1 and 65535; ignoring {port}")
        return None
    return port


@dataclass
class Session:
    """Everything prepare() sets up. Call start() with the GraphController to serve the API."""

    monitor_fd: int  # write end for the encoder's monitor output; close it in the parent after starting it
    monitor: Monitor
    ffmpeg: str
    live: LiveControl | None = None
    server: ControlServer | None = None
    zmq_base: int | None = None  # live control ports are zmq_base and zmq_base + 1; None disables wrapping
    filters: list[NamedFilter] = field(default_factory=list)
    filter_help: FilterHelp | None = None

    def start(self, graphs: "GraphController") -> None:
        if self.server is None:
            return
        self.server.graphs = graphs
        threading.Thread(target=self.server.serve_forever, name="control-http", daemon=True).start()
        log(f"control API on port {self.server.server_address[1]}")


def prepare(source: str, processing: str) -> Session:
    """Set up the monitor, the control API and live control. Never raises for control failures.

    source is the processing file as written (comments kept, used for export and the graph editor).
    processing is the comment-stripped graph.
    """
    read_fd, write_fd = os.pipe()
    port = _port_setting("CONTROL_PORT", DEFAULT_CONTROL_PORT)
    monitor = Monitor(enabled=port is not None)
    monitor.start(read_fd)
    ffmpeg = os.environ.get("FFMPEG_BIN", "ffmpeg")
    session = Session(monitor_fd=write_fd, monitor=monitor, ffmpeg=ffmpeg)
    if port is None:
        return session

    help_ = FilterHelp(ffmpeg)
    session.filter_help = help_
    try:
        session.filters = parse_named_filters(processing, help_.options)
    except ValueError as exc:
        log(f"control API disabled: {exc}")
        return session
    session.live = LiveControl(session.filters, source, help_.options)
    try:
        session.server = ControlServer(("0.0.0.0", port), session.live, monitor)
    except OSError as exc:
        log(f"control API not started on port {port}: {exc}")

    zmq_base = _port_setting("CONTROL_ZMQ_PORT", DEFAULT_ZMQ_PORT)
    if zmq_base is None:
        log("live control disabled: CONTROL_ZMQ_PORT is 0")
    elif not _zmq_ready(ffmpeg):
        log("live control unavailable: needs the azmq filter and the pyzmq package")
    elif zmq_base >= 65535:
        log("live control unavailable: CONTROL_ZMQ_PORT must be below 65535")
    else:
        session.zmq_base = zmq_base
    return session


class GraphController:
    """The running processing graph: validation, hot-swaps, file watching and recovery.

    Each processor is started from a complete graph (wrapped for live control when that is
    available), checked with preflight_error, and brought in through the bridge. One swap runs
    at a time; a request that finds one running gets 409. A processor that fails after taking
    over is replaced with the graph that is running, keeping the live values. A failed
    replacement is retried every RECOVER_DELAY seconds while the previous graph is dropped.
    """

    def __init__(
        self,
        *,
        bridge: object,
        session: Session,
        path: Path | None,
        source: str,
        processing: str,
        processor_args: Callable[[str], list[str]],
        spawn: Callable[[list[str]], object] = Processor,
        preflight_fn: Callable[[str, str], str | None] = preflight_error,
        warmup_frames: int = WARMUP_FRAMES,
        crossfade_frames: int = CROSSFADE_FRAMES,
        watch_interval: float = 0.5,
        watch_settle: float = 0.5,
    ) -> None:
        self._bridge = bridge
        self._live = session.live
        self._help = session.filter_help
        self._ffmpeg = session.ffmpeg
        self._zmq_base = session.zmq_base
        self._filters: list[NamedFilter] = list(session.filters)
        self._path = path
        self._processor_args = processor_args
        self._spawn = spawn
        self._preflight = preflight_fn
        self._warmup = warmup_frames
        self._crossfade = crossfade_frames
        self._watch_interval = watch_interval
        self._watch_settle = watch_settle
        self._swap_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self.source = source  # the text as applied; live values are added by state() and export
        self.processing = processing
        self._file_text = source
        self._port: int | None = None
        self._state = "idle"
        self._swap_error: str | None = None
        self._applied_at: float | None = None
        self._committed_once = False
        self._recovering = False
        bridge.on_failure = self._on_failure  # type: ignore[attr-defined]

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Check the startup graph, then start its processor in the background.

        A startup graph that FFmpeg rejects, without live control, stops the service:
        the file is wrong, and a restart loop would only keep the output on dry audio.
        """
        error = self._preflight(self._ffmpeg, self._graph_for(self.processing, self._zmq_base, False))
        if error is not None and self._zmq_base is not None:
            log(f"live control unavailable: {error}")
            self._zmq_base = None
            error = None
        if error is None:
            error = self._preflight(self._ffmpeg, self._graph_for(self.processing, None, False))
        if error is not None:
            raise SystemExit(f"processing graph does not start: {error}")
        if self._path is not None:
            threading.Thread(target=self._watch, name="graph-watch", daemon=True).start()
        self._start_recovery()

    def stop(self) -> None:
        self._stop.set()

    # -- API ---------------------------------------------------------------

    def state(self) -> dict:
        return {
            "text": self._current_text(),
            "origin": "file" if _same_text(self._current_text(), self._file_text) else "edited",
            "path": str(self._path) if self._path is not None else None,
            "swap": self.swap_state(),
        }

    def swap_state(self) -> dict:
        with self._state_lock:
            return {"state": self._state, "error": self._swap_error, "applied_at": self._applied_at}

    def apply(self, text: str) -> dict:
        """Validate text and run it in place of the current graph. Raises ApiError."""
        return self._swap(text, blocking=False)

    def reload(self) -> dict:
        """Run the processing file's current contents. Raises ApiError."""
        if self._path is None:
            raise ApiError(404, "no processing file is configured")
        try:
            text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ApiError(503, f"cannot read {self._path.name}: {exc}") from None
        self._file_text = text
        return self._swap(text, blocking=False)

    # -- swapping ----------------------------------------------------------

    def _current_text(self) -> str:
        return self._live.export_text() if self._live is not None else self.source

    def _graph_for(self, processing: str, port: int | None, bypass: bool) -> str:
        return processing if port is None else bypass_graph(processing, port, bypass)

    def _next_port(self) -> int | None:
        if self._zmq_base is None:
            return None
        return self._zmq_base + 1 if self._port == self._zmq_base else self._zmq_base

    def _params(self) -> tuple[int, int]:
        """A replacement warms up and crossfades once something has been on air; the first start does neither."""
        return (self._warmup, self._crossfade) if self._committed_once else (0, 0)

    def _swap(self, text: str, blocking: bool) -> dict:
        if not self._swap_lock.acquire(blocking=blocking):
            raise ApiError(409, "a swap is already in progress")
        try:
            self._set_swap("warming", None)
            if len(text.encode("utf-8")) > MAX_GRAPH_BYTES:
                raise ApiError(413, "the processing graph is too large")
            try:
                processing = processing_graph_text(text)
                filters = parse_named_filters(processing, self._help.options) if self._live is not None else []
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
            self._attempt(text, processing, filters, *self._params())
            self._set_swap("idle", None)
            return self.state()
        except ApiError as exc:
            self._set_swap("error", exc.message)
            raise
        finally:
            self._swap_lock.release()

    def _attempt(self, text: str, processing: str, filters: list[NamedFilter], warmup: int, crossfade: int) -> None:
        """Start a processor for processing and wait for it to take over. Caller holds _swap_lock."""
        self._bridge.wait_retired(RETIRE_WAIT)  # type: ignore[attr-defined]  # its ZMQ port must be free
        port = self._next_port()
        bypass = self._live.bypass if self._live is not None else False
        graph = self._graph_for(processing, port, bypass)
        error = self._preflight(self._ffmpeg, graph)
        if error is not None:
            raise ApiError(400, error)
        try:
            processor = self._spawn(self._processor_args(graph))
        except OSError as exc:
            raise ApiError(503, f"could not start FFmpeg: {exc}") from None
        try:
            self._bridge.offer(processor, warmup_frames=warmup, crossfade_frames=crossfade)  # type: ignore[attr-defined]
            self._bridge.wait_outcome(processor, timeout=warmup / SAMPLE_RATE + SWAP_MARGIN)  # type: ignore[attr-defined]
        except BridgeBusy:
            processor.stop()  # type: ignore[attr-defined]
            raise ApiError(409, "a swap is already in progress") from None
        except SwapFailed as exc:
            raise ApiError(503, f"the new graph did not take over; the previous one keeps running: {exc}") from None

        transport = ZmqTransport(port) if port is not None and self._live is not None else None
        self._port = port
        self._committed_once = True
        self.source = text
        self.processing = processing
        self._filters = filters
        self._applied_at = time.time()
        if self._live is not None:
            self._live.rebind(filters, text, transport)
        log("processing graph running" + (f" (live control on port {port})" if port is not None else ""))

    def _start_recovery(self) -> None:
        with self._state_lock:
            if self._recovering:
                return
            self._recovering = True
        threading.Thread(target=self._recover_loop, name="graph-recover", daemon=True).start()

    def _recover_loop(self) -> None:
        """Start a processor for the running graph until one takes over. Live values are kept."""
        try:
            while not self._stop.is_set():
                with self._swap_lock:
                    if self._bridge.active_is_processor():  # type: ignore[attr-defined]
                        return
                    text = self._current_text()
                    try:
                        processing = processing_graph_text(text)
                        filters = parse_named_filters(processing, self._help.options) if self._live is not None else []
                        self._attempt(text, processing, filters, *self._params())
                        self._set_swap("idle", None)
                        return
                    except (ApiError, ValueError) as exc:
                        message = exc.message if isinstance(exc, ApiError) else str(exc)
                        self._set_swap("error", message)
                        log(f"processor did not start, retrying: {message}")
                self._stop.wait(RECOVER_DELAY)
        finally:
            with self._state_lock:
                self._recovering = False

    def _on_failure(self, processor: object, reason: str) -> None:
        """Called by the bridge. Only the active processor needs recovery; a candidate's waiter reports its own failure."""
        if getattr(processor, "outcome", None) != COMMITTED:
            return
        self._set_swap("error", f"processor stopped ({reason}); restarting")
        log(f"processor stopped: {reason}; restarting with the running graph")
        self._start_recovery()

    def _set_swap(self, state: str, error: str | None) -> None:
        with self._state_lock:
            self._state = state
            self._swap_error = error
            if state == "idle":
                self._applied_at = time.time()

    # -- file watching -----------------------------------------------------

    def _watch(self) -> None:
        """Apply processing.ffmpeg when it changes. A change counts once its size and mtime hold for watch_settle seconds."""
        applied = _file_key(self._path)
        seen, seen_at = applied, time.monotonic()
        while not self._stop.wait(self._watch_interval):
            key = _file_key(self._path)
            if key is None:
                continue
            now = time.monotonic()
            if key != seen:
                seen, seen_at = key, now
                continue
            if key == applied or now - seen_at < self._watch_settle:
                continue
            applied = key
            try:
                text = self._path.read_text(encoding="utf-8")  # type: ignore[union-attr]
            except (OSError, UnicodeDecodeError) as exc:
                log(f"cannot read processing file: {exc}")
                continue
            self._file_text = text
            if _same_text(text, self._current_text()):
                continue
            try:
                self._swap(text, blocking=False)
                log("processing.ffmpeg change applied")
            except ApiError as exc:
                if exc.status == 409:
                    applied = None  # a swap is running; look again on the next poll
                else:
                    log(f"processing.ffmpeg change not applied: {exc.message}")
