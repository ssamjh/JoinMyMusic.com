"""Tests for the processing graph controller and the /api/graph endpoints (scripts/control.py).

The bridge and the FFmpeg preflight are replaced by fakes here, so these tests need no FFmpeg.
The real hot-swap through FFmpeg is covered by test_hotswap_ffmpeg.py.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PROCESSING = ROOT / "config" / "processing.ffmpeg"
sys.path.insert(0, str(SCRIPTS))
import bridge as B  # noqa: E402
import control  # noqa: E402
from control import ApiError, ControlServer, GraphController, LiveControl, Monitor, OptionInfo  # noqa: E402

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


def processing_source() -> str:
    return PROCESSING.read_text(encoding="utf-8")


WIDE_RATIO = re.compile(r"(acompressor@wide=threshold=[^:]+:ratio=)([\d.]+)")


def edited(source: str, old: str, new: str) -> str:
    assert old in source, "test text is out of date"
    return source.replace(old, new)


def with_ratio(source: str, ratio: str = "2.5") -> str:
    """The source with the wide compressor ratio set to ratio, whatever value processing.ffmpeg holds."""
    assert WIDE_RATIO.search(source), "acompressor@wide ratio is missing from processing.ffmpeg"
    return WIDE_RATIO.sub(lambda match: match.group(1) + ratio, source, count=1)


def config_ratio(source: str) -> str:
    return WIDE_RATIO.search(source).group(2)


class FakeProcess:
    def __init__(self, args: list[str]) -> None:
        self.args = args
        self.outcome: str | None = None
        self.stopped = False
        self.start_tag = None

    def stop(self) -> None:
        self.stopped = True


class FakeTransport:
    """Accepts every azmq command, as FFmpeg does for a valid parameter."""

    def request(self, text: str) -> str:
        return "0 Success"

    def close(self) -> None:
        return None


class FakeBridge:
    """Records offers and reports them committed, unless told to fail the next one."""

    def __init__(self) -> None:
        self.offers: list[tuple[FakeProcess, int, int]] = []
        self.on_failure = None
        self.active = False
        self.fail_next: str | None = None

    def wait_retired(self, timeout: float) -> None:
        return None

    def offer(self, processor: FakeProcess, warmup_frames: int = 0, crossfade_frames: int = 0) -> None:
        self.offers.append((processor, warmup_frames, crossfade_frames))

    def wait_outcome(self, processor: FakeProcess, timeout: float) -> None:
        if self.fail_next is not None:
            reason, self.fail_next = self.fail_next, None
            raise B.SwapFailed(reason)
        processor.outcome = B.COMMITTED
        self.active = True

    def active_is_processor(self) -> bool:
        return self.active

    def fail_active(self, processor: FakeProcess, reason: str) -> None:
        """What the bridge does when the running processor dies."""
        self.active = False
        self.on_failure(processor, reason)


def preflight(ffmpeg: str, graph: str) -> str | None:
    return "FFmpeg rejected the graph: unknown filter" if "BROKEN" in graph else None


def make_controller(path: Path | None, source: str, *, live: bool = True, **options):
    bridge = FakeBridge()
    filters = control.parse_named_filters(control.processing_graph_text(source), static_options) if live else []
    monitor = Monitor(enabled=False)
    read_fd, write_fd = os.pipe()
    monitor.start(read_fd)
    session = control.Session(
        monitor_fd=write_fd,
        monitor=monitor,
        ffmpeg="ffmpeg",
        live=LiveControl(filters, source, static_options) if live else None,
        zmq_base=None,
        filters=filters,
        filter_help=SimpleNamespace(options=static_options),
    )
    controller = GraphController(
        bridge=bridge,
        session=session,
        path=path,
        source=source,
        processing=control.processing_graph_text(source),
        processor_args=lambda graph: ["fake-ffmpeg", graph],
        spawn=FakeProcess,
        preflight_fn=preflight,
        watch_interval=0.05,
        watch_settle=0.05,
        **options,
    )
    return controller, bridge, session


class GraphControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="jmm-graph-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "processing.ffmpeg"
        self.source = processing_source()
        self.path.write_text(self.source, encoding="utf-8")

    def started(self, **options):
        controller, bridge, session = make_controller(self.path, self.source, **options)
        self.addCleanup(controller.stop)
        controller.start()
        self.wait_for(lambda: bridge.active and not controller._swap_lock.locked(), "startup processor did not take over")
        return controller, bridge, session

    def wait_for(self, condition, message: str, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        self.fail(message)

    def test_startup_starts_the_file_as_the_first_processor_without_warm_up(self):
        controller, bridge, _ = self.started()
        processor, warmup, crossfade = bridge.offers[0]
        self.assertEqual((warmup, crossfade), (0, 0))
        self.assertEqual(processor.args[0], "fake-ffmpeg")
        state = controller.state()
        self.assertEqual(state["origin"], "file")
        self.assertEqual(state["swap"]["state"], "idle")
        self.assertIsNotNone(state["swap"]["applied_at"])
        self.assertEqual(state["path"], str(self.path))

    def test_valid_edit_is_swapped_in_with_warm_up_and_marked_edited(self):
        controller, bridge, session = self.started()
        result = controller.apply(with_ratio(self.source))
        self.assertEqual(result["origin"], "edited")
        self.assertIn("ratio=2.5", result["text"])
        _, warmup, crossfade = bridge.offers[-1]
        self.assertEqual((warmup, crossfade), (44_100, B.CROSSFADE_FRAMES))
        filters = {item["id"]: item for item in session.live.state()["filters"]}
        ratio = next(p["value"] for p in filters["acompressor@wide"]["params"] if p["name"] == "ratio")
        self.assertEqual(ratio, 2.5, "live values must follow the new graph")

    def test_invalid_graph_is_refused_and_the_running_text_stays(self):
        controller, bridge, _ = self.started()
        before = controller.state()["text"]
        offers = len(bridge.offers)
        with self.assertRaises(ApiError) as refused:
            controller.apply(edited(self.source, "agate@gate=threshold", "BROKEN@gate=threshold"))
        self.assertEqual(refused.exception.status, 400)
        self.assertIn("unknown filter", refused.exception.message)
        self.assertEqual(len(bridge.offers), offers, "an invalid graph must not reach the bridge")
        state = controller.state()
        self.assertEqual(state["text"], before)
        self.assertEqual(state["swap"]["state"], "error")

    def test_malformed_graph_contract_is_a_400(self):
        controller, _, _ = self.started()
        with self.assertRaises(ApiError) as refused:
            controller.apply("[1:a:0]anull[proc]")
        self.assertEqual(refused.exception.status, 400)
        with self.assertRaises(ApiError) as duplicate:
            controller.apply(self.source + "\n[0:a:0]anull,acompressor@wide=ratio=2[x];")
        self.assertEqual(duplicate.exception.status, 400)

    def test_swap_while_another_runs_is_409(self):
        controller, _, _ = self.started()
        self.assertTrue(controller._swap_lock.acquire(blocking=False))
        try:
            with self.assertRaises(ApiError) as busy:
                controller.apply(with_ratio(self.source))
            self.assertEqual(busy.exception.status, 409)
        finally:
            controller._swap_lock.release()

    def test_candidate_that_does_not_take_over_is_a_503_and_keeps_the_old_graph(self):
        controller, bridge, _ = self.started()
        before = controller.state()["text"]
        bridge.fail_next = "it exited with status 3"
        with self.assertRaises(ApiError) as failed:
            controller.apply(with_ratio(self.source))
        self.assertEqual(failed.exception.status, 503)
        self.assertIn("previous one keeps running", failed.exception.message)
        self.assertEqual(controller.state()["text"], before)
        self.assertEqual(controller.swap_state()["state"], "error")

    def test_reload_runs_the_file_contents(self):
        controller, _, _ = self.started()
        controller.apply(with_ratio(self.source))
        state = controller.reload()
        self.assertEqual(state["origin"], "file")
        self.assertIn(f"ratio={config_ratio(self.source)}", state["text"])

    def test_file_change_is_applied_once_it_settles_and_bad_changes_are_not(self):
        controller, bridge, _ = self.started()
        offers = len(bridge.offers)
        self.path.write_text(with_ratio(self.source, "2"), encoding="utf-8")
        self.wait_for(lambda: len(bridge.offers) > offers, "a settled file change was not applied")
        self.assertIn("ratio=2", controller.state()["text"])
        self.assertEqual(controller.state()["origin"], "file")

        offers = len(bridge.offers)
        self.path.write_text(self.source.replace("[proc]", "[proc"), encoding="utf-8")
        self.wait_for(lambda: controller.swap_state()["state"] == "error", "a broken file was not reported")
        self.assertEqual(len(bridge.offers), offers)
        self.assertIn("ratio=2", controller.state()["text"], "the running graph must not change")

    def test_failed_processor_is_restarted_with_the_running_graph_and_live_values(self):
        controller, bridge, session = self.started()
        controller.apply(with_ratio(self.source))
        session.live.attach(FakeTransport())
        live_value = session.live.set_param("acompressor@wide", "ratio", 1.75)
        self.assertEqual(live_value, 1.75)
        offers = len(bridge.offers)
        active = bridge.offers[-1][0]
        bridge.fail_active(active, "exited with status 4")
        self.wait_for(lambda: len(bridge.offers) > offers, "the failed processor was not restarted")
        restarted, warmup, crossfade = bridge.offers[-1]
        self.assertEqual((warmup, crossfade), (44_100, B.CROSSFADE_FRAMES))
        self.assertIn("ratio=1.75", restarted.args[1], "the restart must keep the live values")
        self.wait_for(lambda: controller.swap_state()["state"] == "idle", "recovery did not clear the error")

    def test_startup_failure_retries_until_a_processor_takes_over(self):
        controller, bridge, _ = make_controller(self.path, self.source)
        self.addCleanup(controller.stop)
        attempts = []
        original = bridge.wait_outcome

        def flaky(processor, timeout):
            attempts.append(processor)
            if len(attempts) == 1:
                raise B.SwapFailed("it exited with status 3")
            original(processor, timeout)

        bridge.wait_outcome = flaky
        controller.start()
        self.wait_for(lambda: bridge.active, "startup did not retry", timeout=5)
        self.assertGreaterEqual(len(attempts), 2)

    def test_startup_graph_that_ffmpeg_rejects_stops_the_service(self):
        bad = Path(self.temp.name) / "bad.ffmpeg"
        bad_source = self.source.replace("[proc]", "BROKEN[proc]")
        bad.write_text(bad_source, encoding="utf-8")
        controller, _, _ = make_controller(bad, bad_source, live=False)
        with self.assertRaises(SystemExit):
            controller.start()


class GraphHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="jmm-graph-http-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "processing.ffmpeg"
        self.source = processing_source()
        self.path.write_text(self.source, encoding="utf-8")
        self.controller, self.bridge, self.session = make_controller(self.path, self.source)
        self.addCleanup(self.controller.stop)
        self.controller.start()
        deadline = time.monotonic() + 5
        while (not self.bridge.active or self.controller._swap_lock.locked()) and time.monotonic() < deadline:
            time.sleep(0.02)
        server = ControlServer(("127.0.0.1", 0), self.session.live, self.session.monitor, graphs=self.controller)
        self.server = server
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(self._close)

    def _close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def request(self, method: str, path: str, body: object = None, content_type: str = "application/json",
                raw: bytes | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        headers = {"Content-Type": content_type} if content_type else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, json.loads(data) if data else {}

    def test_get_graph_reports_text_origin_and_swap(self):
        status, payload = self.request("GET", "/api/graph")
        self.assertEqual(status, 200)
        self.assertEqual(payload["origin"], "file")
        self.assertIn("[0:a:0]", payload["text"])
        self.assertEqual(payload["swap"]["state"], "idle")
        self.assertIn("error", payload["swap"])
        self.assertIn("applied_at", payload["swap"])

    def test_state_includes_the_swap_object(self):
        status, payload = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload["swap"]["state"], "idle")
        self.assertIn("filters", payload)

    def test_post_graph_applies_an_edit(self):
        status, payload = self.request("POST", "/api/graph", {"text": with_ratio(self.source)})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["origin"], "edited")
        self.assertIn("ratio=2.5", payload["text"])
        self.assertEqual(payload["swap"]["state"], "idle")

    def test_post_graph_rejects_bad_input_with_400(self):
        before = self.request("GET", "/api/graph")[1]["text"]
        status, payload = self.request("POST", "/api/graph", {"text": "BROKEN[proc]"})
        self.assertEqual(status, 400)
        self.assertIn("error", payload)
        status, payload = self.request("POST", "/api/graph", {"text": 5})
        self.assertEqual(status, 400)
        status, payload = self.request("POST", "/api/graph", {"text": "   "})
        self.assertEqual(status, 400)
        self.assertEqual(self.request("GET", "/api/graph")[1]["text"], before)

    def test_body_rules_for_graph_posts(self):
        status, _ = self.request("POST", "/api/graph", raw=b'{"text":"x"}', content_type="text/plain")
        self.assertEqual(status, 415)
        status, _ = self.request("POST", "/api/graph", raw=b"{not json")
        self.assertEqual(status, 400)
        huge = json.dumps({"text": "x" * 70_000}).encode()
        status, _ = self.request("POST", "/api/graph", raw=huge)
        self.assertEqual(status, 413)

    def test_post_graph_while_a_swap_runs_is_409(self):
        self.assertTrue(self.controller._swap_lock.acquire(blocking=False))
        try:
            status, payload = self.request("POST", "/api/graph", {"text": with_ratio(self.source)})
        finally:
            self.controller._swap_lock.release()
        self.assertEqual(status, 409)
        self.assertIn("error", payload)

    def test_post_graph_reports_a_candidate_that_failed_as_503(self):
        self.bridge.fail_next = "it exited with status 3"
        status, payload = self.request("POST", "/api/graph", {"text": with_ratio(self.source)})
        self.assertEqual(status, 503)
        self.assertIn("previous one keeps running", payload["error"])

    def test_reload_restores_the_file(self):
        self.request("POST", "/api/graph", {"text": with_ratio(self.source)})
        status, payload = self.request("POST", "/api/graph/reload", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["origin"], "file")
        self.assertIn(f"ratio={config_ratio(self.source)}", payload["text"])

    def test_export_follows_the_applied_graph_and_live_values(self):
        self.request("POST", "/api/graph", {"text": with_ratio(self.source)})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/export")
        text = conn.getresponse().read().decode()
        conn.close()
        self.assertIn("ratio=2.5", text)


if __name__ == "__main__":
    unittest.main()
