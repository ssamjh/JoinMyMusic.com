"""Tests for scripts/control.py: graph parsing, bypass wrapping, meters, HTTP API and live FFmpeg control.

The integration test needs an FFmpeg build with the azmq filter and pyzmq; it is skipped otherwise.
"""

from __future__ import annotations

import array
import http.client
import importlib
import json
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
import control  # noqa: E402
import run as runtime  # noqa: E402
from control import (  # noqa: E402
    ControlServer,
    LiveControl,
    Monitor,
    OptionInfo,
    TransportError,
    ZmqTransport,
    bypass_graph,
    format_db,
    measure,
    parse_help_options,
    parse_named_filters,
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
        self.assertAlmostEqual(makeup.value, 10 ** (8 / 20), places=5)
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
        self.assertAlmostEqual(volume["value"], 10 ** (8 / 20), places=5)
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
        self.assertIn("acompressor@wide ratio 3", self.transport.commands)
        self.assertEqual(self.transport.commands[-2:], ["volume@jmm_wet volume 1", "volume@jmm_dry volume 0"])
        wide = next(item for item in state["filters"] if item["id"] == "acompressor@wide")
        self.assertEqual(next(p for p in wide["params"] if p["name"] == "ratio")["value"], 3.0)

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


if __name__ == "__main__":
    unittest.main()
