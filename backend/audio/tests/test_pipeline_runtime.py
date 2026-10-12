import array
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import math
import struct
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run.py"
RADIO_SCRIPT = ROOT / "config" / "processing.liq"


FAKE_FFMPEG = (
    "#!/usr/bin/env python3\n"
    "import array, os, sys\n"
    "if '[proc]' in sys.argv:\n"
    "    # Processor stand-in: s16le in, f32le out, one frame per frame, value/32768.\n"
    "    carry = b''\n"
    "    while True:\n"
    "        data = os.read(0, 4096)\n"
    "        if not data:\n"
    "            break\n"
    "        buf = carry + data\n"
    "        usable = len(buf) - len(buf) % 4\n"
    "        carry = buf[usable:]\n"
    "        samples = array.array('h')\n"
    "        samples.frombytes(buf[:usable])\n"
    "        out = array.array('f', [value / 32768.0 for value in samples]).tobytes()\n"
    "        view = memoryview(out)\n"
    "        while view:\n"
    "            view = view[os.write(1, view):]\n"
    "else:\n"
    "    # Encoder stand-in: record its f32le input.\n"
    "    with open(os.environ['CAPTURE'], 'wb') as out:\n"
    "        while True:\n"
    "            block = sys.stdin.buffer.read(8192)\n"
    "            if not block:\n"
    "                break\n"
    "            out.write(block)\n"
    "            out.flush()\n"
)


def decode_floats(data: bytes) -> list[float]:
    values = array.array("f")
    values.frombytes(data[: len(data) // 4 * 4])
    return list(values)


def make_fake_liquidsoap(directory: Path) -> Path:
    """Pass the paced FIFO through unchanged; DSP is exercised separately with Liquidsoap."""
    executable = directory / "fake-liquidsoap"
    executable.write_text(
        "#!/bin/sh\nexec cat \"$RADIO_INPUT_FIFO\"\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


class PipelineRuntimeTest(unittest.TestCase):
    def test_paces_audio_keeps_silence_and_reopens_fifo_on_shutdown(self):
        with tempfile.TemporaryDirectory(prefix="jmm-pipeline-") as temp:
            work = Path(temp)
            fifo = work / "spotify.pcm"
            capture = work / "captured.pcm"
            config_source = work / "spotify.yml"
            config_source.write_text("test: config\n", encoding="utf-8")
            audio_config = work / "audio.yml"
            audio_config.write_text("vban:\n  enabled: false\n", encoding="utf-8")
            ffmpeg = work / "fake-ffmpeg"
            ffmpeg.write_text(FAKE_FFMPEG, encoding="utf-8")
            librespot = work / "fake-librespot"
            librespot.write_text(
                "#!/usr/bin/env python3\n"
                "import os, time\n"
                "fifo = os.environ['AUDIO_FIFO']\n"
                "def play(byte):\n"
                "    with open(fifo, 'wb', buffering=0) as out:\n"
                "        for _ in range(8):\n"
                "            out.write(bytes([byte]) * 3528); time.sleep(0.02)\n"
                "play(65)\n"
                "time.sleep(0.7)\n"
                "play(66)\n"
                "while True: time.sleep(1)\n",
                encoding="utf-8",
            )
            ffmpeg.chmod(0o755)
            librespot.chmod(0o755)
            liquidsoap = make_fake_liquidsoap(work)
            vban_marker = work / "vban-started"
            vban = work / "fake-vban-emitter"
            vban.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "open(os.environ['VBAN_MARKER'], 'w').close()\n",
                encoding="utf-8",
            )
            vban.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "AUDIO_FIFO": str(fifo),
                    "AUDIO_CONFIG_PATH": str(audio_config),
                    "CAPTURE": str(capture),
                    "FFMPEG_BIN": str(ffmpeg),
                    "LIQUIDSOAP_BIN": str(liquidsoap),
                    "RADIO_LIQ_PATH": str(RADIO_SCRIPT),
                    "RADIO_INPUT_FIFO": str(work / "liquidsoap-input.pcm"),
                    "LIBRESPOT_BIN": str(librespot),
                    "LIBRESPOT_CONFIG_DIR": str(work / "state"),
                    "VBAN_EMITTER_BIN": str(vban),
                    "VBAN_MARKER": str(vban_marker),
                    "SPOTIFY_CONFIG_SOURCE": str(config_source),
                    "RTMP_URL": "rtmp://localhost/live/test",
                    "ICECAST_URL": "icecast://source:password@localhost:8000/test.aac",
                }
            )
            proc = subprocess.Popen([sys.executable, str(RUNNER)], env=env)
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if capture.exists() and 0x4242 / 32768.0 in decode_floats(capture.read_bytes()):
                        break
                    if proc.poll() is not None:
                        self.fail(f"runner exited before reconnect: {proc.returncode}")
                    time.sleep(0.05)
                else:
                    self.fail("second FIFO writer did not reach FFmpeg")
            finally:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=8)

            samples = decode_floats(capture.read_bytes())[0::2]  # left channel, one value per frame
            a_value, b_value = 0x4141 / 32768.0, 0x4242 / 32768.0  # the bytes 'A' and 'B' as s16le
            first = samples.index(a_value) if a_value in samples else -1
            second = samples.index(b_value) if b_value in samples else -1
            self.assertGreaterEqual(first, 0, "first playback block was not forwarded")
            self.assertGreater(second, first, "reopened FIFO playback was not forwarded")
            gap = samples[first + 882 : second]
            self.assertGreater(gap.count(0.0), 20_000, "pause did not produce timed silence")
            self.assertGreater(len(samples), 22_050, "FIFO input was not paced at the PCM sample rate")
            self.assertFalse(vban_marker.exists(), "disabled VBAN unexpectedly started an emitter")
            self.assertTrue((work / "state" / "config.yml").is_file())

    def test_vban_receiver_config_and_arguments(self):
        sys.path.insert(0, str(RUNNER.parent))
        import run as runtime

        def load(text: str):
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "audio.yml"
                path.write_text(text, encoding="utf-8")
                with patch.dict(os.environ, {"AUDIO_CONFIG_PATH": str(path)}):
                    return runtime.load_vban_receiver_config()

        self.assertEqual(load("vban:\n  enabled: false\n"), {"enabled": False})
        config = load(
            "vban_receiver:\n  enabled: true\n  sender_ip: 10.0.0.5\n  port: 6981\n"
            "  stream_name: Mic\n  sample_rate: 44100\n  channels: '1, 1'\n"
        )
        self.assertEqual(config["channels"], "1,1")
        self.assertEqual(config["quality"], "medium")
        self.assertEqual(
            load("vban_receiver:\n  enabled: true\n  sender_ip: 10.0.0.5\n  quality: very_slow\n")["quality"],
            "very_slow",
        )
        with patch.dict(os.environ, {"VBAN_RECEPTOR_BIN": "rx", "VBAN_RECEIVER_FIFO": "/tmp/in.pcm"}):
            self.assertEqual(
                runtime.vban_receiver_args(config),
                ["rx", "-i", "10.0.0.5", "-p", "6981", "-s", "Mic", "-b", "file", "-d", "/tmp/in.pcm",
                 "-c", "1,1", "-q", "0"],
            )
        for bad in (
            "  sender_ip: nope\n",
            "  sender_ip: 10.0.0.5\n  sample_rate: 48000\n",
            "  sender_ip: 10.0.0.5\n  channels: '1'\n",
            "  sender_ip: 10.0.0.5\n  stream_name: ThisNameIsTooLong\n",
            "  sender_ip: 10.0.0.5\n  port: 0\n",
            "  sender_ip: 10.0.0.5\n  quality: best\n",
        ):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                load("vban_receiver:\n  enabled: true\n" + bad)

    def test_mic_and_live_input_config(self):
        sys.path.insert(0, str(RUNNER.parent))
        import run as runtime

        def load(function, text: str):
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "audio.yml"
                path.write_text(text, encoding="utf-8")
                with patch.dict(os.environ, {"AUDIO_CONFIG_PATH": str(path)}):
                    return function()

        self.assertEqual(load(runtime.load_mic_config, ""), {"enabled": True, "trim_db": -6.0})
        self.assertEqual(
            load(runtime.load_mic_config, "mic:\n  enabled: false\n  trim_db: 3\n"),
            {"enabled": False, "trim_db": 3.0},
        )
        self.assertEqual(load(runtime.load_live_input_config, ""), {"duck_db": -10.0})
        self.assertEqual(load(runtime.load_live_input_config, "live_input:\n  duck_db: 0\n"), {"duck_db": 0.0})
        for text in ("mic: nope\n", "mic:\n  enabled: 1\n", "mic:\n  trim_db: 13\n",
                     "mic:\n  trim_db: -41\n", "mic:\n  trim_db: true\n"):
            with self.subTest(text=text), self.assertRaises(SystemExit):
                load(runtime.load_mic_config, text)
        for text in ("live_input: [1]\n", "live_input:\n  duck_db: 1\n"):
            with self.subTest(text=text), self.assertRaises(SystemExit):
                load(runtime.load_live_input_config, text)
        # ducking moved out of vban_receiver: an old key is refused rather than ignored
        with self.assertRaises(SystemExit):
            load(
                runtime.load_vban_receiver_config,
                "vban_receiver:\n  enabled: true\n  sender_ip: 10.0.0.5\n  duck_db: -10\n",
            )

    def test_unpaced_writer_is_throttled_without_losing_audio(self):
        """go-librespot's pipe output writes as fast as it can; no audio may be dropped."""
        with tempfile.TemporaryDirectory(prefix="jmm-pipeline-") as temp:
            work = Path(temp)
            capture = work / "captured.pcm"
            config_source = work / "spotify.yml"
            config_source.write_text("test: config\n", encoding="utf-8")
            audio_config = work / "audio.yml"
            audio_config.write_text("vban:\n  enabled: false\n", encoding="utf-8")
            total = 176_400  # one second of 44.1 kHz stereo s16le
            ffmpeg = work / "fake-ffmpeg"
            ffmpeg.write_text(FAKE_FFMPEG, encoding="utf-8")
            librespot = work / "fake-librespot"
            librespot.write_text(
                "#!/usr/bin/env python3\n"
                "import os, time\n"
                "with open(os.environ['AUDIO_FIFO'], 'wb', buffering=0) as out:\n"
                f"    out.write(b'C' * {total})\n"
                "open(os.environ['DONE_MARKER'], 'w').close()\n"
                "while True: time.sleep(1)\n",
                encoding="utf-8",
            )
            ffmpeg.chmod(0o755)
            librespot.chmod(0o755)
            done = work / "writer-done"
            env = os.environ.copy()
            env.update(
                {
                    "AUDIO_FIFO": str(work / "spotify.pcm"),
                    "AUDIO_CONFIG_PATH": str(audio_config),
                    "CAPTURE": str(capture),
                    "DONE_MARKER": str(done),
                    "FFMPEG_BIN": str(ffmpeg),
                    "LIQUIDSOAP_BIN": str(make_fake_liquidsoap(work)),
                    "RADIO_LIQ_PATH": str(RADIO_SCRIPT),
                    "RADIO_INPUT_FIFO": str(work / "liquidsoap-input.pcm"),
                    "LIBRESPOT_BIN": str(librespot),
                    "LIBRESPOT_CONFIG_DIR": str(work / "state"),
                    "SPOTIFY_CONFIG_SOURCE": str(config_source),
                    "RTMP_URL": "rtmp://localhost/live/test",
                    "ICECAST_URL": "icecast://source:password@localhost:8000/test.aac",
                }
            )
            proc = subprocess.Popen([sys.executable, str(RUNNER)], env=env)
            try:
                started = time.monotonic()
                deadline = started + 8
                while time.monotonic() < deadline and not done.exists():
                    if proc.poll() is not None:
                        self.fail(f"runner exited early: {proc.returncode}")
                    time.sleep(0.01)
                writer_seconds = time.monotonic() - started
                time.sleep(0.6)  # let the queued tail drain at real time
            finally:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=8)

            self.assertTrue(done.exists(), "unpaced writer never finished")
            self.assertGreater(writer_seconds, 0.6, "writer was not throttled to real time")
            samples = decode_floats(capture.read_bytes())
            self.assertEqual(samples.count(0x4343 / 32768.0), total // 2, "audio was dropped or duplicated")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_real_ffmpeg_filter_graph_encodes_both_stream_formats(self):
        with tempfile.TemporaryDirectory(prefix="jmm-ffmpeg-") as temp:
            work = Path(temp)
            env = os.environ.copy()
            env.update(
                {
                    "FFMPEG_BIN": shutil.which("ffmpeg") or "ffmpeg",
                    "RTMP_URL": str(work / "test.flv"),
                    "ICECAST_URL": str(work / "test.aac"),
                }
            )
            # Exercise the production filter graph and both muxers/codecs with
            # short PCM from stdin; local files keep this test network-free.
            sys.path.insert(0, str(RUNNER.parent))
            try:
                import run as runtime

                with patch.dict(os.environ, env, clear=False):
                    monitor_read, monitor_write = os.pipe()
                    args = runtime.encoder_args(monitor_fd=monitor_write)
            finally:
                sys.path.remove(str(RUNNER.parent))
                sys.modules.pop("run", None)
            index = 0
            while index < len(args):
                if args[index] in {"-rw_timeout", "-content_type"}:
                    del args[index : index + 2]
                else:
                    index += 1
            args[0] = env["FFMPEG_BIN"]

            samples = bytearray()
            for sample_index in range(8_820):
                value = 5000 * math.sin(2 * math.pi * 440 * sample_index / 44_100) / 32768.0
                samples.extend(struct.pack("<ff", value, value))
            result = subprocess.run(
                args, input=samples, capture_output=True, timeout=15, env=env, pass_fds=(monitor_write,)
            )
            os.close(monitor_write)
            monitor_pcm = bytearray()
            while chunk := os.read(monitor_read, 65536):
                monitor_pcm.extend(chunk)
            os.close(monitor_read)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertGreater(len(monitor_pcm), 0, "monitor pipe carried no audio")
            self.assertGreater((work / "test.flv").stat().st_size, 0)
            self.assertGreater(
                (work / "test.aac").stat().st_size,
                0,
                result.stderr.decode(errors="replace"),
            )

    def test_startup_failure_closes_ffmpeg_and_returns_nonzero(self):
        with tempfile.TemporaryDirectory(prefix="jmm-fail-") as temp:
            work = Path(temp)
            config = work / "spotify.yml"
            config.write_text("test: config\n", encoding="utf-8")
            audio_config = work / "audio.yml"
            audio_config.write_text("vban:\n  enabled: false\n", encoding="utf-8")
            ffmpeg = work / "fake-ffmpeg"
            ffmpeg.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "while sys.stdin.buffer.read(8192): pass\n",
                encoding="utf-8",
            )
            ffmpeg.chmod(0o755)
            liquidsoap = make_fake_liquidsoap(work)
            env = os.environ.copy()
            env.update(
                {
                    "AUDIO_FIFO": str(work / "spotify.pcm"),
                    "AUDIO_CONFIG_PATH": str(audio_config),
                    "FFMPEG_BIN": str(ffmpeg),
                    "LIQUIDSOAP_BIN": str(liquidsoap),
                    "RADIO_LIQ_PATH": str(RADIO_SCRIPT),
                    "RADIO_INPUT_FIFO": str(work / "liquidsoap-input.pcm"),
                    "LIBRESPOT_BIN": str(work / "missing-librespot"),
                    "LIBRESPOT_CONFIG_DIR": str(work / "state"),
                    "SPOTIFY_CONFIG_SOURCE": str(config),
                    "RTMP_URL": "rtmp://localhost/live/test",
                    "ICECAST_URL": "icecast://source:password@localhost:8000/test.aac",
                }
            )
            result = subprocess.run([sys.executable, str(RUNNER)], env=env, capture_output=True, timeout=8)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"missing-librespot", result.stderr)

    @unittest.skipUnless(os.name == "posix", "process groups are POSIX-only")
    def test_shutdown_kills_stubborn_child_within_global_deadline(self):
        sys.path.insert(0, str(RUNNER.parent))
        try:
            import run as runtime

            stubborn = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
                ],
                start_new_session=True,
            )
            try:
                time.sleep(0.1)
                started = time.monotonic()
                runtime.stop_processes([stubborn], process_groups=(stubborn,))
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, runtime.SHUTDOWN_TIMEOUT + 0.75)
                self.assertIsNotNone(stubborn.poll(), "stubborn child was not reaped")
                with self.assertRaises(ProcessLookupError):
                    os.kill(stubborn.pid, 0)
            finally:
                if stubborn.poll() is None:
                    stubborn.kill()
                    stubborn.wait()
        finally:
            sys.path.remove(str(RUNNER.parent))
            sys.modules.pop("run", None)

    def test_enabled_vban_rejects_invalid_destination(self):
        with tempfile.TemporaryDirectory(prefix="jmm-vban-config-") as temp:
            config_path = Path(temp) / "audio.yml"
            config_path.write_text(
                "vban:\n  enabled: true\n  ip_address: receiver.local\n  port: 6980\n  stream_name: JMM Audio\n",
                encoding="utf-8",
            )
            sys.path.insert(0, str(RUNNER.parent))
            try:
                import run as runtime

                with patch.dict(os.environ, {"AUDIO_CONFIG_PATH": str(config_path)}, clear=False):
                    with self.assertRaisesRegex(SystemExit, "IPv4 address"):
                        runtime.load_vban_config()
            finally:
                sys.path.remove(str(RUNNER.parent))
                sys.modules.pop("run", None)

    @unittest.skipUnless(
        os.environ.get("VBAN_EMITTER_BIN")
        or Path("/usr/bin/vban_emitter").is_file(),
        "vban_emitter is not installed",
    )
    def test_vban_enabled_sends_processed_pcm_packets_to_udp(self):
        emitter_bin = os.environ.get("VBAN_EMITTER_BIN", "/usr/bin/vban_emitter")
        with tempfile.TemporaryDirectory(prefix="jmm-vban-") as temp:
            work = Path(temp)
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.bind(("127.0.0.1", 0))
            udp.settimeout(0.2)
            port = udp.getsockname()[1]
            config_path = work / "audio.yml"
            config_path.write_text(
                f"vban:\n  enabled: true\n  ip_address: 127.0.0.1\n  port: {port}\n  stream_name: JMM Audio\n",
                encoding="utf-8",
            )
            vban_fifo = work / "vban.pcm"
            os.mkfifo(vban_fifo)
            env = os.environ.copy()
            env.update(
                {
                    "AUDIO_CONFIG_PATH": str(config_path),
                    "FFMPEG_BIN": shutil.which("ffmpeg") or "ffmpeg",
                    "VBAN_EMITTER_BIN": emitter_bin,
                    "VBAN_FIFO": str(vban_fifo),
                    "RTMP_URL": str(work / "test.flv"),
                    "ICECAST_URL": str(work / "test.aac"),
                }
            )
            sys.path.insert(0, str(RUNNER.parent))
            try:
                import run as runtime

                with patch.dict(os.environ, env, clear=False):
                    vban_config = runtime.load_vban_config()
                    emitter_args = runtime.vban_args(vban_config)
                    args = runtime.encoder_args(vban_enabled=True)
            finally:
                sys.path.remove(str(RUNNER.parent))
                sys.modules.pop("run", None)

            for index in range(len(args) - 1, -1, -1):
                if args[index] in {"-rw_timeout", "-content_type"}:
                    del args[index : index + 2]
            args[0] = env["FFMPEG_BIN"]
            emitter = subprocess.Popen(
                emitter_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            ffmpeg = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            samples = bytearray()
            for sample_index in range(22_050):
                value = 8000 * math.sin(2 * math.pi * 440 * sample_index / 44_100) / 32768.0
                samples.extend(struct.pack("<ff", value, value))
            try:
                _stdout, stderr = ffmpeg.communicate(input=samples, timeout=15)
                packets = []
                deadline = time.monotonic() + 3
                # The processing graph buffers about 100 ms, so the first packets may be silent.
                while len(packets) < 40 and time.monotonic() < deadline:
                    udp.settimeout(min(0.2, max(0.01, deadline - time.monotonic())))
                    try:
                        packet, _address = udp.recvfrom(2048)
                        packets.append(packet)
                    except TimeoutError:
                        pass
            finally:
                if ffmpeg.poll() is None:
                    ffmpeg.kill()
                    ffmpeg.wait()
                if emitter.poll() is None:
                    emitter.terminate()
                _emitter_output, emitter_stderr = emitter.communicate(timeout=5)
                udp.close()

            self.assertEqual(
                ffmpeg.returncode,
                0,
                stderr.decode(errors="replace") + "\nVBAN: " + emitter_stderr.decode(errors="replace"),
            )
            self.assertGreaterEqual(len(packets), 2, "VBAN emitter did not send UDP packets")
            packet = packets[0]
            self.assertEqual(packet[:4], b"VBAN")
            self.assertEqual(packet[4], 16, "VBAN sample-rate index should represent 44.1 kHz")
            self.assertLessEqual(packet[5] + 1, 256, "VBAN must cap a packet at 256 sample frames")
            self.assertEqual((packet[5] + 1) * 4, len(packet) - 28)
            self.assertEqual(packet[6], 1, "VBAN channel count should be stereo")
            self.assertEqual(packet[7] & 0x07, 1, "VBAN sample format should be signed 16-bit PCM")
            self.assertEqual(packet[8:24].rstrip(b"\x00"), b"JMM Audio")
            first_frame = struct.unpack("<I", packet[24:28])[0]
            next_frame = struct.unpack("<I", packets[1][24:28])[0]
            self.assertEqual(next_frame, first_frame + 1)
            payload = packet[28:]
            self.assertTrue(payload, "VBAN packet contains no PCM payload")
            self.assertEqual(len(payload) % 4, 0, "VBAN payload must contain whole stereo frames")
            self.assertTrue(any(any(item[28:]) for item in packets), "the processed test tone was silent")

    def test_vban_emitter_failure_stops_other_processes(self):
        with tempfile.TemporaryDirectory(prefix="jmm-vban-fail-") as temp:
            work = Path(temp)
            source_config = work / "spotify.yml"
            source_config.write_text("test: config\n", encoding="utf-8")
            audio_config = work / "audio.yml"
            audio_config.write_text(
                "vban:\n  enabled: true\n  ip_address: 127.0.0.1\n  port: 6980\n  stream_name: JMM Audio\n",
                encoding="utf-8",
            )
            librespot_pid = work / "librespot.pid"
            librespot = work / "fake-librespot"
            librespot.write_text(
                "#!/usr/bin/env python3\n"
                "import os, time\n"
                "open(os.environ['PID_FILE'], 'w').write(str(os.getpid()))\n"
                "while True: time.sleep(1)\n",
                encoding="utf-8",
            )
            ffmpeg = work / "fake-ffmpeg"
            ffmpeg.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "while sys.stdin.buffer.read(8192): pass\n",
                encoding="utf-8",
            )
            emitter = work / "failed-vban-emitter"
            emitter.write_text(
                "#!/usr/bin/env python3\nimport time\ntime.sleep(0.2)\nraise SystemExit(2)\n",
                encoding="utf-8",
            )
            liquidsoap = make_fake_liquidsoap(work)
            for executable in (librespot, ffmpeg, emitter):
                executable.chmod(0o755)

            fifo = work / "spotify.pcm"
            vban_fifo = work / "vban.pcm"
            env = os.environ.copy()
            env.update(
                {
                    "AUDIO_FIFO": str(fifo),
                    "AUDIO_CONFIG_PATH": str(audio_config),
                    "FFMPEG_BIN": str(ffmpeg),
                    "LIQUIDSOAP_BIN": str(liquidsoap),
                    "RADIO_LIQ_PATH": str(RADIO_SCRIPT),
                    "RADIO_INPUT_FIFO": str(work / "liquidsoap-input.pcm"),
                    "LIBRESPOT_BIN": str(librespot),
                    "LIBRESPOT_CONFIG_DIR": str(work / "state"),
                    "PID_FILE": str(librespot_pid),
                    "VBAN_EMITTER_BIN": str(emitter),
                    "VBAN_FIFO": str(vban_fifo),
                    "SPOTIFY_CONFIG_SOURCE": str(source_config),
                    "RTMP_URL": "rtmp://localhost/live/test",
                    "ICECAST_URL": "icecast://source:password@localhost:8000/test.aac",
                }
            )
            result = subprocess.run(
                [sys.executable, str(RUNNER)], env=env, capture_output=True, timeout=8
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"vban_emitter exited with status 2", result.stderr)
            self.assertTrue(librespot_pid.is_file(), "go-librespot test child did not start")
            with self.assertRaises(ProcessLookupError):
                os.kill(int(librespot_pid.read_text(encoding="utf-8")), 0)


if __name__ == "__main__":
    unittest.main()
