"""/cover/{image_id}: proxied album art with constant CORS and immutable caching."""
import os
import sys
from types import SimpleNamespace

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402
from support import ApiTestCase, main  # noqa: E402

IMMUTABLE = "public, max-age=31536000, immutable"


class FakeUpstream:
    """Replaces httpx.AsyncClient for the cover route; ``result`` is returned or raised."""

    result = None
    urls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aclose(self):
        pass

    async def get(self, url, **kwargs):
        FakeUpstream.urls.append(url)
        if isinstance(FakeUpstream.result, Exception):
            raise FakeUpstream.result
        return FakeUpstream.result


class CoverTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        FakeUpstream.urls = []
        FakeUpstream.result = httpx.Response(
            200, content=b"image-bytes", headers={"content-type": "image/png"}
        )
        # Only main's view of httpx is replaced; the rest of the process keeps the real module.
        fake_httpx = SimpleNamespace(
            AsyncClient=FakeUpstream, HTTPError=httpx.HTTPError, Timeout=httpx.Timeout
        )
        self.patch(main, "httpx", fake_httpx)
        # Fresh shared-client registry so the fake is created for this test only.
        self.patch(main, "_http_clients", {})

    def test_proxies_image_with_constant_cors_and_immutable_cache(self):
        resp = self.client.get("/cover/ab12CD34")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"image-bytes")
        self.assertEqual(resp.headers["content-type"], "image/png")
        self.assertEqual(resp.headers["access-control-allow-origin"], "*")
        self.assertEqual(resp.headers["cache-control"], IMMUTABLE)
        self.assertEqual(FakeUpstream.urls, ["https://i.scdn.co/image/ab12CD34"])

    def test_cors_is_not_reflected_for_allowed_origins(self):
        resp = self.client.get(
            "/cover/ab12CD34", headers={"Origin": "https://joinmymusic.com"}
        )
        self.assertEqual(resp.headers["access-control-allow-origin"], "*")
        self.assertNotIn("vary", {k.lower() for k in resp.headers})

    def test_invalid_ids_are_not_fetched(self):
        for image_id in ("bad-id!", "a" * 65, "abc.def"):
            with self.subTest(image_id=image_id):
                self.assertEqual(self.client.get(f"/cover/{image_id}").status_code, 404)
        self.assertEqual(FakeUpstream.urls, [])

    def test_upstream_miss_is_404(self):
        FakeUpstream.result = httpx.Response(404, content=b"")
        self.assertEqual(self.client.get("/cover/ab12CD34").status_code, 404)

    def test_upstream_error_is_502(self):
        FakeUpstream.result = httpx.ConnectError("unreachable")
        self.assertEqual(self.client.get("/cover/ab12CD34").status_code, 502)


if __name__ == "__main__":
    support.unittest.main()
