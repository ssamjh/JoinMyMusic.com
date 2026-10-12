"""Audio control proxy: the listen and microphone sockets, and the meters stream."""
import os
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402
from fastapi import WebSocketDisconnect  # noqa: E402

import support  # noqa: E402
from support import ADMIN, AUTH_KEY, ApiTestCase, main  # noqa: E402

MIC_PATH = "/admin/audio/mic"
LISTEN_PATH = "/admin/audio/listen"
SAME_ORIGIN = {"Origin": "http://testserver", "X-Auth-Key": AUTH_KEY}
FRAME_BYTES = 3528  # one 20 ms listen message


def fake_audio_client(**methods):
    """An httpx.AsyncClient stand-in for the audio upstream."""
    client = mock.MagicMock()
    for name, value in methods.items():
        setattr(client, name, value)
    return client


def streamed_listen(chunks, status_code=200):
    """An upstream listen response whose body yields the given chunks (exceptions are raised)."""
    async def body():
        for chunk in chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    upstream = mock.MagicMock(status_code=status_code)
    upstream.aiter_raw = lambda: body()
    upstream.aclose = mock.AsyncMock()
    return upstream


def refused(test, path, headers):
    with test.assertRaises(WebSocketDisconnect) as cm:
        with test.client.websocket_connect(path, headers=headers):
            pass
    test.assertEqual(cm.exception.code, 1008)


class ControlRouteTests(ApiTestCase):
    def test_unlisted_control_path_is_refused(self):
        resp = self.client.get("/admin/audio/api/mic", headers=ADMIN)
        self.assertEqual(resp.status_code, 404)

    def test_listen_is_not_proxied_over_http(self):
        self.assertEqual(self.client.get("/admin/audio/api/listen", headers=ADMIN).status_code, 404)


class ByteBacklogTests(ApiTestCase):
    def test_drops_oldest_past_the_limit(self):
        async def run():
            backlog = main.ByteBacklog(max_bytes=4)
            backlog.put(b"aa")
            backlog.put(b"bb")
            backlog.put(b"cc")  # 6 bytes queued: "aa" goes
            backlog.close()
            return [chunk async for chunk in backlog.body()]

        self.assertEqual(self.run_async(run()), [b"bb", b"cc"])

    def test_keeps_an_oversized_newest_chunk(self):
        async def run():
            backlog = main.ByteBacklog(max_bytes=2)
            backlog.put(b"aaaa")
            backlog.close()
            return [chunk async for chunk in backlog.body()]

        self.assertEqual(self.run_async(run()), [b"aaaa"])


class MicSocketAuthTests(ApiTestCase):
    def test_rejects_missing_key(self):
        refused(self, MIC_PATH, {"Origin": "http://testserver"})

    def test_rejects_wrong_key(self):
        refused(self, MIC_PATH, {"Origin": "http://testserver", "X-Auth-Key": "wrong"})

    def test_rejects_other_origin_even_with_key(self):
        refused(self, MIC_PATH, {"Origin": "https://evil.example", "X-Auth-Key": AUTH_KEY})

    def test_rejects_missing_origin(self):
        refused(self, MIC_PATH, {"X-Auth-Key": AUTH_KEY})

    def test_admin_cookie_is_accepted(self):
        self.client.cookies.set("admin_key", AUTH_KEY)
        upstream = fake_audio_client(post=mock.AsyncMock(return_value=SimpleNamespace(status_code=404)))
        self.patch(main, "http_client", mock.MagicMock(return_value=upstream))
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.client.websocket_connect(MIC_PATH, headers={"Origin": "http://testserver"}) as ws:
                ws.receive_bytes()
        # Accepted (the handshake passed), then closed because the mic is disabled.
        self.assertEqual(cm.exception.reason, "mic input is disabled")


class MicSocketRelayTests(ApiTestCase):
    def test_mic_disabled_closes_with_reason(self):
        upstream = fake_audio_client(post=mock.AsyncMock(return_value=SimpleNamespace(status_code=404)))
        self.patch(main, "http_client", mock.MagicMock(return_value=upstream))
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.client.websocket_connect(MIC_PATH, headers=SAME_ORIGIN) as ws:
                ws.receive_bytes()
        self.assertEqual(cm.exception.code, 1011)
        self.assertEqual(cm.exception.reason, "mic input is disabled")

    def test_unreachable_audio_closes_with_reason(self):
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.client.websocket_connect(MIC_PATH, headers=SAME_ORIGIN) as ws:
                ws.receive_bytes()
        self.assertEqual(cm.exception.code, 1011)
        self.assertEqual(cm.exception.reason, "audio control is unreachable")

    def test_frames_are_relayed_as_one_chunked_body(self):
        received = []

        async def post(url, content, headers):
            async for chunk in content:
                received.append(chunk)
            return SimpleNamespace(status_code=200)

        upstream = fake_audio_client(post=mock.AsyncMock(side_effect=post))
        self.patch(main, "http_client", mock.MagicMock(return_value=upstream))
        with self.client.websocket_connect(MIC_PATH, headers=SAME_ORIGIN) as ws:
            ws.send_bytes(b"\x01\x00")
            ws.send_bytes(b"\x02\x00")
        self.assertEqual(received, [b"\x01\x00", b"\x02\x00"])
        self.assertTrue(upstream.post.call_args.args[0].endswith("/api/mic"))


class ListenSocketAuthTests(ApiTestCase):
    def test_rejects_missing_key(self):
        refused(self, LISTEN_PATH, {"Origin": "http://testserver"})

    def test_rejects_wrong_key(self):
        refused(self, LISTEN_PATH, {"Origin": "http://testserver", "X-Auth-Key": "wrong"})

    def test_rejects_other_origin_even_with_key(self):
        refused(self, LISTEN_PATH, {"Origin": "https://evil.example", "X-Auth-Key": AUTH_KEY})

    def test_rejects_missing_origin(self):
        refused(self, LISTEN_PATH, {"X-Auth-Key": AUTH_KEY})

    def test_rejected_handshake_never_reaches_audio(self):
        upstream = fake_audio_client(send=mock.AsyncMock())
        self.patch(main, "http_client", mock.MagicMock(return_value=upstream))
        refused(self, LISTEN_PATH, {"Origin": "https://evil.example", "X-Auth-Key": AUTH_KEY})
        upstream.send.assert_not_called()


class ListenSocketRelayTests(ApiTestCase):
    def test_unreachable_audio_closes_with_reason(self):
        upstream = fake_audio_client(send=mock.AsyncMock(side_effect=httpx.ConnectError("refused")))
        self.patch(main, "http_client", mock.MagicMock(return_value=upstream))
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.client.websocket_connect(LISTEN_PATH, headers=SAME_ORIGIN) as ws:
                ws.receive_bytes()
        self.assertEqual(cm.exception.code, 1011)
        self.assertEqual(cm.exception.reason, "audio control is unreachable")

    def test_error_status_closes_with_reason(self):
        upstream = fake_audio_client(send=mock.AsyncMock(return_value=streamed_listen([], status_code=503)))
        self.patch(main, "http_client", mock.MagicMock(return_value=upstream))
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.client.websocket_connect(LISTEN_PATH, headers=SAME_ORIGIN) as ws:
                ws.receive_bytes()
        self.assertEqual(cm.exception.code, 1011)
        self.assertEqual(cm.exception.reason, "audio control returned 503")

    def test_upstream_failure_closes_with_reason(self):
        upstream = streamed_listen([b"\x01" * 8, httpx.ReadError("lost")])
        self.patch(main, "http_client", mock.MagicMock(return_value=fake_audio_client(
            send=mock.AsyncMock(return_value=upstream))))
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.client.websocket_connect(LISTEN_PATH, headers=SAME_ORIGIN) as ws:
                ws.receive_bytes()
        self.assertEqual(cm.exception.code, 1011)
        self.assertEqual(cm.exception.reason, "audio control closed the listen feed")
        upstream.aclose.assert_awaited()

    def test_messages_are_whole_frames_of_20_ms(self):
        # 10584 bytes in, uneven upstream chunks: three full messages, nothing left over.
        chunks = [b"\x01" * 5, b"\x02" * 3523, b"\x03" * 10, b"\x04" * 7046]
        upstream = streamed_listen(chunks)
        client = fake_audio_client(send=mock.AsyncMock(return_value=upstream))
        self.patch(main, "http_client", mock.MagicMock(return_value=client))

        messages = []
        with self.assertRaises(WebSocketDisconnect) as cm:
            with self.client.websocket_connect(LISTEN_PATH, headers=SAME_ORIGIN) as ws:
                while True:
                    messages.append(ws.receive_bytes())

        self.assertEqual(cm.exception.code, 1000)
        self.assertEqual([len(m) for m in messages], [FRAME_BYTES] * 3)
        self.assertEqual(b"".join(messages), b"".join(chunks))
        method, url = client.build_request.call_args.args[:2]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/api/listen"))
        self.assertIsNone(client.build_request.call_args.kwargs["timeout"].read)
        upstream.aclose.assert_awaited()

    def test_partial_tail_is_not_sent(self):
        # A trailing partial message never reaches the socket; the feed ends cleanly.
        upstream = streamed_listen([b"\x07" * (FRAME_BYTES + 4)])
        self.patch(main, "http_client", mock.MagicMock(return_value=fake_audio_client(
            send=mock.AsyncMock(return_value=upstream))))
        messages = []
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(LISTEN_PATH, headers=SAME_ORIGIN) as ws:
                while True:
                    messages.append(ws.receive_bytes())
        self.assertEqual(messages, [b"\x07" * FRAME_BYTES])
