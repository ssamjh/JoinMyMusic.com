"""Metadata timing, the delayed poll commit, the broadcaster and SSE initial state."""
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: E402
from support import (  # noqa: E402
    ApiTestCase,
    StateTestCase,
    add_listener,
    main,
    make_request,
    parse_sse,
    sse,
    storage,
    track,
)

POLLED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class TimingTests(StateTestCase):
    def setUp(self):
        super().setUp()
        self.patch(sse, "METADATA_DELAY", 5.0)
        self.patch(sse, "METADATA_DELAY_MS", 5000.0)

    def test_stream_started_at_subtracts_the_heard_position(self):
        # 20 s into the song live is 15 s heard; position 0 was heard 15 s ago.
        self.assertEqual(
            sse.stream_started_at(POLLED_AT, 20000), POLLED_AT - timedelta(seconds=15)
        )
        # Inside the delay buffer the anchor lies in the future.
        self.assertEqual(
            sse.stream_started_at(POLLED_AT, 1000), POLLED_AT + timedelta(seconds=4)
        )

    def test_enrich_with_timing_for_the_anchored_song(self):
        started = datetime.now(timezone.utc) - timedelta(seconds=10)
        sse.current_song_state.update(songid="s1", started_at=started.isoformat())
        out = sse.enrich_with_timing(track("s1", progress_ms=12345))
        self.assertNotIn("progress_ms", out)
        self.assertEqual(out["started_at"], started.isoformat())
        self.assertAlmostEqual(out["elapsed_ms"], 10000, delta=1000)
        self.assertEqual(out["deck_side"], "left")
        self.assertEqual(out["songid"], "s1")

    def test_enrich_with_timing_keeps_an_explicit_deck_side(self):
        out = sse.enrich_with_timing(track("s1", deck_side="right"))
        self.assertEqual(out["deck_side"], "right")

    def test_enrich_with_timing_for_another_song(self):
        sse.current_song_state.update(songid="s1", started_at=POLLED_AT.isoformat())
        out = sse.enrich_with_timing(track("s2"))
        self.assertIsNone(out["started_at"])
        self.assertIsNone(out["elapsed_ms"])

    def test_live_metadata_uses_the_live_position(self):
        out = sse.live_metadata_for_clients(track("s1", progress_ms=30000), POLLED_AT)
        self.assertNotIn("progress_ms", out)
        self.assertEqual(out["elapsed_ms"], 30000)
        self.assertEqual(out["started_at"], (POLLED_AT - timedelta(seconds=30)).isoformat())
        self.assertEqual(out["deck_side"], "left")

    def test_live_metadata_when_paused_is_empty(self):
        out = sse.live_metadata_for_clients(track("s1", playing=False, progress_ms=30000))
        self.assertEqual(out["songid"], "")
        self.assertIs(out["playing"], False)
        self.assertIsNone(out["started_at"])
        self.assertIsNone(out["elapsed_ms"])
        self.assertNotIn("progress_ms", out)

    def test_metadata_for_clients_hides_paused_tracks(self):
        self.assertEqual(sse.metadata_for_clients(track("s1", playing=False))["songid"], "")
        self.assertEqual(sse.committed_metadata_for_clients()["songid"], "")
        sse.delayed_current_metadata.update(track("s1"))
        self.assertEqual(sse.committed_metadata_for_clients()["songid"], "s1")


class FakeLibrespot:
    def __init__(self, current):
        self.current = current

    async def get_playback_snapshot(self):
        return {"current": dict(self.current), "queue": []}


class FastAsyncio:
    """Stands in for the asyncio module inside sse so the poll loop sleeps briefly."""

    def __getattr__(self, name):
        return getattr(asyncio, name)

    @staticmethod
    async def sleep(delay, result=None):
        return await asyncio.sleep(min(delay, 0.05), result)


class PollTests(StateTestCase):
    DELAY = 0.3

    def setUp(self):
        super().setUp()
        self.patch(sse, "METADATA_DELAY", self.DELAY)
        self.patch(sse, "METADATA_DELAY_MS", self.DELAY * 1000)
        self.patch(sse, "asyncio", FastAsyncio())

    def test_metadata_is_realtime_at_once_and_delayed_for_the_stream(self):
        async def get(queue):
            return await asyncio.wait_for(queue.get(), 2)

        async def scenario():
            normal = sse.broadcaster.subscribe()
            realtime = sse.realtime_broadcaster.subscribe()
            fake = FakeLibrespot(track("s1", progress_ms=1000))
            poller = asyncio.create_task(
                sse.poll_spotify(fake, storage.listeners, storage.vote_skips)
            )
            try:
                event, data = await get(realtime)
                t_live = time.monotonic()
                self.assertEqual((event, data["songid"]), ("metadata", "s1"))
                self.assertEqual(data["elapsed_ms"], 1000)
                self.assertTrue(normal.empty())
                self.assertNotIn("songid", sse.delayed_current_metadata)

                event, data = await get(normal)
                self.assertEqual((event, data["songid"]), ("metadata", "s1"))
                self.assertGreaterEqual(time.monotonic() - t_live, self.DELAY / 2)
                self.assertEqual(sse.current_song_state["songid"], "s1")
                self.assertIsNotNone(sse.current_song_state["started_at"])
                self.assertEqual(sse.delayed_current_metadata["songid"], "s1")
                self.assertTrue(realtime.empty())

                # Song change: realtime metadata and skip-vote reset now; the
                # normal feed keeps s1 until the delay, then moves s1 to history.
                storage.vote_skips["s1"] = {"listener:ip"}
                fake.current = track("s2", progress_ms=0)
                event, data = await get(realtime)
                self.assertEqual((event, data["songid"]), ("metadata", "s2"))
                event, data = await get(realtime)
                self.assertEqual(event, "skipvotes")
                self.assertEqual(data, {"song": "s2", "count": 0, "needed": 2})
                self.assertNotIn("s1", storage.vote_skips)

                event, _ = await get(normal)
                self.assertEqual(event, "skipvotes")
                self.assertEqual(sse.delayed_current_metadata["songid"], "s1")

                event, data = await get(normal)
                self.assertEqual((event, data["songid"]), ("metadata", "s2"))
                event, data = await get(normal)
                self.assertEqual(event, "history")
                self.assertEqual(data["history"][0]["songid"], "s1")
                event, data = await get(realtime)
                self.assertEqual(event, "history")
                self.assertEqual(storage.play_history[0]["songid"], "s1")
                self.assertEqual(sse.current_song_state["songid"], "s2")
            finally:
                poller.cancel()
                try:
                    await poller
                except asyncio.CancelledError:
                    pass

        self.run_async(scenario())


class BroadcasterTests(StateTestCase):
    def test_order_is_preserved_per_client(self):
        async def scenario():
            b = sse.SSEBroadcaster()
            q1, q2 = b.subscribe(), b.subscribe()
            for i in range(5):
                await b.broadcast("e", {"i": i})
            expected = [("e", {"i": i}) for i in range(5)]
            for q in (q1, q2):
                self.assertEqual([q.get_nowait() for _ in range(5)], expected)

        self.run_async(scenario())

    def test_queue_is_bounded(self):
        self.assertEqual(sse.SSEBroadcaster().subscribe().maxsize, 100)

    def test_full_client_is_dropped(self):
        async def scenario():
            b = sse.SSEBroadcaster()
            slow, fast = b.subscribe(), b.subscribe()
            for i in range(100):
                await b.broadcast("e", {"i": i})
                fast.get_nowait()
            self.assertEqual(b.clients, [slow, fast])
            await b.broadcast("e", {"i": 100})
            self.assertEqual(b.clients, [fast])
            # The dropped client keeps only the close marker, nothing stale.
            self.assertEqual(slow.qsize(), 1)
            self.assertIs(slow.get_nowait(), sse.SSE_CLOSED)
            self.assertEqual(fast.get_nowait(), ("e", {"i": 100}))
            b.unsubscribe(slow)  # already gone: no error

        self.run_async(scenario())


class SseInitialStateTests(ApiTestCase):
    """Reads the event generators directly: TestClient buffers whole responses."""

    def setUp(self):
        super().setUp()
        add_listener("person-1")
        add_listener("person-2")
        add_listener("screen-1", display=True)
        storage.play_history.append(dict(track("old"), played_at=time.time()))
        storage.upcoming_queue.append({"songid": "next1"})
        sse.delayed_current_metadata.update(track("delayed"))
        sse.live_input_state.update(live=True, realtime=False)
        self.playback = {"current": track("live", progress_ms=5000)}

    def read(self, endpoint, path, query=b"", count=1, after=None):
        """Read ``count`` events, then optionally broadcast and read one more."""
        async def scenario():
            response = await endpoint(make_request(path, query))
            body = response.body_iterator
            events = []
            try:
                for _ in range(count):
                    events.append(parse_sse(await asyncio.wait_for(body.__anext__(), 2)))
                if after:
                    await after()
                    events.append(parse_sse(await asyncio.wait_for(body.__anext__(), 2)))
            finally:
                await body.aclose()
            return events

        return self.run_async(scenario())

    def test_events_initial_state(self):
        async def push():
            self.assertEqual(len(sse.broadcaster.clients), 1)
            await sse.broadcaster.broadcast("skipvotes", {"song": "x", "count": 1, "needed": 2})

        events = self.read(main.sse_events, "/events", count=5, after=push)
        self.assertEqual(
            [e for e, _ in events],
            ["metadata", "listeners", "history", "queue", "source", "skipvotes"],
        )
        data = dict(events[:5])
        self.assertEqual(data["metadata"]["songid"], "delayed")
        self.assertEqual(data["listeners"], {"count": 2})
        self.assertEqual(data["history"]["history"][0]["songid"], "old")
        self.assertEqual(data["queue"], {"queue": [{"songid": "next1"}]})
        self.assertEqual(data["source"], {"live": True})
        self.assertEqual(sse.broadcaster.clients, [])  # unsubscribed on close

    def test_realtime_without_flags_sends_everything(self):
        events = self.read(main.sse_events_realtime, "/events-realtime", count=5)
        self.assertEqual(
            [e for e, _ in events], ["metadata", "history", "queue", "source", "listeners"]
        )
        data = dict(events)
        self.assertEqual(data["metadata"]["songid"], "live")
        self.assertEqual(data["metadata"]["elapsed_ms"], 5000)
        self.assertEqual(data["source"], {"live": False})
        self.assertEqual(data["listeners"], {"count": 2})
        self.assertEqual(sse.realtime_broadcaster.clients, [])

    def test_realtime_music_only(self):
        async def push():
            await sse.realtime_broadcaster.broadcast("listeners", {"count": 9})
            await sse.realtime_broadcaster.broadcast("queue", {"queue": []})

        events = self.read(
            main.sse_events_realtime, "/events-realtime", b"music=1", count=4, after=push
        )
        self.assertEqual(
            [e for e, _ in events], ["metadata", "history", "queue", "source", "queue"]
        )

    def test_realtime_listeners_only(self):
        async def push():
            await sse.realtime_broadcaster.broadcast("metadata", {"songid": "x"})
            await sse.realtime_broadcaster.broadcast("listeners", {"count": 7})

        events = self.read(
            main.sse_events_realtime, "/events-realtime", b"music=0&listeners=1",
            count=1, after=push,
        )
        self.assertEqual(events, [("listeners", {"count": 2}), ("listeners", {"count": 7})])

    def test_overflowed_stream_ends_so_the_client_reconnects(self):
        async def scenario():
            response = await main.sse_events(make_request("/events"))
            body = response.body_iterator
            try:
                for _ in range(5):  # initial state
                    await asyncio.wait_for(body.__anext__(), 2)
                # Nobody reads while 101 events arrive: the queue overflows.
                for i in range(101):
                    await sse.broadcaster.broadcast("e", {"i": i})
                self.assertEqual(sse.broadcaster.clients, [])
                with self.assertRaises(StopAsyncIteration):
                    await asyncio.wait_for(body.__anext__(), 2)
            finally:
                await body.aclose()

        self.run_async(scenario())

    def test_overflowed_realtime_stream_ends(self):
        async def scenario():
            response = await main.sse_events_realtime(make_request("/events-realtime", b"music=1"))
            body = response.body_iterator
            try:
                for _ in range(4):  # music initial state
                    await asyncio.wait_for(body.__anext__(), 2)
                for i in range(101):
                    await sse.realtime_broadcaster.broadcast("listeners", {"count": i})
                self.assertEqual(sse.realtime_broadcaster.clients, [])
                # The close marker ends the stream even though "listeners" is filtered out.
                with self.assertRaises(StopAsyncIteration):
                    await asyncio.wait_for(body.__anext__(), 2)
            finally:
                await body.aclose()

        self.run_async(scenario())


class RealtimeSnapshotTests(ApiTestCase):
    """New realtime connections and vote lookups reuse the poller's snapshot while fresh."""

    def set_snapshot(self, current, age=0.0):
        sse.latest_snapshot.update(
            current=dict(current),
            polled_at=datetime.now(timezone.utc) - timedelta(seconds=age),
            monotonic=time.monotonic() - age,
        )

    def first_realtime_event(self, query=b"music=1"):
        async def scenario():
            response = await main.sse_events_realtime(make_request("/events-realtime", query))
            body = response.body_iterator
            try:
                return parse_sse(await asyncio.wait_for(body.__anext__(), 2))
            finally:
                await body.aclose()

        return self.run_async(scenario())

    def test_fresh_snapshot_skips_the_playback_lookup(self):
        self.set_snapshot(track("snap", progress_ms=30000))
        event, data = self.first_realtime_event()
        self.assertEqual(event, "metadata")
        self.assertEqual(data["songid"], "snap")
        self.assertAlmostEqual(data["elapsed_ms"], 30000, delta=1000)
        self.assertEqual(self.get_current_playback_calls(), 0)

    def test_snapshot_timing_is_anchored_to_its_own_poll_time(self):
        self.set_snapshot(track("snap", progress_ms=30000), age=2.0)
        _, data = self.first_realtime_event()
        # Position is progress plus the snapshot's age; the anchor is the same instant.
        self.assertAlmostEqual(data["elapsed_ms"], 32000, delta=1000)
        anchor = datetime.fromisoformat(data["started_at"])
        self.assertAlmostEqual(
            (datetime.now(timezone.utc) - anchor).total_seconds(), 32, delta=1
        )

    def test_stale_snapshot_falls_back_to_go_librespot(self):
        self.set_snapshot(track("snap", progress_ms=30000), age=sse.REALTIME_SNAPSHOT_MAX_AGE + 5)
        self.playback = {"current": track("fresh", progress_ms=1000)}
        _, data = self.first_realtime_event()
        self.assertEqual(data["songid"], "fresh")
        self.assertEqual(self.get_current_playback_calls(), 1)

    def test_no_snapshot_falls_back_to_go_librespot(self):
        self.playback = {"current": track("fresh", progress_ms=1000)}
        _, data = self.first_realtime_event()
        self.assertEqual(data["songid"], "fresh")
        self.assertEqual(self.get_current_playback_calls(), 1)

    def test_no_music_subscription_skips_the_playback_lookup(self):
        self.set_snapshot(track("snap"))
        event, data = self.first_realtime_event(b"listeners=1")
        self.assertEqual((event, data), ("listeners", {"count": 0}))
        self.assertEqual(self.get_current_playback_calls(), 0)

    def test_skip_stats_and_vote_use_a_fresh_snapshot(self):
        self.set_snapshot(track("snap"))
        add_listener("person-1")
        self.assertEqual(self.client.get("/skip/stats").json()["song"], "snap")
        self.assertEqual(self.get_current_playback_calls(), 0)

    def get_current_playback_calls(self):
        return main.librespot.get_current_playback.await_count


if __name__ == "__main__":
    support.unittest.main()
