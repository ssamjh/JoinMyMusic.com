"""Live network recovery tests for the production FFmpeg command.

These tests run FFmpeg with paced raw PCM and exercise Icecast startup outages
and dropped connections against a tiny local HTTP source mock. Run in the
project Linux image with `python3 -m unittest tests.test_reconnect`.
"""

from __future__ import annotations

import os
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from scripts import run as production_run
except ImportError:  # pragma: no cover - only for unusual standalone test layouts
    production_run = None


SAMPLE_RATE = 44_100
CHANNELS = 2
BLOCK_BYTES = SAMPLE_RATE * CHANNELS * 4 // 50  # 20 ms of F32LE stereo, the encoder's input.


def unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _ReusableServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False


class IcecastSourceMock:
    """Accept FFmpeg's Icecast HTTP source request and count audio bytes."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._condition = threading.Condition()
        self.accepted = 0
        self.bytes_received = 0
        self.request_headers: list[bytes] = []
        self.connection_bytes: list[int] = []
        self.sockets: set[socket.socket] = set()
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                conn: socket.socket = self.request
                conn.settimeout(0.5)
                with owner._condition:
                    owner.accepted += 1
                    owner.sockets.add(conn)
                    owner.connection_bytes.append(0)
                    connection_index = len(owner.connection_bytes) - 1
                    owner._condition.notify_all()
                header = bytearray()
                try:
                    while b"\r\n\r\n" not in header and len(header) < 16_384:
                        chunk = conn.recv(4096)
                        if not chunk:
                            return
                        header.extend(chunk)
                    marker = header.find(b"\r\n\r\n") + 4
                    body = len(header) - marker
                    with owner._condition:
                        owner.request_headers.append(bytes(header[:marker]))
                        owner.bytes_received += body
                        owner.connection_bytes[connection_index] += body
                        owner._condition.notify_all()
                    # FFmpeg's Icecast protocol expects an HTTP success response.
                    conn.sendall(b"HTTP/1.0 200 OK\r\nConnection: keep-alive\r\n\r\n")
                    while True:
                        try:
                            chunk = conn.recv(16_384)
                        except socket.timeout:
                            continue
                        if not chunk:
                            return
                        with owner._condition:
                            owner.bytes_received += len(chunk)
                            owner.connection_bytes[connection_index] += len(chunk)
                            owner._condition.notify_all()
                except (ConnectionError, OSError):
                    return
                finally:
                    with owner._condition:
                        owner.sockets.discard(conn)
                        owner._condition.notify_all()

        self._server = _ReusableServer(("127.0.0.1", port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wait_for(
        self,
        *,
        accepted: int = 1,
        byte_count: int = 1,
        connection_index: int | None = None,
        connection_byte_count: int = 1,
        timeout: float = 6,
    ) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while (
                self.accepted < accepted
                or self.bytes_received < byte_count
                or (
                    connection_index is not None
                    and (
                        len(self.connection_bytes) <= connection_index
                        or self.connection_bytes[connection_index] < connection_byte_count
                    )
                )
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def disconnect_clients(self) -> None:
        with self._condition:
            active = list(self.sockets)
        for conn in active:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self.disconnect_clients()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=1)


class ReconnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if os.name != "posix":
            raise unittest.SkipTest("the production FFmpeg pipe command requires Linux/POSIX")
        if production_run is None:
            raise unittest.SkipTest("could not import scripts/run.py")
        if not (os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")):
            raise unittest.SkipTest("FFmpeg is unavailable; run these tests in the project image")

    def setUp(self) -> None:
        self._saved_env = {name: os.environ.get(name) for name in ("RTMP_URL", "ICECAST_URL", "STREAM_BITRATE")}
        os.environ["STREAM_BITRATE"] = "256k"
        self.proc: subprocess.Popen[bytes] | None = None
        self.writer_stop = threading.Event()
        self.writer: threading.Thread | None = None
        self.mock: IcecastSourceMock | None = None

    def tearDown(self) -> None:
        self.writer_stop.set()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        if self.proc is not None and self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        if self.proc is not None and self.proc.stderr is not None:
            self.proc.stderr.close()
        if self.writer is not None:
            self.writer.join(timeout=1)
        if self.mock is not None:
            self.mock.close()
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def start_ffmpeg(self, rtmp_port: int, icecast_port: int) -> subprocess.Popen[bytes]:
        os.environ["RTMP_URL"] = f"rtmp://127.0.0.1:{rtmp_port}/live/test"
        os.environ["ICECAST_URL"] = f"icecast://source:secret@127.0.0.1:{icecast_port}/test.aac"
        monitor_read, monitor_write = os.pipe()
        args = production_run.encoder_args(vban_enabled=False, monitor_fd=monitor_write)
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0,
            pass_fds=(monitor_write,),
        )
        os.close(monitor_write)

        def drain_monitor() -> None:
            while os.read(monitor_read, 65536):
                pass
            os.close(monitor_read)

        threading.Thread(target=drain_monitor, daemon=True).start()
        self.proc = proc
        assert proc.stdin is not None
        block = bytes(BLOCK_BYTES)

        def feed() -> None:
            while not self.writer_stop.is_set():
                try:
                    proc.stdin.write(block)
                except (BrokenPipeError, OSError, ValueError):
                    return
                self.writer_stop.wait(0.02)

        self.writer = threading.Thread(target=feed, daemon=True)
        self.writer.start()
        return proc

    def test_initial_icecast_and_rtmp_outages_do_not_stop_stream(self) -> None:
        """Start with both endpoints down, then bring Icecast online."""
        icecast_port, rtmp_port = unused_port(), unused_port()
        proc = self.start_ffmpeg(rtmp_port, icecast_port)
        time.sleep(2.0)
        self.assertIsNone(proc.poll(), "FFmpeg exited while the network destinations were unavailable")

        self.mock = IcecastSourceMock(icecast_port)
        self.mock.start()
        self.assertTrue(self.mock.wait_for(accepted=1, byte_count=1024, connection_index=0, connection_byte_count=512, timeout=7), "Icecast did not reconnect and receive audio")
        self.assertIn(b"content-type: audio/aac", self.mock.request_headers[0].lower())
        self.assertIsNone(proc.poll(), "FFmpeg stopped after Icecast recovered")

    def test_icecast_drop_during_playback_recovers_without_ffmpeg_restart(self) -> None:
        """An established Icecast session can drop and reconnect while RTMP is down."""
        icecast_port, rtmp_port = unused_port(), unused_port()
        self.mock = IcecastSourceMock(icecast_port)
        self.mock.start()
        proc = self.start_ffmpeg(rtmp_port, icecast_port)
        self.assertTrue(self.mock.wait_for(accepted=1, byte_count=1024, connection_index=0, connection_byte_count=512, timeout=7), "initial Icecast source did not connect")
        before_drop = self.mock.bytes_received

        self.mock.disconnect_clients()
        self.assertTrue(self.mock.wait_for(accepted=2, byte_count=before_drop + 512, connection_index=1, connection_byte_count=512, timeout=7), "Icecast did not reconnect after a midstream drop")
        self.assertIn(b"content-type: audio/aac", self.mock.request_headers[1].lower())
        self.assertIsNone(proc.poll(), "FFmpeg exited after an Icecast disconnect")


if __name__ == "__main__":
    unittest.main()
