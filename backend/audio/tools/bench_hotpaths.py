#!/usr/bin/env python3
"""Timings of the audio bridge and meter hot paths, old against new where the code changed.

Standard library only, and it runs on any Python 3 (the scripts import fcntl, which only exists
on Linux, so it is stubbed when missing; the functions timed here do not use it):

    python3 backend/audio/tools/bench_hotpaths.py
    python3 backend/audio/tools/bench_hotpaths.py --repeat 10 --json

The numbers are per call. One 10 ms block is the unit that the render loop handles, so the
time per block divided by 10 ms is the share of one core that the path uses in steady state.
Timings depend on the machine; compare the old and new columns on the same run.
"""

from __future__ import annotations

import argparse
import array
import json
import random
import sys
import timeit
import types
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
try:
    import fcntl  # noqa: F401
except ImportError:  # not Linux: bridge.py only uses fcntl for pipe sizes
    sys.modules["fcntl"] = types.SimpleNamespace(fcntl=lambda *args: None, F_SETPIPE_SZ=1031)

import bridge  # noqa: E402
import control  # noqa: E402


def dry_block_before(block: bytes) -> bytes:
    """bridge.dry_block as it was: a generator over the samples. Kept for comparison."""
    samples = array.array("h")
    samples.frombytes(block)
    if not bridge._LITTLE_ENDIAN:
        samples.byteswap()
    return bridge._float_bytes(array.array("f", (value * bridge._DRY_SCALE for value in samples)))


def time_call(fn, repeat: int, number: int) -> float:
    """Best time per call in microseconds over repeat runs of number calls each."""
    return min(timeit.repeat(fn, number=number, repeat=repeat)) / number * 1e6


def main() -> None:
    parser = argparse.ArgumentParser(description="Time the audio hot paths, old against new.")
    parser.add_argument("--repeat", type=int, default=5, help="runs per measurement; the best is reported")
    parser.add_argument("--number", type=int, default=300, help="calls per run")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args()

    rnd = random.Random(1)
    block = array.array("h", (rnd.randrange(-32768, 32768) for _ in range(bridge.BLOCK_FRAMES * 2))).tobytes()
    column = array.array("h", (rnd.randrange(-32768, 32768) for _ in range(bridge.BLOCK_FRAMES * 2)))
    outgoing = bytes(bridge.OUT_BLOCK_BYTES)
    incoming = bytes(bridge.OUT_BLOCK_BYTES)

    rows = []
    before = time_call(lambda: dry_block_before(block), args.repeat, args.number)
    after = time_call(lambda: bridge.dry_block(block), args.repeat, args.number)
    rows.append({"path": "dry_block (per 10 ms block)", "before_us": round(before, 1),
                 "after_us": round(after, 1), "speedup": round(before / after, 2)})
    measure = time_call(lambda: control.measure(column), args.repeat, args.number)
    rows.append({"path": "meter measure (per 10 ms column, unchanged)", "before_us": None,
                 "after_us": round(measure, 1), "speedup": None})
    mix = time_call(lambda: bridge.mix_blocks(outgoing, incoming, 0, bridge.CROSSFADE_FRAMES),
                    args.repeat, max(1, args.number // 10))
    rows.append({"path": "mix_blocks (per crossfade block, unchanged)", "before_us": None,
                 "after_us": round(mix, 1), "speedup": None})

    if args.json:
        print(json.dumps(rows, indent=2))
        return
    print(f"{'path':<46}{'before us':>11}{'after us':>11}{'speedup':>9}")
    for row in rows:
        before_text = "-" if row["before_us"] is None else f"{row['before_us']:.1f}"
        speed_text = "-" if row["speedup"] is None else f"{row['speedup']:.2f}x"
        print(f"{row['path']:<46}{before_text:>11}{row['after_us']:>11.1f}{speed_text:>9}")


if __name__ == "__main__":
    main()
