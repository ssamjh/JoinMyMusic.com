#!/usr/bin/env python3
"""Run Spotify Connect PCM through Liquidsoap and FFmpeg to RTMP and Icecast."""

from __future__ import annotations

import os
import ipaddress
import errno
import selectors
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import control  # noqa: E402  (live control and monitor; same directory)
from bridge import Bridge, shrink_pipe  # noqa: E402  (hot-swappable processing)

SAMPLE_RATE = 44_100
CHANNELS = 2
BYTES_PER_FRAME = 2 * CHANNELS
BLOCK_BYTES = SAMPLE_RATE * BYTES_PER_FRAME // 50  # 20 ms at 44.1 kHz
# go-librespot's pipe output is not paced: it decodes as fast as this queue accepts and
# blocks when it is full. Every queued byte is latency, so keep 100 ms: enough for its
# 46 ms writes plus scheduling jitter.
MAX_QUEUE_BYTES = SAMPLE_RATE * BYTES_PER_FRAME // 10
# Kernel FIFOs default to 64 KiB (about 370 ms here); shrink both to about 46 ms.
BLOCK_SECONDS = 0.02
SHUTDOWN_TIMEOUT = 3
stopping = False


def request_stop(_signum: int, _frame: object) -> None:
    global stopping
    stopping = True


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def fifo_path() -> str:
    return os.environ.get("AUDIO_FIFO", "/run/audio/spotify.pcm")


DEFAULT_AUDIO_FILTER = (
    "alimiter=limit=0.95:attack=5:release=100:level=false"
)


REPO_PROCESSING_GRAPH = Path(__file__).resolve().parents[1] / "config" / "processing.ffmpeg"


def processing_graph_path() -> Path:
    """Radio processing graph: PROCESSING_FFMPEG_PATH, then the mounted /etc/jmm copy, then the checkout's config/processing.ffmpeg."""
    configured = os.environ.get("PROCESSING_FFMPEG_PATH", "").strip()
    if configured:
        return Path(configured)
    installed = Path("/etc/jmm/processing.ffmpeg")
    return installed if installed.is_file() else REPO_PROCESSING_GRAPH


processing_graph_text = control.processing_graph_text


def load_processing_graph(path: Path | None = None) -> str:
    """Return the processing graph from processing.ffmpeg with comment lines removed."""
    source = path or processing_graph_path()
    if not source.is_file():
        raise SystemExit(f"Processing graph not found: {source}")
    try:
        return processing_graph_text(source.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"{source}: {exc}") from exc


def _encoder_chain(source: str, vban_enabled: bool, safety: bool, monitor: bool) -> str:
    """RADIO_FILTER, the safety limiter, then the AAC / VBAN / monitor split."""
    custom_filter = os.environ.get("RADIO_FILTER", "").strip()
    tail = [part for part in (custom_filter, DEFAULT_AUDIO_FILTER if safety else "") if part]
    chain = source + (",".join(tail) or "anull")
    outputs = ["[stream]"] + (["[vban]"] if vban_enabled else []) + (["[mon]"] if monitor else [])
    if len(outputs) == 1:
        return f"{chain}[stream]"
    return f"{chain},asplit={len(outputs)}{''.join(outputs)}"


def encoder_filtergraph(
    vban_enabled: bool = False, safety: bool = True, monitor: bool = False
) -> str:
    """Encoder graph: reads the processor's f32 output as [0:a:0]."""
    return _encoder_chain("[0:a:0]", vban_enabled, safety, monitor)


def audio_filtergraph(
    vban_enabled: bool = False, graph: str | None = None, safety: bool = True, monitor: bool = False
) -> str:
    """The processing graph and the encoder graph as one filtergraph (used by the lab and tests)."""
    processing = graph if graph is not None else load_processing_graph()
    return f"{processing};{_encoder_chain('[proc]', vban_enabled, safety, monitor)}"


def radio_fifo_path() -> str:
    return os.environ.get("RADIO_INPUT_FIFO", "/run/audio/liquidsoap-input.pcm")


def radio_script_path() -> str:
    return os.environ.get("RADIO_LIQ_PATH", "/etc/jmm/processing.liq")


def load_audio_config() -> dict[str, object]:
    config_path = Path(os.environ.get("AUDIO_CONFIG_PATH", "/etc/jmm/audio.yml"))
    if not config_path.is_file():
        raise SystemExit(f"Audio config file not found: {config_path}")
    with config_path.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file) or {}
    if not isinstance(config, dict):
        raise SystemExit("Audio config must be a YAML mapping")
    return config


def validate_stream_name(stream_name: object, section: str) -> str:
    if not isinstance(stream_name, str) or not stream_name:
        raise SystemExit(f"{section}.stream_name must be a non-empty string")
    try:
        encoded_name = stream_name.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SystemExit(f"{section}.stream_name must contain ASCII characters only") from exc
    if len(encoded_name) > 15 or b"\x00" in encoded_name:
        raise SystemExit(f"{section}.stream_name must be at most 15 ASCII bytes")
    return stream_name


def validate_port(port: object, section: str) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise SystemExit(f"{section}.port must be an integer from 1 to 65535")
    return port


def load_vban_config() -> dict[str, object]:
    config = load_audio_config()
    vban = config.get("vban", {})
    if not isinstance(vban, dict):
        raise SystemExit("The 'vban' section in audio.yml must be a mapping")
    enabled = vban.get("enabled", False)
    if not isinstance(enabled, bool):
        raise SystemExit("vban.enabled must be true or false")
    if not enabled:
        return {"enabled": False}

    address = vban.get("ip_address", "")
    try:
        parsed_address = ipaddress.IPv4Address(str(address))
    except ipaddress.AddressValueError as exc:
        raise SystemExit("vban.ip_address must be an IPv4 address") from exc
    port = validate_port(vban.get("port", 6980), "vban")
    stream_name = validate_stream_name(vban.get("stream_name", "JMM Audio"), "vban")
    return {
        "enabled": True,
        "ip_address": str(parsed_address),
        "port": port,
        "stream_name": stream_name,
    }


# Network quality: the receive jitter buffer in seconds. The
# Liquidsoap input refills to this level after an underrun and holds at most twice it.
VBAN_RECEIVER_QUALITY = {"optimal": 0.1, "fast": 0.2, "medium": 0.4, "slow": 0.8, "very_slow": 1.5}


def load_vban_receiver_config() -> dict[str, object]:
    """Incoming VBAN stream, mixed with Spotify at equal level before processing."""
    receiver = load_audio_config().get("vban_receiver", {})
    if not isinstance(receiver, dict):
        raise SystemExit("The 'vban_receiver' section in audio.yml must be a mapping")
    enabled = receiver.get("enabled", False)
    if not isinstance(enabled, bool):
        raise SystemExit("vban_receiver.enabled must be true or false")
    if not enabled:
        return {"enabled": False}

    address = receiver.get("sender_ip", "")
    try:
        parsed_address = ipaddress.IPv4Address(str(address))
    except ipaddress.AddressValueError as exc:
        raise SystemExit("vban_receiver.sender_ip must be an IPv4 address") from exc
    # The whole chain runs at 44.1 kHz and nothing is resampled. sample_rate may be given,
    # but only as 44100.
    sample_rate = receiver.get("sample_rate", SAMPLE_RATE)
    if isinstance(sample_rate, bool) or sample_rate != SAMPLE_RATE:
        raise SystemExit("vban_receiver.sample_rate must be 44100: send 44.1 kHz from the VBAN sender")
    channels = receiver.get("channels", "1,2")
    parts = channels.split(",") if isinstance(channels, str) else []
    if len(parts) != 2 or not all(part.strip().isdigit() and int(part) >= 1 for part in parts):
        raise SystemExit("vban_receiver.channels must name two stream channels, for example '1,2' or '1,1'")
    quality = receiver.get("quality", "medium")
    if quality not in VBAN_RECEIVER_QUALITY:
        raise SystemExit("vban_receiver.quality must be one of: " + ", ".join(VBAN_RECEIVER_QUALITY))
    return {
        "enabled": True,
        "quality": quality,
        "sender_ip": str(parsed_address),
        "port": validate_port(receiver.get("port", 6980), "vban_receiver"),
        "stream_name": validate_stream_name(receiver.get("stream_name", "Stream1"), "vban_receiver"),
        "channels": ",".join(part.strip() for part in parts),
    }


def vban_receiver_fifo_path() -> str:
    return os.environ.get("VBAN_RECEIVER_FIFO", "/run/audio/vban-in.pcm")


def vban_receiver_args(config: dict[str, object]) -> list[str]:
    if not config.get("enabled"):
        return []
    return [
        os.environ.get("VBAN_RECEPTOR_BIN", "/usr/bin/vban_receptor"),
        "-i",
        str(config["sender_ip"]),
        "-p",
        str(config["port"]),
        "-s",
        str(config["stream_name"]),
        "-b",
        "file",
        "-d",
        vban_receiver_fifo_path(),
        "-c",
        str(config["channels"]),
        "-q",
        "0",
    ]


def vban_args(config: dict[str, object]) -> list[str]:
    if not config.get("enabled"):
        return []
    return [
        os.environ.get("VBAN_EMITTER_BIN", "/usr/bin/vban_emitter"),
        "-i",
        str(config["ip_address"]),
        "-p",
        str(config["port"]),
        "-s",
        str(config["stream_name"]),
        "-b",
        "file",
        "-d",
        os.environ.get("VBAN_FIFO", "/run/audio/vban.pcm"),
        "-r",
        str(SAMPLE_RATE),
        "-n",
        str(CHANNELS),
        "-f",
        "16I",
    ]


def tee_escape_url(url: str) -> str:
    """Escape tee-muxer delimiters in a destination URL."""
    return url.replace("\\", "\\\\").replace("|", "\\|")


def processor_args(graph: str) -> list[str]:
    """Processor command: s16le stereo on stdin, the processing graph, f32le stereo frames on stdout.

    The graph is replaceable (see bridge.py). Float output keeps the bypass and dry paths
    from clipping before the encoder's safety limiter.
    """
    return [
        os.environ.get("FFMPEG_BIN", "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        os.environ.get("FFMPEG_LOGLEVEL", "warning"),
        "-nostdin",
        "-filter_complex_threads",
        "1",
        "-probesize",
        "32",
        "-analyzeduration",
        "0",
        "-f",
        "s16le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        "-i",
        "pipe:0",
        "-filter_complex",
        graph,
        "-map",
        "[proc]",
        "-vn",
        "-c:a",
        "pcm_f32le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        "-flush_packets",
        "1",
        "-f",
        "f32le",
        "pipe:1",
    ]


def encoder_args(vban_enabled: bool = False, monitor_fd: int = 3) -> list[str]:
    """Encoder command: f32le stereo on stdin, RADIO_FILTER and limiter, then the outputs.

    The monitor output goes to pipe:monitor_fd (raw s16le stereo 44.1 kHz), after the limiter.
    """
    rtmp = required_env("RTMP_URL")
    icecast = required_env("ICECAST_URL")
    bitrate = os.environ.get("STREAM_BITRATE", "256k")
    filters = encoder_filtergraph(vban_enabled, monitor=True)
    icecast_scheme = urlsplit(icecast).scheme.lower()
    icecast_content_type = ":content_type=audio/aac" if icecast_scheme == "icecast" else ""
    tee_targets = (
        f"[f=flv:onfail=ignore:rw_timeout=3000000:flush_packets=1]"
        f"{tee_escape_url(rtmp)}|"
        f"[f=adts:onfail=ignore:rw_timeout=3000000:flush_packets=1{icecast_content_type}]"
        f"{tee_escape_url(icecast)}"
    )
    args = [
        os.environ.get("FFMPEG_BIN", "ffmpeg"),
        "-hide_banner",
        "-loglevel",
        os.environ.get("FFMPEG_LOGLEVEL", "warning"),
        "-nostdin",
        "-filter_complex_threads",
        "1",
        "-f",
        "f32le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        "-probesize",
        "32",
        "-analyzeduration",
        "0",
        "-i",
        "pipe:0",
        "-filter_complex",
        filters,
        "-map",
        "[stream]",
        "-vn",
        "-ac",
        "2",
        "-c:a",
        "aac",
        "-b:a",
        bitrate,
        "-flags",
        "+global_header",
        "-f",
        "tee",
        "-use_fifo",
        "1",
        "-fifo_options",
        "attempt_recovery=1:recover_any_error=1:recovery_wait_time=1:drop_pkts_on_overflow=1:queue_size=64",
        tee_targets,
    ]
    if vban_enabled:
        args.extend(
            [
                "-map",
                "[vban]",
                "-vn",
                "-ac",
                str(CHANNELS),
                "-ar",
                str(SAMPLE_RATE),
                "-c:a",
                "pcm_s16le",
                "-f",
                "s16le",
                "-flush_packets",
                "1",
                os.environ.get("VBAN_FIFO", "/run/audio/vban.pcm"),
            ]
        )
        args.insert(1, "-y")
    args.extend(
        [
            "-map",
            "[mon]",
            "-vn",
            "-ac",
            str(CHANNELS),
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            f"pipe:{monitor_fd}",
        ]
    )
    return args


def stop_processes(
    processes: list[subprocess.Popen[bytes] | None],
    process_groups: tuple[subprocess.Popen[bytes] | None, ...] = (),
) -> None:
    """Stop all children under one deadline, then kill and reap any holdouts."""
    groups = [proc for proc in process_groups if proc is not None]
    group_ids = {proc.pid for proc in groups}
    children = [proc for proc in processes if proc is not None]

    for proc in children:
        if proc.pid in group_ids:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif proc.poll() is None:
            proc.terminate()

    deadline = time.monotonic() + SHUTDOWN_TIMEOUT
    for proc in children:
        if proc.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                break

    # The process-group leader may exit before its external cat helpers do.
    # Escalate every Liquidsoap group after the shared grace period so no helper
    # keeps an output pipe open after the service has stopped.
    for proc in groups:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    for proc in children:
        if proc.poll() is None:
            proc.kill()
    for proc in children:
        if proc.poll() is None:
            proc.wait()


def open_radio_fifo_writer(alive: Callable[[], None], timeout: float = 15.0) -> int:
    """Wait for Liquidsoap's external raw-audio reader without blocking startup."""
    path = radio_fifo_path()
    deadline = time.monotonic() + timeout
    while not stopping:
        alive()
        try:
            return os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno not in (errno.ENXIO, errno.ENOENT):
                raise
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for Liquidsoap to read {path}")
        time.sleep(0.05)
    raise RuntimeError("Shutdown requested while waiting for Liquidsoap input")


def check_helpers(helpers: list[tuple[str, subprocess.Popen[bytes]]]) -> None:
    """The VBAN helpers have no restart of their own; their exit restarts the service."""
    for name, proc in helpers:
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited with status {proc.returncode}")


def pump_fifo(
    path: str,
    alive: Callable[[], None],
) -> None:
    """Pace the raw FIFO at 44.1 kHz and preserve silence across pauses.

    alive() raises when any child process or the bridge has failed; the pump then stops.
    """
    # Opening both ends prevents EOF spin when Spotify closes its writer between
    # tracks and lets a fresh playback writer reconnect without restarting FFmpeg.
    source_fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    shrink_pipe(source_fd)
    selector = selectors.DefaultSelector()
    selector.register(source_fd, selectors.EVENT_READ)
    pending = bytearray()
    next_block = time.monotonic()
    output_fd: int | None = None

    def send(block: bytearray) -> None:
        assert output_fd is not None
        view = memoryview(block)
        while view and not stopping:
            alive()
            try:
                written = os.write(output_fd, view)
                view = view[written:]
            except BlockingIOError:
                select.select([], [output_fd], [], 0.1)

    try:
        output_fd = open_radio_fifo_writer(alive)
        shrink_pipe(output_fd)
        while not stopping:
            alive()

            now = time.monotonic()
            timeout = min(0.02, max(0.0, next_block - now))
            # Stop reading when the queue is full so go-librespot blocks instead of
            # racing ahead; dropping audio here would skip music.
            events = selector.select(timeout) if len(pending) < MAX_QUEUE_BYTES else []
            if len(pending) >= MAX_QUEUE_BYTES:
                select.select([], [], [], timeout)
            for key, _ in events:
                try:
                    available = MAX_QUEUE_BYTES - len(pending)
                    chunk = os.read(key.fd, min(64 * 1024, available))
                except BlockingIOError:
                    continue
                if chunk:
                    pending.extend(chunk)

            if time.monotonic() >= next_block:
                # Emit one exact-duration block each tick. Padding makes pause,
                # stop and the gap before the next track continuous silence.
                usable = min(BLOCK_BYTES, len(pending))
                usable -= usable % BYTES_PER_FRAME
                block = bytearray(BLOCK_BYTES)
                if usable:
                    block[:usable] = pending[:usable]
                    del pending[:usable]
                send(block)
                next_block += BLOCK_SECONDS
                current = time.monotonic()
                if next_block < current - 0.1:
                    next_block = current
    finally:
        selector.unregister(source_fd)
        selector.close()
        os.close(source_fd)
        if output_fd is not None:
            os.close(output_fd)


def prepare_config() -> None:
    source = Path(os.environ.get("SPOTIFY_CONFIG_SOURCE", "/etc/jmm/spotify.yml"))
    config_dir = Path(os.environ.get("LIBRESPOT_CONFIG_DIR", "/var/lib/go-librespot"))
    config_dir.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        raise SystemExit(f"Spotify config file not found: {source}")
    shutil.copyfile(source, config_dir / "config.yml")


def prepare_fifo() -> None:
    path = Path(fifo_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_fifo():
            raise SystemExit(f"Audio path exists but is not a FIFO: {path}")
        return
    os.mkfifo(path, 0o600)


def prepare_vban_fifo(enabled: bool) -> None:
    if not enabled:
        return
    path = Path(os.environ.get("VBAN_FIFO", "/run/audio/vban.pcm"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_fifo():
            raise SystemExit(f"VBAN audio path exists but is not a FIFO: {path}")
        return
    os.mkfifo(path, 0o600)


def prepare_fifo_at(path_text: str, label: str) -> None:
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_fifo():
            raise SystemExit(f"{label} path exists but is not a FIFO: {path}")
        return
    os.mkfifo(path, 0o600)


def prepare_radio_fifo() -> None:
    path = Path(radio_fifo_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_fifo():
            raise SystemExit(f"Radio processing path exists but is not a FIFO: {path}")
        return
    os.mkfifo(path, 0o600)


def prepare_vban_receiver_fifo(enabled: bool) -> None:
    if not enabled:
        return
    path = Path(vban_receiver_fifo_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_fifo():
            raise SystemExit(f"VBAN receiver path exists but is not a FIFO: {path}")
        return
    os.mkfifo(path, 0o600)


def supervise() -> int:
    global stopping
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    prepare_config()
    prepare_fifo()
    prepare_radio_fifo()

    config_dir = os.environ.get("LIBRESPOT_CONFIG_DIR", "/var/lib/go-librespot")
    librespot_bin = os.environ.get("LIBRESPOT_BIN", "/usr/bin/go-librespot")
    liquidsoap_bin = os.environ.get("LIQUIDSOAP_BIN", "/usr/bin/liquidsoap")
    processing_script = radio_script_path()
    if not Path(processing_script).is_file():
        raise SystemExit(f"Liquidsoap processing script not found: {processing_script}")
    processing_graph = processing_graph_path()
    if not processing_graph.is_file():
        raise SystemExit(f"Processing graph not found: {processing_graph}")
    source_text = processing_graph.read_text(encoding="utf-8")
    try:
        processing = processing_graph_text(source_text)
    except ValueError as exc:
        raise SystemExit(f"{processing_graph}: {exc}") from exc
    vban_config = load_vban_config()
    vban_on = bool(vban_config.get("enabled"))
    prepare_vban_fifo(vban_on)
    receiver_config = load_vban_receiver_config()
    prepare_vban_receiver_fifo(bool(receiver_config.get("enabled")))
    # Control page, meters and live control. Control failures degrade; they never stop the stream.
    session = control.prepare(source_text, processing)
    vban: subprocess.Popen[bytes] | None = None
    receiver: subprocess.Popen[bytes] | None = None
    helpers: list[tuple[str, subprocess.Popen[bytes]]] = []
    encoder: subprocess.Popen[bytes] | None = None
    liquidsoap: subprocess.Popen[bytes] | None = None
    librespot: subprocess.Popen[bytes] | None = None
    bridge: Bridge | None = None
    graphs: control.GraphController | None = None
    try:
        if vban_on:
            vban = subprocess.Popen(vban_args(vban_config))
            helpers.append(("vban_emitter", vban))
        if receiver_config.get("enabled"):
            # The receptor opens its FIFO on the first packet; Liquidsoap reads it when it arrives.
            receiver = subprocess.Popen(vban_receiver_args(receiver_config), stdout=subprocess.DEVNULL)
            helpers.append(("vban_receptor", receiver))
        # The encoder runs for the life of the service. It reads the processor's f32 output.
        encoder = subprocess.Popen(
            encoder_args(vban_on, monitor_fd=session.monitor_fd),
            stdin=subprocess.PIPE,
            pass_fds=(session.monitor_fd,),
        )
        os.close(session.monitor_fd)
        assert encoder.stdin is not None
        radio_env = os.environ.copy()
        radio_env["RADIO_INPUT_FIFO"] = radio_fifo_path()
        # Liquidsoap writes a copy of the input mix here for the admin monitor's input meters.
        # Opened read-write so the open never blocks and the reader never sees EOF.
        monitor_fifo = control.monitor_input_fifo_path()
        prepare_fifo_at(monitor_fifo, "Input monitor")
        session.monitor.start_input(os.open(monitor_fifo, os.O_RDWR))
        radio_env["MONITOR_INPUT_FIFO"] = monitor_fifo
        # Server socket for the admin monitor's AGC bypass; remove one left by an earlier run.
        radio_env["LIQUIDSOAP_SOCKET"] = control.liquidsoap_socket_path()
        Path(radio_env["LIQUIDSOAP_SOCKET"]).unlink(missing_ok=True)
        radio_env.pop("VBAN_INPUT_FIFO", None)
        if receiver_config.get("enabled"):
            radio_env["VBAN_INPUT_FIFO"] = vban_receiver_fifo_path()
            radio_env["VBAN_INPUT_BUFFER"] = str(VBAN_RECEIVER_QUALITY[str(receiver_config["quality"])])
        liquidsoap = subprocess.Popen(
            [liquidsoap_bin, processing_script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            env=radio_env,
            start_new_session=True,
        )
        assert liquidsoap.stdout is not None
        shrink_pipe(encoder.stdin.fileno())
        bridge = Bridge(liquidsoap.stdout.fileno(), encoder.stdin.fileno())
        graphs = control.GraphController(
            bridge=bridge,
            session=session,
            path=processing_graph,
            source=source_text,
            processing=processing,
            processor_args=processor_args,
        )
        graphs.start()
        session.start(graphs)
        bridge.start()
        librespot = subprocess.Popen([librespot_bin, "--config_dir", config_dir])
        suffix = " and VBAN" if vban is not None else ""
        mixed = " with the VBAN input mixed in" if receiver is not None else ""
        print(f"Spotify Connect API{mixed} and Liquidsoap-processed AAC RTMP/Icecast{suffix} streams started", flush=True)

        def alive() -> None:
            if encoder is not None and encoder.poll() is not None:
                raise RuntimeError(f"FFmpeg encoder exited with status {encoder.returncode}")
            if liquidsoap is not None and liquidsoap.poll() is not None:
                raise RuntimeError(f"Liquidsoap exited with status {liquidsoap.returncode}")
            if librespot is not None and librespot.poll() is not None:
                raise RuntimeError(f"go-librespot exited with status {librespot.returncode}")
            check_helpers(helpers)
            if bridge is not None:
                bridge.check()

        pump_fifo(fifo_path(), alive)
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        if not stopping:
            print(f"Audio service failed: {exc}", file=sys.stderr)
            return 1
        return 0
    finally:
        stopping = True
        if graphs is not None:
            graphs.stop()
        if bridge is not None:
            bridge.stop()
        if encoder is not None and encoder.stdin is not None:
            try:
                encoder.stdin.close()
            except OSError:
                pass
        stop_processes(
            [librespot, liquidsoap, encoder, vban, receiver],
            process_groups=(liquidsoap,),
        )


if __name__ == "__main__":
    raise SystemExit(supervise())
