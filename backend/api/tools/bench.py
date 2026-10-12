#!/usr/bin/env python3
"""Latency, SQLite query-count, SSE and memory benchmark for the API.

By default the app runs in this process on a local port (uvicorn, lifespan off, so
the playback poller and other background loops do not start) with a throwaway
SQLite database, seeded requests/bans/listeners, and stubbed Spotify, go-librespot
and Turnstile calls. With --url it only sends HTTP requests to a running server and
seeds nothing: admin endpoints need --key, and /request is only posted with
--post-requests because it queues real songs there.

Expected SQLite statements per request before optimisation, derived from reading
the code (not measured). Each is_banned/is_song_banned call opens its own connection.
  /metadata, /skip/stats  0
  /admin/requests         1 + 2R for R listed rows (R <= 100, the query's LIMIT):
                          one SELECT on requests, then per row one SELECT on bans
                          (is_banned) and one on song_bans (is_song_banned);
                          1 + 2R connections
  /admin/listeners        L for L listeners: one SELECT on bans each
  /request (new submission, auto-approve on)
                          4: SELECT bans, SELECT song_bans, SELECT auto_approve,
                          INSERT; the trace also shows the implicit BEGIN and the
                          COMMIT, so 6 traced statements over 3 connections
"""
import argparse
import asyncio
import json
import os
import shutil
import socket
import statistics
import sys
import tempfile
import time
import tracemalloc
import uuid

import httpx

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH_KEY = "bench-key"
TRACE = {"stmts": 0, "conns": 0}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", help="benchmark a running server instead of the in-process app")
    p.add_argument("--key", help="admin key for --url (admin endpoints are skipped without it)")
    p.add_argument("--post-requests", action="store_true", help="with --url, also POST /request")
    p.add_argument("--rows", type=int, default=100, help="seeded requests (in-process)")
    p.add_argument("--bans", type=int, default=100, help="seeded bans (in-process)")
    p.add_argument("--listeners", type=int, default=20, help="seeded listeners (in-process)")
    p.add_argument("-n", "--requests", type=int, default=200, help="requests per endpoint")
    p.add_argument("-c", "--concurrency", type=int, default=10)
    p.add_argument("--sse", type=int, default=50, help="concurrent /events streams to open")
    p.add_argument("--no-tracemalloc", action="store_true", help="skip tracemalloc (it adds overhead)")
    p.add_argument("--json", metavar="PATH", help="also write the results as JSON")
    return p.parse_args()


def setup_in_process(args):
    """Import the app against a temp database, stub external calls and seed data."""
    tmp = tempfile.mkdtemp(prefix="jmm-api-bench-")
    os.environ.update({
        "DB_FILE": os.path.join(tmp, "bench.db"),
        "TOKEN_CACHE_FILE": os.path.join(tmp, "token_cache.json"),
        "SPOTIFY_CLIENT_ID": "bench", "SPOTIFY_CLIENT_SECRET": "bench",
        "LIBRESPOT_API_URL": "http://127.0.0.1:9", "AUDIO_CONTROL_URL": "http://127.0.0.1:9",
        "TURNSTILE_SECRET": "", "AUTH_KEY": AUTH_KEY,
    })
    sys.path.insert(0, API_DIR)
    import main
    import storage

    storage.init_db()
    original_get_db = storage.get_db

    def traced_get_db():
        conn = original_get_db()
        TRACE["conns"] += 1
        conn.set_trace_callback(lambda _sql: TRACE.__setitem__("stmts", TRACE["stmts"] + 1))
        return conn

    # main reaches the database only through storage functions, so patching storage is enough.
    storage.get_db = traced_get_db

    async def nothing(*_args, **_kwargs):
        return None

    async def passed(*_args, **_kwargs):
        return True

    async def track_info(track_id):
        return {"song": "Bench", "songid": track_id, "artist": [{"id": "a", "name": "Artist"}],
                "album": "Album", "albumid": "b", "cover": ""}

    playback = {"current": {"songid": "benchsong", "song": "Bench", "playing": True,
                            "artist": [], "progress_ms": 0}}

    async def current_playback():
        return playback

    main.verify_turnstile = passed
    main.spotify_client.get_track_info = track_info
    main.librespot.add_to_queue = nothing
    main.librespot.get_status = nothing
    main.librespot.get_current_playback = current_playback

    conn = original_get_db()
    conn.executemany(
        """INSERT INTO requests (spotify_uri, track_name, artist_name, requester_name,
           requester_ip, status) VALUES (?, ?, ?, ?, ?, ?)""",
        [(f"track{i}", f"Track {i}", "Artist", "Bench", f"10.0.{i // 250}.{i % 250 + 1}",
          "pending" if i % 2 else "approved") for i in range(args.rows)],
    )
    conn.executemany(
        "INSERT INTO bans (ip, uuid, reason) VALUES (?, ?, 'bench')",
        [(f"172.16.{i // 250}.{i % 250 + 1}" if i % 4 else f"192.168.{i // 4 % 256}.0/24",
          "") for i in range(args.bans)],
    )
    conn.commit()
    conn.close()
    for i in range(args.listeners):
        lid = str(uuid.uuid4())
        storage.listeners[lid] = {"name": f"L{i}", "ip": f"10.1.0.{i % 250 + 1}",
                                  "last_seen": time.time(), "user_agent": "bench",
                                  "volume": None, "uuid": lid, "display": False,
                                  "theme": None, "effects": None}
    return main, storage, tmp


async def start_server(app):
    import uvicorn

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off",
                            log_level="warning", timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started or task.done():
            break
        await asyncio.sleep(0.05)
    if not server.started:
        raise RuntimeError("in-process server did not start")
    return server, task, f"http://127.0.0.1:{port}"


def plan(args, in_process, key, storage):
    admin = {"X-Auth-Key": key} if key else None

    def request_body(i):
        return {"uri": f"benchtrack{i % 50}", "name": "Bench",
                "submission_id": uuid.uuid4().hex, "turnstile": "bench"}

    steps = [("GET /metadata", "GET", "/metadata", None, None, None),
             ("GET /skip/stats", "GET", "/skip/stats", None, None, None)]
    if admin:
        steps += [("GET /admin/requests", "GET", "/admin/requests", admin, None, None),
                  ("GET /admin/listeners", "GET", "/admin/listeners", admin, None, None)]
    if in_process or args.post_requests:
        # In-process the per-IP rate limit is reset before each call so every request is served.
        reset = storage.rate_limits.clear if in_process else None
        steps.append(("POST /request", "POST", "/request", None, request_body, reset))
    return steps


async def send(client, method, path, headers, body, before, i):
    if before:
        before()
    kwargs = {"json": body(i)} if body else {}
    return await client.request(method, path, headers=headers, **kwargs)


async def measure(client, step, n, concurrency):
    _, method, path, headers, body, before = step
    latencies, statuses = [], {}
    sem = asyncio.Semaphore(concurrency)

    async def one(i):
        async with sem:
            t = time.perf_counter()
            resp = await send(client, method, path, headers, body, before, i)
            latencies.append((time.perf_counter() - t) * 1000)
            statuses[resp.status_code] = statuses.get(resp.status_code, 0) + 1

    await asyncio.gather(*(one(i) for i in range(n)))
    q = statistics.quantiles(latencies, n=100, method="inclusive") if len(latencies) > 1 else latencies * 99
    return {"n": n, "statuses": statuses, "p50_ms": q[49], "p95_ms": q[94], "p99_ms": q[98]}


async def sse_phase(base, n, subscribed=None):
    """Open n /events streams, time each to its first event, hold them, then close."""
    first, held, stop = [], [0], asyncio.Event()
    limits = httpx.Limits(max_connections=n + 10, max_keepalive_connections=0)
    async with httpx.AsyncClient(base_url=base, limits=limits, timeout=httpx.Timeout(10.0)) as client:
        async def hold():
            t = time.perf_counter()
            async with client.stream("GET", "/events") as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        first.append((time.perf_counter() - t) * 1000)
                        break
                held[0] += 1
                await stop.wait()
                held[0] -= 1

        tasks = [asyncio.create_task(hold()) for _ in range(n)]
        deadline = time.perf_counter() + 15
        while held[0] < n and not all(t.done() for t in tasks) and time.perf_counter() < deadline:
            await asyncio.sleep(0.05)
        result = {"requested": n, "held": held[0],
                  "server_subscribers": subscribed() if subscribed else None}
        stop.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [o for o in outcomes if isinstance(o, Exception)]
    result["errors"] = len(errors)
    if first:
        result["first_event_p50_ms"] = statistics.median(first)
        result["first_event_max_ms"] = max(first)
    return result


async def lag_sampler(samples, stop, interval=0.01):
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        t = loop.time()
        await asyncio.sleep(interval)
        samples.append(max(0.0, loop.time() - t - interval) * 1000)


def memory_info():
    """Current/peak resident set size in MB for this process, or None."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in (
                        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                        "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                        "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

            kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            c = Counters()
            c.cb = ctypes.sizeof(Counters)
            if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return None
            return {"rss_mb": c.WorkingSetSize / 2**20, "peak_rss_mb": c.PeakWorkingSetSize / 2**20}
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KB on Linux, bytes on macOS
        return {"rss_mb": None, "peak_rss_mb": peak / 2**20 if sys.platform == "darwin" else peak / 1024}
    except Exception:
        return None


async def run(args):
    in_process = not args.url
    server = task = storage = tmp = None
    if in_process:
        main, storage, tmp = setup_in_process(args)
        server, task, base = await start_server(main.app)
        key = AUTH_KEY
        subscribed = lambda: len(sys.modules["sse"].broadcaster.clients)  # noqa: E731
    else:
        base, key, subscribed = args.url.rstrip("/"), args.key, None
    results = {"mode": "in-process" if in_process else args.url, "endpoints": {},
               "seeded": {"rows": args.rows, "bans": args.bans, "listeners": args.listeners} if in_process else None}
    trace_mem = in_process and not args.no_tracemalloc
    if trace_mem:
        tracemalloc.start()
    lag, stop = [], asyncio.Event()
    sampler = asyncio.create_task(lag_sampler(lag, stop))
    try:
        limits = httpx.Limits(max_connections=args.concurrency + 5)
        async with httpx.AsyncClient(base_url=base, timeout=30, limits=limits) as client:
            for step in plan(args, in_process, key, storage):
                entry = {}
                if in_process:  # one request on its own, for exact query counts
                    TRACE.update(stmts=0, conns=0)
                    await send(client, *step[1:], 0)
                    entry.update(stmts=TRACE["stmts"], conns=TRACE["conns"])
                entry.update(await measure(client, step, args.requests, args.concurrency))
                results["endpoints"][step[0]] = entry
        results["sse"] = await sse_phase(base, args.sse, subscribed)
    finally:
        stop.set()
        await sampler
        if server:
            server.should_exit = True
            await task
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    lag.sort()
    results["loop_lag_ms"] = {"p99": lag[int(len(lag) * 0.99)] if lag else None,
                              "max": lag[-1] if lag else None, "samples": len(lag)}
    if in_process:
        results["memory"] = memory_info() or {}
        if trace_mem:
            results["memory"]["tracemalloc_peak_mb"] = tracemalloc.get_traced_memory()[1] / 2**20
            tracemalloc.stop()
    return results


def fmt(value, digits=1):
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}" if isinstance(value, float) else str(value)


def print_report(r):
    print(f"mode: {r['mode']}" + (f"  seeded: {r['seeded']}" if r.get("seeded") else ""))
    print(f"{'endpoint':<22}{'n':>6}{'2xx':>6}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}{'stmts':>7}{'conns':>7}")
    for name, e in r["endpoints"].items():
        ok = sum(v for k, v in e["statuses"].items() if 200 <= k < 300)
        print(f"{name:<22}{e['n']:>6}{ok:>6}{fmt(e['p50_ms']):>9}{fmt(e['p95_ms']):>9}"
              f"{fmt(e['p99_ms']):>9}{fmt(e.get('stmts')):>7}{fmt(e.get('conns')):>7}")
        other = {k: v for k, v in e["statuses"].items() if not 200 <= k < 300}
        if other:
            print(f"{'':<22}other statuses: {other}")
    s = r["sse"]
    print(f"SSE /events: held {s['held']}/{s['requested']}, errors {s['errors']}, "
          f"server subscribers {fmt(s['server_subscribers'])}, first event p50 "
          f"{fmt(s.get('first_event_p50_ms'))} ms, max {fmt(s.get('first_event_max_ms'))} ms")
    lag = r["loop_lag_ms"]
    print(f"event-loop lag: p99 {fmt(lag['p99'], 2)} ms, max {fmt(lag['max'], 2)} ms ({lag['samples']} samples)")
    if "memory" in r:
        m = r["memory"]
        print(f"memory: rss {fmt(m.get('rss_mb'))} MB, peak rss {fmt(m.get('peak_rss_mb'))} MB, "
              f"tracemalloc peak {fmt(m.get('tracemalloc_peak_mb'))} MB")


def main():
    args = parse_args()
    results = asyncio.run(run(args))
    print_report(results)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)


if __name__ == "__main__":
    main()
