"""Tests for scripts/control.py: graph parsing, bypass wrapping, meters, HTTP API and live FFmpeg control.

The integration test needs an FFmpeg build with the azmq filter and pyzmq; it is skipped otherwise.
"""

from __future__ import annotations

import array
import http.client
import importlib
import io
import json
import os
import re
import shutil
import socket
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
# Makeup gain and wide-band ratio as set in processing.ffmpeg, so retuning the file does not break these tests.
_PROCESSING_TEXT = PROCESSING.read_text(encoding="utf-8")
MAKEUP_DB = float(re.search(r"volume@makeup=(-?[\d.]+)dB", _PROCESSING_TEXT).group(1))
WIDE_RATIO = float(re.search(r"acompressor@wide=threshold=[^:]+:ratio=([\d.]+)", _PROCESSING_TEXT).group(1))
sys.path.insert(0, str(SCRIPTS))
import control  # noqa: E402
import run as runtime  # noqa: E402
from control import (  # noqa: E402
    ControlServer,
    Listener,
    LiveControl,
    MicInput,
    Monitor,
    OptionInfo,
    TransportError,
    ZmqTransport,
    bypass_graph,
    format_db,
    measure,
    parse_help_options,
    parse_named_filters,
    read_chunks,
    substitute_values,
)

# Liveness as `ffmpeg -h filter=NAME` reports it for the filters in processing.ffmpeg.
HELP = {
    "agate": [OptionInfo(n, True) for n in ("threshold", "ratio", "range", "attack", "release", "makeup", "knee")],
    "acompressor": [OptionInfo(n, True) for n in ("threshold", "ratio", "attack", "release", "makeup", "knee")],
    "equalizer": [OptionInfo(n, True) for n in ("frequency", "f", "width_type", "t", "width", "w", "gain", "g")],
    "treble": [OptionInfo(n, True) for n in ("gain", "g", "frequency", "f", "width", "w")],
    "asoftclip": [OptionInfo("threshold", True), OptionInfo("type", True), OptionInfo("oversample", False)],
    "alimiter": [OptionInfo(n, True) for n in ("limit", "attack", "release")] + [OptionInfo("level", False)],
    "volume": [OptionInfo("volume", True), OptionInfo("precision", False)],
}


def static_options(name: str) -> list[OptionInfo]:
    return HELP.get(name, [])


def processing_graph() -> str:
    return runtime.load_processing_graph(PROCESSING)


def ffmpeg_path() -> str | None:
    return os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")


def ffmpeg_has_azmq() -> bool:
    ffmpeg = ffmpeg_path()
    return bool(ffmpeg) and control.has_filter(ffmpeg, "azmq")


def zmq_available() -> bool:
    try:
        importlib.import_module("zmq")
    except ImportError:
        return False
    return True


class HelpParsingTest(unittest.TestCase):
    def test_runtime_flag_is_read_from_the_help_line(self):
        text = (
            "Filter volume\n"
            "volume AVOptions:\n"
            "  volume            <string>     ..F.A....T. set volume adjustment expression (default \"1.0\")\n"
            "  precision         <int>        ..F.A...... select mathematical precision (from 0 to 2)\n"
            "     fixed           0            ..F.A...... select 8-bit fixed-point\n"
        )
        self.assertEqual(parse_help_options(text), [OptionInfo("volume", True), OptionInfo("precision", False)])


class GraphParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.filters = {item.id: item for item in parse_named_filters(processing_graph(), static_options)}

    def test_every_adjustable_filter_is_named_and_unnamed_ones_are_not(self):
        expected = {
            "agate@gate", "acompressor@wide", "equalizer@sub", "equalizer@air", "treble@shelf",
            "acompressor@band0", "acompressor@band1", "acompressor@band2", "acompressor@band3", "acompressor@band4",
            "asoftclip@band0clip", "alimiter@band0lim", "alimiter@band1lim", "alimiter@band2lim",
            "alimiter@band3lim", "alimiter@band4lim", "volume@band0gain", "volume@band1gain",
            "volume@band2gain", "volume@band3gain", "volume@makeup", "asoftclip@clip", "alimiter@final",
        }
        self.assertEqual(set(self.filters), expected)
        self.assertNotIn("volume@", next(iter(self.filters)))  # the first filter is the gate

    def test_numeric_options_only_and_dB_values_become_linear(self):
        sub = self.filters["equalizer@sub"]
        self.assertEqual([p.name for p in sub.params], ["f", "w", "g"])  # t=q is not numeric
        makeup = self.filters["volume@makeup"].params[0]
        self.assertEqual(makeup.name, "volume")
        self.assertTrue(makeup.db)
        self.assertAlmostEqual(makeup.value, 10 ** (MAKEUP_DB / 20), places=5)
        self.assertTrue(makeup.live)
        final = self.filters["alimiter@final"]
        self.assertNotIn("level", [p.name for p in final.params])

    def test_live_flags_follow_the_runtime_flag(self):
        oversample = parse_named_filters("[0:a:0]asoftclip@x=threshold=0.5:oversample=4[proc]", static_options)[0]
        flags = {p.name: p.live for p in oversample.params}
        self.assertEqual(flags, {"threshold": True, "oversample": False})

    def test_duplicate_names_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_named_filters("[0:a:0]agate@a=ratio=2,agate@a=ratio=3[proc]", static_options)


class BypassGraphTest(unittest.TestCase):
    def test_wraps_the_graph_with_an_azmq_input_and_a_dry_path(self):
        wrapped = bypass_graph(processing_graph(), 5555)
        self.assertTrue(wrapped.startswith(
            "[0:a:0]azmq=bind_address=tcp\\\\://127.0.0.1\\\\:5555,asplit=2[jmm_wet][jmm_dry];[jmm_wet]volume=12dB,"
        ))
        self.assertTrue(wrapped.endswith(
            "[jmm_wetout]volume@jmm_wet=1[jmm_w];[jmm_dry]volume=12dB,volume@jmm_dry=0[jmm_d];"
            "[jmm_w][jmm_d]amix=inputs=2:normalize=0[proc]"
        ))
        self.assertEqual(wrapped.count("[proc]"), 1)

    def test_requires_the_processing_graph_contract(self):
        with self.assertRaises(ValueError):
            bypass_graph("[1:a]volume=2[proc]", 5555)
        with self.assertRaises(ValueError):
            bypass_graph("[0:a:0]volume=2[stream]", 5555)


class ExportTest(unittest.TestCase):
    def test_format_db_uses_two_decimals_without_trailing_zeros(self):
        self.assertEqual(format_db(10 ** (6.8 / 20)), "6.8dB")
        self.assertEqual(format_db(10 ** (-1.9 / 20)), "-1.9dB")
        self.assertEqual(format_db(1.0), "0dB")
        self.assertEqual(format_db(0.0), "0")

    def test_changed_values_are_written_back_in_their_original_style(self):
        source = PROCESSING.read_text(encoding="utf-8")
        filters = parse_named_filters(processing_graph(), static_options)
        values = {(item.id, p.name): p.value for item in filters for p in item.params}
        self.assertEqual(substitute_values(source, static_options, values), source.rstrip("\n") + "\n")

        values[("volume@makeup", "volume")] = 10 ** (9 / 20)
        values[("acompressor@wide", "ratio")] = 2.5
        text = substitute_values(source, static_options, values)
        self.assertIn("volume@makeup=9dB,asoftclip@clip=", text)
        self.assertIn("acompressor@wide=threshold=0.316:ratio=2.5:", text)
        self.assertIn("volume=12dB", text)  # unnamed headroom stage is not touched
        self.assertEqual(
            [line for line in text.splitlines() if line.startswith("#")],
            [line for line in source.splitlines() if line.startswith("#")],
        )


class MeterTest(unittest.TestCase):
    def test_columns_hold_peak_and_rms_per_side(self):
        left = array.array("h", [16384] * 441)
        right = array.array("h", [-32768] * 441)
        samples = array.array("h", [0] * (441 * 2))
        samples[0::2] = left
        samples[1::2] = right
        self.assertEqual(measure(samples), [0.5, 1.0, 0.5, 1.0])

    def test_snapshot_returns_new_columns_once_per_client(self):
        read_fd, write_fd = os.pipe()
        monitor = Monitor(enabled=True)
        monitor.start(read_fd)
        try:
            for _ in range(3):
                monitor.feed_input(bytes(3528))
            cursor = {"in": 0, "out": 0}
            deadline = time.monotonic() + 5
            seen = 0
            while seen < 6 and time.monotonic() < deadline:
                seen += len(monitor.snapshot(cursor)["in"])
                time.sleep(0.01)
            self.assertEqual(seen, 6)
            self.assertEqual(monitor.snapshot(cursor)["in"], [])
        finally:
            os.close(write_fd)

    def test_disabled_monitor_still_drains_the_pipe(self):
        read_fd, write_fd = os.pipe()
        monitor = Monitor(enabled=False)
        monitor.start(read_fd)
        written = os.write(write_fd, bytes(70_000))  # larger than the pipe buffer: must not block
        os.close(write_fd)
        self.assertGreater(written, 0)
        monitor.feed_input(bytes(3528))
        self.assertEqual(monitor.snapshot({"in": 0, "out": 0}), {"in": [], "out": []})


class MonitorQueueTest(unittest.TestCase):
    """Monitor queueing, draining, idle trimming and snapshot cursors, through the public calls only."""

    def wait_for(self, condition, seconds: float = 5.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.01)
        return False

    def test_pipe_output_is_drained_and_measured_as_output(self):
        read_fd, write_fd = os.pipe()
        monitor = Monitor(enabled=True)
        monitor.start(read_fd)
        try:
            samples = array.array("h", [16384, -32768] * 441 * 10)  # ten 10 ms stereo columns
            data = samples.tobytes()
            written = 0
            while written < len(data):
                written += os.write(write_fd, data[written:written + 4096])
            cursor = {"in": 0, "out": 0}
            seen: list = []
            self.assertTrue(self.wait_for(lambda: (seen.extend(monitor.snapshot(cursor)["out"]), len(seen) >= 10)[1]))
            self.assertEqual(seen[:10], [[0.5, 1.0, 0.5, 1.0]] * 10)
            self.assertEqual(cursor["out"], 10)
        finally:
            os.close(write_fd)

    def test_input_blocks_are_measured_in_the_order_they_arrive(self):
        read_fd, write_fd = os.pipe()
        monitor = Monitor(enabled=True)
        monitor.start(read_fd)
        try:
            monitor.feed_input(bytes(control.COLUMN_BYTES))  # silence
            monitor.feed_input(array.array("h", [16384] * (control.COLUMN_BYTES // 2)).tobytes())
            cursor = {"in": 0, "out": 0}
            seen: list = []
            self.assertTrue(self.wait_for(lambda: (seen.extend(monitor.snapshot(cursor)["in"]), len(seen) >= 2)[1]))
            self.assertEqual(seen[:2], [[0.0, 0.0, 0.0, 0.0], [0.5, 0.5, 0.5, 0.5]])
        finally:
            os.close(write_fd)

    def test_a_snapshot_returns_at_most_the_newest_event_cap_and_then_nothing_new(self):
        read_fd, write_fd = os.pipe()
        monitor = Monitor(enabled=True)
        monitor.start(read_fd)
        try:
            monitor.feed_input(bytes(60 * control.COLUMN_BYTES))
            probe = {"in": 0, "out": 0}
            self.assertTrue(self.wait_for(lambda: (monitor.snapshot(probe), probe["in"] >= 60)[1]))
            cursor = {"in": 0, "out": 0}
            first = monitor.snapshot(cursor)["in"]
            self.assertEqual(len(first), control.EVENT_COLUMN_CAP)
            self.assertEqual(cursor["in"], 60)
            self.assertEqual(monitor.snapshot(cursor)["in"], [])
        finally:
            os.close(write_fd)

    def test_idle_monitor_trims_its_backlog_to_a_few_chunks(self):
        read_fd, write_fd = os.pipe()
        monitor = Monitor(enabled=True)
        monitor.start(read_fd)
        try:
            for _ in range(1000):  # nobody is watching, so the analyser only trims
                monitor.feed_input(bytes(3528))  # two columns per chunk
            time.sleep(0.3)
            cursor = {"in": 0, "out": 0}
            monitor.snapshot(cursor)  # starts watching; the kept backlog is analysed now
            time.sleep(0.5)
            monitor.snapshot(cursor)
            self.assertLessEqual(cursor["in"], control.IDLE_BYTES // control.COLUMN_BYTES)  # columns analysed
            self.assertGreater(cursor["in"], 0)
        finally:
            os.close(write_fd)

    def test_feeding_never_waits_for_a_stuck_analyser(self):
        read_fd, write_fd = os.pipe()
        release = threading.Event()
        monitor = Monitor(enabled=True)
        monitor._measure_side = lambda side, buffer: release.wait(10)
        monitor.start(read_fd)
        try:
            monitor.snapshot({"in": 0, "out": 0})  # watch, so the analyser reaches the stuck measurement
            feeder = threading.Thread(target=lambda: [monitor.feed_input(bytes(3528)) for _ in range(2000)])
            feeder.start()
            feeder.join(5)
            self.assertFalse(feeder.is_alive())
        finally:
            release.set()
            os.close(write_fd)

    def test_the_pipe_keeps_draining_while_the_analyser_is_stuck(self):
        read_fd, write_fd = os.pipe()
        release = threading.Event()
        monitor = Monitor(enabled=True)
        monitor._measure_side = lambda side, buffer: release.wait(10)
        monitor.start(read_fd)
        writer = threading.Thread(target=lambda: [os.write(write_fd, bytes(4096)) for _ in range(100)])
        try:
            monitor.snapshot({"in": 0, "out": 0})
            writer.start()
            writer.join(5)  # 400 KB through a 64 KB pipe: finishes only if the drain keeps reading
            self.assertFalse(writer.is_alive())
        finally:
            release.set()
            writer.join(5)
            os.close(write_fd)


class AudioQueueTest(unittest.TestCase):
    """The byte-bounded meter queues: bound, frame alignment, draining and thread safety."""

    def test_the_backlog_is_bounded_in_bytes_and_keeps_the_newest_frames(self):
        self.assertEqual(control.METER_BACKLOG_BYTES, 264_600)  # 1.5 s of stereo s16le at 44.1 kHz
        stream = bytes(i % 251 for i in range(3 * control.METER_BACKLOG_BYTES))
        sizes = [1, 3, 5, 4096, 65536, 7, 12345]
        queue = control._AudioQueue()
        position, step = 0, 0
        while position < len(stream):
            size = sizes[step % len(sizes)]
            queue.put(stream[position:position + size])
            position += size
            step += 1
            self.assertLessEqual(len(queue), control.METER_BACKLOG_BYTES)
        data = queue.take()
        self.assertGreater(len(data), control.METER_BACKLOG_BYTES - control.FRAME_BYTES)
        self.assertEqual(data, stream[len(stream) - len(data):])  # the newest audio survives
        self.assertEqual((len(stream) - len(data)) % control.FRAME_BYTES, 0)  # whole frames were dropped

    def test_take_returns_everything_once_and_in_order(self):
        queue = control._AudioQueue()
        queue.put(b"abc")
        queue.put(bytearray(b"defg"))
        self.assertEqual(len(queue), 7)
        self.assertEqual(queue.take(), b"abcdefg")
        self.assertEqual(queue.take(), b"")
        self.assertEqual(len(queue), 0)

    def test_trim_keeps_at_most_the_newest_bytes(self):
        queue = control._AudioQueue()
        queue.put(bytes(range(100)))
        queue.trim(20)
        self.assertEqual(queue.take(), bytes(range(80, 100)))
        queue.put(bytes(range(20)))
        queue.trim(10)
        kept = queue.take()
        self.assertLessEqual(len(kept), 10)
        self.assertEqual(kept, bytes(range(20))[20 - len(kept):])

    def test_concurrent_put_and_take_keep_the_newest_audio_intact(self):
        stream = bytes(i % 251 for i in range(400_000))
        queue = control._AudioQueue()
        finished = threading.Event()

        def produce() -> None:
            for start in range(0, len(stream), 4000):
                queue.put(stream[start:start + 4000])
            finished.set()

        producer = threading.Thread(target=produce)
        producer.start()
        taken = 0
        while not (finished.is_set() and len(queue) == 0):
            taken += len(queue.take())
        producer.join(5)
        self.assertFalse(producer.is_alive())
        self.assertGreater(taken, 0)
        self.assertEqual(queue.take(), b"")

    def test_monitor_drops_the_oldest_input_beyond_the_backlog(self):
        monitor = Monitor(enabled=True)  # no analyser: the backlog only grows until it is bounded
        chunk = bytes(3528)
        for _ in range(300):
            monitor.feed_input(chunk)
        self.assertEqual(len(monitor._input), control.METER_BACKLOG_BYTES)

    def test_a_disabled_monitor_queues_nothing(self):
        monitor = Monitor(enabled=False)
        monitor.feed_input(bytes(3528))
        self.assertEqual(len(monitor._input), 0)


class FakeTransport:
    def __init__(self, reply: str = "0 Success", error: bool = False) -> None:
        self.commands: list[str] = []
        self.reply = reply
        self.error = error

    def request(self, text: str) -> str:
        self.commands.append(text)
        if self.error:
            raise TransportError("no answer")
        return self.reply


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        graph = processing_graph()
        self.filters = parse_named_filters(graph, static_options)
        self.source = PROCESSING.read_text(encoding="utf-8")
        self.live = LiveControl(self.filters, self.source, static_options)
        self.transport = FakeTransport()
        self.live.attach(self.transport)
        read_fd, self.write_fd = os.pipe()
        self.monitor = Monitor(enabled=True)
        self.monitor.start(read_fd)
        self.server = ControlServer(("127.0.0.1", 0), self.live, self.monitor)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        os.close(self.write_fd)

    def request(self, method: str, path: str, body: object = None, content_type: str = "application/json",
                raw: bytes | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        headers = {"Content-Type": content_type} if content_type else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, response.getheader("Content-Type", ""), data

    def json_request(self, method: str, path: str, body: object = None):
        status, _, data = self.request(method, path, body)
        return status, json.loads(data)

    def test_state_lists_filters_in_graph_order_with_defaults(self):
        status, state = self.json_request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(state["control"])
        self.assertFalse(state["bypass"])
        ids = [item["id"] for item in state["filters"]]
        self.assertEqual(ids[0], "agate@gate")
        self.assertEqual(ids[-1], "alimiter@final")
        makeup = next(item for item in state["filters"] if item["id"] == "volume@makeup")
        volume = makeup["params"][0]
        self.assertEqual(volume["name"], "volume")
        self.assertAlmostEqual(volume["value"], volume["default"])
        self.assertAlmostEqual(volume["value"], 10 ** (MAKEUP_DB / 20), places=5)
        self.assertTrue(volume["live"])

    def test_param_sends_the_command_and_echoes_the_value(self):
        status, payload = self.json_request(
            "POST", "/api/param", {"target": "acompressor@wide", "name": "ratio", "value": 2.5}
        )
        self.assertEqual((status, payload), (200, {"ok": True, "value": 2.5}))
        self.assertEqual(self.transport.commands[-1], "acompressor@wide ratio 2.5")
        _, state = self.json_request("GET", "/api/state")
        wide = next(item for item in state["filters"] if item["id"] == "acompressor@wide")
        self.assertEqual(next(p for p in wide["params"] if p["name"] == "ratio")["value"], 2.5)

    def test_volume_is_linear_on_the_wire(self):
        self.json_request("POST", "/api/param", {"target": "volume@makeup", "name": "volume", "value": 1.5})
        self.assertEqual(self.transport.commands[-1], "volume@makeup volume 1.5")

    def test_invalid_requests_are_rejected_before_reaching_ffmpeg(self):
        cases = [
            {"target": "nope@x", "name": "ratio", "value": 2},
            {"target": "acompressor@wide", "name": "nosuch", "value": 2},
            {"target": "asoftclip@clip", "name": "oversample", "value": 2},
            {"target": "acompressor@wide", "name": "ratio", "value": "2"},
            {"target": "acompressor@wide", "name": "ratio", "value": True},
        ]
        for body in cases:
            with self.subTest(body=body):
                status, payload = self.json_request("POST", "/api/param", body)
                self.assertEqual(status, 400)
                self.assertIn("error", payload)
        status, _, _ = self.request("POST", "/api/param", raw=b'{"target":"acompressor@wide","name":"ratio","value":NaN}')
        self.assertEqual(status, 400)
        self.assertEqual(self.transport.commands, [])

    def test_body_rules(self):
        self.assertEqual(self.request("POST", "/api/bypass", {"bypass": True}, content_type="text/plain")[0], 415)
        self.assertEqual(self.request("POST", "/api/bypass", raw=b"x" * 5000)[0], 413)
        self.assertEqual(self.request("POST", "/api/bypass", raw=b"{nope")[0], 400)
        self.assertEqual(self.request("POST", "/api/bypass", {"bypass": "yes"})[0], 400)
        self.assertEqual(self.request("POST", "/api/unknown", {})[0], 404)
        self.assertEqual(self.request("POST", "/api/reset")[0], 200)
        self.assertEqual(self.request("POST", "/api/reset", content_type="text/plain")[0], 415)

    def test_ffmpeg_rejection_is_409_and_silence_is_503(self):
        self.transport.reply = "38 Function not implemented"
        status, payload = self.json_request("POST", "/api/param", {"target": "acompressor@wide", "name": "ratio", "value": 2})
        self.assertEqual(status, 409)
        self.assertIn("rejected", payload["error"])
        self.transport.reply, self.transport.error = "0 Success", True
        status, payload = self.json_request("POST", "/api/param", {"target": "acompressor@wide", "name": "ratio", "value": 2})
        self.assertEqual(status, 503)

    def test_bypass_and_reset(self):
        status, state = self.json_request("POST", "/api/bypass", {"bypass": True})
        self.assertEqual((status, state["bypass"]), (200, True))
        self.assertEqual(self.transport.commands[-2:], ["volume@jmm_wet volume 0", "volume@jmm_dry volume 1"])

        self.json_request("POST", "/api/param", {"target": "acompressor@wide", "name": "ratio", "value": 2.5})
        self.transport.commands.clear()
        status, state = self.json_request("POST", "/api/reset")
        self.assertEqual(status, 200)
        self.assertFalse(state["bypass"])
        self.assertIn(f"acompressor@wide ratio {WIDE_RATIO:g}", self.transport.commands)
        self.assertEqual(self.transport.commands[-2:], ["volume@jmm_wet volume 1", "volume@jmm_dry volume 0"])
        wide = next(item for item in state["filters"] if item["id"] == "acompressor@wide")
        self.assertEqual(next(p for p in wide["params"] if p["name"] == "ratio")["value"], WIDE_RATIO)

    def test_export_writes_live_values_into_the_file(self):
        self.json_request("POST", "/api/param", {"target": "acompressor@wide", "name": "ratio", "value": 2.5})
        status, content_type, data = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/plain"))
        text = data.decode("utf-8")
        self.assertIn("acompressor@wide=threshold=0.316:ratio=2.5:", text)
        self.assertIn("# Two peaking EQ bands", text)

    def test_meters_stream_columns_as_sse(self):
        for _ in range(4):
            self.monitor.feed_input(bytes(3528))
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/meters")
        response = conn.getresponse()
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        event = None
        for _ in range(200):
            line = response.fp.readline().decode("utf-8")
            if line.startswith("data:"):
                event = json.loads(line[5:])
                break
        conn.close()
        self.assertIsNotNone(event)
        self.assertGreater(len(event["in"]), 0)
        self.assertLessEqual(len(event["in"]), control.EVENT_COLUMN_CAP)
        self.assertEqual(len(event["in"][0]), 4)


class ControlDisabledTest(unittest.TestCase):
    def test_port_zero_disables_the_page_and_live_control(self):
        with patch.dict(os.environ, {"CONTROL_PORT": "0"}):
            session = control.prepare(PROCESSING.read_text(encoding="utf-8"), processing_graph())
        try:
            self.assertIsNone(session.server)
            self.assertIsNone(session.live)
            self.assertIsNone(session.zmq_base)
            self.assertGreaterEqual(session.monitor_fd, 0)
        finally:
            os.close(session.monitor_fd)

    def test_encoder_args_always_carry_the_monitor_output(self):
        env = {"RTMP_URL": "rtmp://localhost/live/x", "ICECAST_URL": "icecast://s:p@localhost:8000/x.aac"}
        with patch.dict(os.environ, env):
            args = runtime.encoder_args(monitor_fd=9)
            self.assertIn("[mon]", args[args.index("-filter_complex") + 1])
            self.assertEqual(args[args.index("-f") + 1], "f32le")
            self.assertEqual(args[-1], "pipe:9")
            vban_args = runtime.encoder_args(vban_enabled=True, monitor_fd=9)
            graph = vban_args[vban_args.index("-filter_complex") + 1]
            self.assertIn("asplit=3[stream][vban][mon]", graph)
            self.assertIn("[vban]", vban_args)
            self.assertEqual(vban_args[-1], "pipe:9")
            self.assertIn("-map", vban_args)

    def test_processor_reads_s16_and_writes_float_frames(self):
        args = runtime.processor_args(processing_graph())
        self.assertEqual(args[args.index("-f") + 1], "s16le")
        self.assertEqual(args[-1], "pipe:1")
        self.assertIn("pcm_f32le", args)
        self.assertIn("-nostdin", args)

    def test_zmq_port_in_use_is_reported_by_the_processor_preflight(self):
        """A port already in use makes azmq fail, so the swap is refused and the running graph stays."""
        if not (ffmpeg_path() and ffmpeg_has_azmq() and zmq_available()):
            self.skipTest("needs an FFmpeg build with azmq and pyzmq")
        blocker = socket_on_free_port()
        port = blocker.getsockname()[1]
        try:
            error = control.preflight_error(ffmpeg_path(), bypass_graph(processing_graph(), port))
            self.assertIsNotNone(error)
            self.assertIsNone(control.preflight_error(ffmpeg_path(), bypass_graph(processing_graph(), free_port())))
        finally:
            blocker.close()


def socket_on_free_port():
    import socket

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    return listener


@unittest.skipUnless(ffmpeg_path() and ffmpeg_has_azmq() and zmq_available(),
                     "needs an FFmpeg build with the azmq filter and pyzmq")
class LiveFFmpegTest(unittest.TestCase):
    """Runs the wrapped graph in FFmpeg on a tone and checks that commands are accepted."""

    def test_bypass_and_parameter_commands_are_accepted_by_ffmpeg(self):
        ffmpeg = ffmpeg_path()
        port = free_port()
        graph = bypass_graph(processing_graph(), port)
        full = runtime.audio_filtergraph(False, graph=graph, monitor=True)
        monitor_read, monitor_write = os.pipe()
        args = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100,aformat=channel_layouts=stereo",
            "-filter_complex", full,
            "-map", "[stream]", "-f", "null", "-",
            "-map", "[mon]", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", "-f", "s16le", f"pipe:{monitor_write}",
        ]
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, pass_fds=(monitor_write,))
        os.close(monitor_write)
        collected = bytearray()

        def drain() -> None:
            while True:
                chunk = os.read(monitor_read, 65536)
                if not chunk:
                    break
                collected.extend(chunk)

        threading.Thread(target=drain, daemon=True).start()
        try:
            filters = parse_named_filters(processing_graph(), static_options)
            transport = ZmqTransport(port)
            live = LiveControl(filters, PROCESSING.read_text(encoding="utf-8"), static_options)
            live.attach(transport)
            deadline = time.monotonic() + 15
            while True:
                try:
                    live.set_bypass(True)
                    break
                except Exception:
                    if time.monotonic() > deadline or proc.poll() is not None:
                        self.fail("FFmpeg did not answer the bypass command: "
                                  + proc.stderr.read().decode(errors="replace")[-400:])
                    time.sleep(0.2)
            self.assertTrue(live.state()["bypass"])
            live.set_bypass(False)
            self.assertEqual(live.set_param("acompressor@wide", "ratio", 2.5), 2.5)
            self.assertEqual(live.set_param("volume@makeup", "volume", 1.5), 1.5)
            self.assertEqual(live.set_param("equalizer@sub", "g", 2), 2.0)
            with self.assertRaises(control.ApiError) as rejected:
                live._send("agate@gate nosuch 2")
            self.assertEqual(rejected.exception.status, 409)
            time.sleep(0.5)
            self.assertIsNone(proc.poll(), "FFmpeg exited after the control commands")
        finally:
            transport.close()
            proc.kill()
            proc.wait()
            _, stderr = proc.communicate()
            time.sleep(0.1)
        self.assertGreater(len(collected), 0, "monitor pipe carried no audio")


def free_port() -> int:
    listener = socket_on_free_port()
    port = listener.getsockname()[1]
    listener.close()
    return port


class ListenerQueueTest(unittest.TestCase):
    def test_every_listener_gets_the_same_bytes_in_order(self):
        first, second = Listener(), Listener()
        for block in (b"\x01\x02\x03\x04", b"\x05\x06\x07\x08\x09\x0a\x0b\x0c"):
            first.push(block)
            second.push(block)
        expected = b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c"
        self.assertEqual(first.take(0), expected)
        self.assertEqual(second.take(0), expected)
        self.assertEqual(first.take(0), b"")

    def test_a_slow_listener_keeps_the_newest_whole_frames(self):
        listener = Listener(limit=16)
        stream = bytes(index % 256 for index in range(100))
        for start in range(0, len(stream), 7):  # blocks that do not line up with frames
            listener.push(stream[start:start + 7])
        data = listener.take(0)
        self.assertLessEqual(len(data), 16)
        self.assertEqual(len(data) % control.FRAME_BYTES, 0)
        self.assertEqual(data, stream[len(stream) - len(data):])
        self.assertEqual(listener.dropped + len(data), len(stream))

    def test_take_waits_for_audio_and_then_returns_empty(self):
        listener = Listener()
        self.assertEqual(listener.take(0.01), b"")
        threading.Timer(0.02, listener.push, args=(b"\x00\x01\x02\x03",)).start()
        self.assertEqual(listener.take(2.0), b"\x00\x01\x02\x03")


class ListenFeedTest(unittest.TestCase):
    def test_push_with_no_listeners_does_nothing(self):
        feed = control.ListenFeed()
        feed.push(bytes(3528))
        self.assertEqual(feed._listeners, ())

    def test_every_listener_gets_each_block_in_order(self):
        feed = control.ListenFeed()
        first, second = feed.attach(), feed.attach()
        feed.push(bytes(range(8)))
        feed.push(bytearray(range(8, 12)))
        self.assertEqual(first.take(0), bytes(range(12)))
        self.assertEqual(second.take(0), bytes(range(12)))

    def test_a_listener_that_never_reads_does_not_slow_push(self):
        feed = control.ListenFeed()
        feed.attach()  # never taken from
        block = bytes(3528)  # one 20 ms block
        started = time.monotonic()
        for _ in range(2000):  # 40 s of audio
            feed.push(block)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_detached_listener_gets_nothing_more(self):
        feed = control.ListenFeed()
        listener = feed.attach()
        feed.push(bytes(8))
        feed.detach(listener)
        feed.push(bytes(8))
        self.assertEqual(listener.take(0), bytes(8))
        self.assertEqual(feed._listeners, ())

    def test_the_monitor_output_is_not_offered_to_listeners(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        monitor = Monitor(enabled=False)
        monitor.start(read_fd)
        feed = control.ListenFeed()
        listener = feed.attach()
        os.write(write_fd, bytes(range(16)))  # the encoder's monitor output
        time.sleep(0.2)  # long enough for the drain to read it
        self.assertEqual(listener.take(0), b"")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "the pump reads a FIFO")
    def test_pump_offers_every_block_it_sends_to_liquidsoap(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        source = os.path.join(directory, "spotify.pcm")
        os.mkfifo(source)
        writer = os.open(source, os.O_RDWR)  # stands in for go-librespot and keeps the FIFO open
        self.addCleanup(os.close, writer)
        payload = bytes(range(256)) * 64  # whole frames, under a second of audio
        os.write(writer, payload)

        out_read, out_write = os.pipe()
        self.addCleanup(os.close, out_read)
        received = bytearray()  # what Liquidsoap reads from the input FIFO

        def read_out() -> None:
            while True:
                chunk = os.read(out_read, 65536)
                if not chunk:
                    return
                received.extend(chunk)

        reader = threading.Thread(target=read_out, daemon=True)
        feed = control.ListenFeed()
        listener = feed.attach()
        heard = bytearray()
        with patch.object(runtime, "stopping", False), \
                patch.object(runtime, "open_radio_fifo_writer", return_value=out_write):
            pump = threading.Thread(target=runtime.pump_fifo, args=(source, lambda: None, feed), daemon=True)
            reader.start()
            pump.start()
            deadline = time.monotonic() + 5
            while len(heard) < len(payload) and time.monotonic() < deadline:
                heard += listener.take(0.1)
            runtime.stopping = True
            pump.join(5)
            reader.join(5)
        heard += listener.take(0)
        self.assertFalse(pump.is_alive())
        self.assertEqual(bytes(heard[:len(payload)]), payload)  # the input, in order, before processing
        self.assertEqual(len(heard) % control.FRAME_BYTES, 0)
        self.assertEqual(bytes(received), bytes(heard))  # listeners hear exactly the blocks Liquidsoap gets

    def test_listen_endpoint_streams_the_input_until_the_client_leaves(self):
        live = LiveControl(parse_named_filters(processing_graph(), static_options), PROCESSING.read_text(encoding="utf-8"), static_options)
        feed = control.ListenFeed()
        server = ControlServer(("127.0.0.1", 0), live, Monitor(enabled=False), listen=feed)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("GET", "/api/listen")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "application/octet-stream")
            self.assertIsNone(response.getheader("Content-Length"))
            deadline = time.monotonic() + 5
            while not feed._listeners and time.monotonic() < deadline:
                time.sleep(0.005)
            payload = bytes(range(16))
            feed.push(payload)
            self.assertEqual(response.read(16), payload)
            conn.close()
            deadline = time.monotonic() + 5
            while feed._listeners and time.monotonic() < deadline:
                feed.push(b"\x00" * 4)  # the next block finds the closed client
                time.sleep(0.05)
            self.assertEqual(feed._listeners, ())
        finally:
            server.shutdown()
            server.server_close()


class ChunkedBodyTest(unittest.TestCase):
    def test_sizes_extensions_and_trailers_are_handled(self):
        body = b"4\r\nabcd\r\n3;ext=1\r\nxyz\r\nA\r\n0123456789\r\n0\r\nTrailer: x\r\n\r\n"
        self.assertEqual(list(read_chunks(io.BytesIO(body))), [b"abcd", b"xyz", b"0123456789"])
        self.assertEqual(list(read_chunks(io.BytesIO(b"0\r\n\r\n"))), [])

    def test_malformed_and_short_bodies_are_400(self):
        bodies = (
            b"x\r\n",             # not hex
            b"+2\r\nab\r\n0\r\n\r\n",  # signs are not part of a chunk size
            b"4\r\nab",           # body ends inside a chunk
            b"2\r\nabXY0\r\n\r\n",  # missing CRLF after the data
            b"4\r\nabcd",         # no terminator and no final chunk
            b"2\r\nab\r\n0\r\nTrailer",  # trailer without its end
        )
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(control.ApiError) as raised:
                list(read_chunks(io.BytesIO(body)))
            self.assertEqual(raised.exception.status, 400)


class FakeConnection:
    def __init__(self) -> None:
        self.shut: list[int] = []

    def shutdown(self, how: int) -> None:
        self.shut.append(how)


class MicInputTest(unittest.TestCase):
    def setUp(self) -> None:
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        os.set_blocking(self.write_fd, False)
        self.mic = MicInput(self.write_fd)

    def tearDown(self) -> None:
        os.close(self.read_fd)
        os.close(self.write_fd)

    def drained(self) -> bytes:
        out = bytearray()
        while True:
            try:
                chunk = os.read(self.read_fd, 65536)
            except BlockingIOError:
                return bytes(out)
            if not chunk:
                return bytes(out)
            out += chunk

    def test_mono_samples_go_to_both_channels(self):
        session = self.mic.begin(FakeConnection())
        self.mic.write(session, b"\x01\x02\x03\x04")
        self.assertEqual(self.drained(), b"\x01\x02\x01\x02\x03\x04\x03\x04")

    def test_a_sample_split_across_chunks_is_joined(self):
        session = self.mic.begin(FakeConnection())
        self.mic.write(session, b"\x01")  # half a sample: held until its pair arrives
        self.assertEqual(self.drained(), b"")
        self.mic.write(session, b"\x02\x03\x04\x05")  # two whole samples and one byte held back
        self.assertEqual(self.drained(), b"\x01\x02\x01\x02\x03\x04\x03\x04")

    def test_a_new_session_ends_the_previous_one(self):
        first, second = FakeConnection(), FakeConnection()
        old = self.mic.begin(first)
        new = self.mic.begin(second)
        self.assertEqual(first.shut, [socket.SHUT_RD])
        self.assertEqual(second.shut, [])
        self.assertFalse(self.mic.is_current(old))
        self.assertTrue(self.mic.is_current(new))
        self.mic.write(old, b"\x09\x00")  # ignored once superseded
        self.assertEqual(self.drained(), b"")
        self.mic.write(new, b"\x07\x00")
        self.assertEqual(self.drained(), b"\x07\x00\x07\x00")
        self.mic.end(new)
        self.assertFalse(self.mic.is_current(new))

    def test_a_full_fifo_drops_blocks_instead_of_waiting(self):
        session = self.mic.begin(FakeConnection())
        worker = threading.Thread(target=self.mic.write, args=(session, b"\x01\x00" * 200_000), daemon=True)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "a full FIFO held up the writer")
        data = self.drained()
        self.assertGreater(len(data), 0)
        self.assertEqual(len(data) % 4, 0, "dropped blocks must keep the stream frame-aligned")


class MicEndpointTest(unittest.TestCase):
    def serve(self, mic: MicInput | None) -> ControlServer:
        live = LiveControl(parse_named_filters(processing_graph(), static_options), PROCESSING.read_text(encoding="utf-8"), static_options)
        server = ControlServer(("127.0.0.1", 0), live, Monitor(enabled=False), mic=mic)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_disabled_mic_answers_404(self):
        server = self.serve(None)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request("POST", "/api/mic", body=[b"\x01\x00"], headers={"Content-Type": "application/octet-stream"}, encode_chunked=True)
        response = conn.getresponse()
        self.assertEqual(response.status, 404)
        self.assertEqual(json.loads(response.read()), {"error": "mic input is disabled"})
        conn.close()

    def test_chunked_body_is_written_as_stereo_and_answered_ok(self):
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        server = self.serve(MicInput(write_fd))
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request("POST", "/api/mic", body=[b"\x01\x02", b"\x03\x04"], encode_chunked=True)
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read()), {"ok": True})
        conn.close()
        self.assertEqual(os.read(read_fd, 4096), b"\x01\x02\x01\x02\x03\x04\x03\x04")

    def test_a_body_that_is_not_chunked_is_400(self):
        server = self.serve(MicInput(os.open(os.devnull, os.O_WRONLY)))
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        conn.request("POST", "/api/mic", body=b"\x01\x00", headers={"Content-Length": "2"})
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        conn.close()

    def test_source_reports_the_live_input_level_key(self):
        server = self.serve(None)
        with patch.object(LiveControl, "live_level_db", return_value=-21.5):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("GET", "/api/source")
            response = conn.getresponse()
            self.assertEqual(json.loads(response.read()), {"live_level_db": -21.5})
            conn.close()


if __name__ == "__main__":
    unittest.main()
