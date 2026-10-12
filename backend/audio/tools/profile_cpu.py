#!/usr/bin/env python3
"""CPU and memory profile of the audio container, read from /proc. Standard library only.

Run it inside the running container. The image does not contain tools/, so pipe the file in:

    docker compose exec -T audio python3 - --seconds 10 < audio/tools/profile_cpu.py
    docker compose exec -T audio python3 - --seconds 20 --meters --json < audio/tools/profile_cpu.py

It samples go-librespot, Liquidsoap, the FFmpeg processor and encoder, the VBAN helpers and
the Python supervisor, and every thread of each. CPU is a percentage of one core (100 = one
core fully used). Thread names are the kernel names that bridge.py and control.py set from
their Python thread names, for example monitor-drain, monitor-analyze and processor-stdout.

--meters keeps the control API's meter stream open for the first half of the sample. The
report then has a window with the meters open and one with them closed, so the cost of
the meter analysis is the difference between the two.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import threading
import time

PROC = "/proc"
TICKS = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
NAMED_ROLES = {
    "go-librespot": "go-librespot",
    "liquidsoap": "liquidsoap",
    "vban_emitter": "vban-emitter",
    "vban_receptor": "vban-receptor",
}


def read_text(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return None


def role_of(argv: list[str]) -> str | None:
    """The process's role from its command line, or None when it is not part of the audio chain."""
    if not argv:
        return None
    base = os.path.basename(argv[0])
    if base in NAMED_ROLES:
        return NAMED_ROLES[base]
    if base.startswith("ffmpeg"):
        if "tee" in argv:
            return "ffmpeg encoder"
        if "pipe:1" in argv:
            return "ffmpeg processor"
        return "ffmpeg"
    if len(argv) > 1 and os.path.basename(argv[1]) == "run.py":
        return "supervisor"
    return None


def parse_stat(text: str) -> tuple[int, int, int] | None:
    """(utime + stime in ticks, start time in ticks, RSS in pages) from a /proc/<pid>/stat line."""
    # The command name is in parentheses and may contain spaces, so split after the last ')'.
    fields = text[text.rindex(")") + 2:].split()
    try:
        cpu = int(fields[11]) + int(fields[12])  # fields 14 and 15
        started = int(fields[19])  # field 22
        rss = int(fields[21])  # field 24
    except (ValueError, IndexError):
        return None
    return cpu, started, rss


def sample() -> tuple[float, dict[int, dict]]:
    """Process and thread CPU counters now. Process identity is (pid, start time), so a reused pid is not mixed up."""
    processes: dict[int, dict] = {}
    own = os.getpid()
    for entry in os.listdir(PROC):
        if not entry.isdigit() or int(entry) == own:
            continue
        pid = int(entry)
        raw = read_text(f"{PROC}/{pid}/cmdline")
        if not raw:
            continue  # kernel threads and exited processes have no command line
        role = role_of([part for part in raw.split("\0") if part])
        stat = read_text(f"{PROC}/{pid}/stat")
        parsed = parse_stat(stat) if stat else None
        if role is None or parsed is None:
            continue
        cpu, started, rss = parsed
        threads = {}
        for tid in os.listdir(f"{PROC}/{pid}/task"):
            thread_stat = read_text(f"{PROC}/{pid}/task/{tid}/stat")
            thread_name = (read_text(f"{PROC}/{pid}/task/{tid}/comm") or "?").strip()
            thread_parsed = parse_stat(thread_stat) if thread_stat else None
            if thread_parsed is not None:
                threads[int(tid)] = {"name": thread_name, "cpu": thread_parsed[0]}
        processes[pid] = {"role": role, "started": started, "cpu": cpu, "rss_pages": rss, "threads": threads}
    return time.monotonic(), processes


def percent(delta_ticks: int, seconds: float) -> float:
    return 100.0 * delta_ticks / TICKS / seconds if seconds > 0 else 0.0


def summarize(first: tuple[float, dict], last: tuple[float, dict]) -> dict:
    """CPU per process and per thread name between two samples. Threads sharing a name are added together."""
    seconds = last[0] - first[0]
    result = []
    for pid, now in last[1].items():
        before = first[1].get(pid)
        if before is None or before["started"] != now["started"]:
            continue  # started after the first sample, or a different process with the same pid
        by_name: dict[str, int] = {}
        for tid, thread in now["threads"].items():
            earlier = before["threads"].get(tid)
            if earlier is None or earlier["name"] != thread["name"]:
                continue
            by_name[thread["name"]] = by_name.get(thread["name"], 0) + thread["cpu"] - earlier["cpu"]
        result.append({
            "pid": pid,
            "role": now["role"],
            "cpu_percent": round(percent(now["cpu"] - before["cpu"], seconds), 2),
            "rss_mb": round(now["rss_pages"] * PAGE / 2**20, 1),
            "threads": [
                {"name": name, "cpu_percent": round(percent(ticks, seconds), 2)}
                for name, ticks in sorted(by_name.items(), key=lambda item: -item[1])
            ],
        })
    result.sort(key=lambda item: -item["cpu_percent"])
    return {"seconds": round(seconds, 2), "processes": result}


def hold_meters(host: str, port: int, seconds: float, result: dict) -> None:
    """Read the meter stream for the given time, as the admin panel does, and count its events."""
    events = 0
    try:
        connection = http.client.HTTPConnection(host, port, timeout=5)
        connection.request("GET", "/api/meters")
        response = connection.getresponse()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            line = response.readline()
            if not line:
                break
            if line.startswith(b"data:"):
                events += 1
        connection.close()
    except OSError as exc:
        result["error"] = str(exc)
    result["events"] = events


def print_window(label: str, window: dict) -> None:
    print(f"\n== {label}: {window['seconds']} s ==")
    print(f"{'PROCESS':<18}{'CPU %':>8}{'RSS MB':>9}")
    for process in window["processes"]:
        print(f"{process['role']:<18}{process['cpu_percent']:>8.2f}{process['rss_mb']:>9.1f}")
        for thread in process["threads"]:
            if thread["cpu_percent"] >= 0.05:
                print(f"  {thread['name']:<16}{thread['cpu_percent']:>8.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="CPU and memory profile of the audio container.")
    parser.add_argument("--seconds", type=float, default=10.0, help="sample length (default 10)")
    parser.add_argument("--meters", action="store_true",
                        help="hold the meter stream open for the first half of the sample")
    parser.add_argument("--control", default="127.0.0.1:8090", help="control API host:port")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error("--seconds must be positive")
    host, _, port = args.control.rpartition(":")

    windows: list[tuple[str, dict]] = []
    meter_result: dict = {}
    start = sample()
    if args.meters:
        half = args.seconds / 2
        holder = threading.Thread(target=hold_meters, args=(host or "127.0.0.1", int(port), half, meter_result))
        holder.start()
        time.sleep(half)
        middle = sample()
        holder.join()
        time.sleep(max(0.0, args.seconds - half))
        end = sample()
        windows = [("meters open", summarize(start, middle)), ("meters closed", summarize(middle, end))]
    else:
        time.sleep(args.seconds)
        windows = [("all", summarize(start, sample()))]

    if args.json:
        print(json.dumps({"windows": [{"label": label, **window} for label, window in windows],
                          "meters": meter_result or None}, indent=2))
        return
    for label, window in windows:
        print_window(label, window)
    if args.meters:
        print(f"\nmeter events received: {meter_result.get('events', 0)}"
              + (f" (error: {meter_result['error']})" if "error" in meter_result else ""))


if __name__ == "__main__":
    main()
