#!/usr/bin/env python3
"""Processing lab: synthesise test programme, run filter chains, measure results.

Stdlib only, plus the ffmpeg binary. Run inside the project image through
tools/lab.sh so FFmpeg 5.1 matches production. Paths are
relative to backend/audio (the container's working directory) or absolute.

  gen [OUTDIR]                           synthesise programme.wav + segments.json
  run [--filter-script F] --in W --out W  processing.ffmpeg graph + production tail (scripts/run.py)
  liq --in W --out W [--script S]        Liquidsoap passthrough via the test harness
  measure W [--ref R] [--json]           loudness, bands, width, sections, latency
  compare A B [--json]                   two files side by side
  encode W [--bitrate 256k]              AAC (ADTS) round trip with decoded true peak
"""
from __future__ import annotations

import argparse
import array
import importlib.util
import json
import math
import operator
import os
import re
import statistics
import subprocess
import sys
import wave
from pathlib import Path

RATE = 44_100
ROOT = Path(__file__).resolve().parents[1]
MATERIAL = ROOT / ".lab" / "material"
SILENT_LUFS = -69.0
DEC = 40  # coarse decimation for the latency search (about 1.1 kHz)
BANDS = [("<120 Hz", 0, 120), ("120-400 Hz", 120, 400), ("400-2k Hz", 400, 2000),
         ("2k-6k Hz", 2000, 6000), (">6k Hz", 6000, 0)]


def ffrun(args: list[str], data: bytes | None = None, loglevel: str = "info") -> subprocess.CompletedProcess:
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", loglevel, *args]
    proc = subprocess.run(cmd, input=data if data is not None else b"",
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode:
        raise SystemExit(f"ffmpeg failed ({proc.returncode}): {proc.stderr.decode('utf-8', 'replace')[-1500:]}")
    return proc


# --- WAV helpers -----------------------------------------------------------

def read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (2, 2, RATE):
            raise SystemExit(f"{path}: expected 44.1 kHz stereo s16 WAV")
        return wav.readframes(wav.getnframes())


def write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(pcm)


def samples(pcm: bytes) -> array.array:
    arr = array.array("h")
    arr.frombytes(pcm[: len(pcm) - len(pcm) % 4])
    if sys.byteorder == "big":
        arr.byteswap()
    return arr


# --- gen -------------------------------------------------------------------

KICK = "0.6*sin(2*PI*(38+34*exp(-30*mod(t,0.5)))*mod(t,0.5))*exp(-9*mod(t,0.5))"
FADE = "max(0,1-t/6)"


def _tone(exprs: str, seconds: float) -> str:
    return f"aevalsrc=exprs='{exprs}':s={RATE}:d={seconds}"


def _noise(color: str, amp: float, seconds: float, seed: int, side: str = "both") -> str:
    pan = {"both": "c0=c0|c1=c0", "L": "c0=c0|c1=0*c0", "R": "c0=0*c0|c1=c0"}[side]
    return f"anoisesrc=c={color}:a={amp}:d={seconds}:r={RATE}:s={seed},pan=stereo|{pan}"


def _mix(*chains: str) -> str:
    body = ";".join(f"{chain}[m{i}]" for i, chain in enumerate(chains))
    labels = "".join(f"[m{i}]" for i in range(len(chains)))
    return f"{body};{labels}amix=inputs={len(chains)}:normalize=0"


# (name, role, seconds, FFmpeg filtergraph). Only lavfi sources and filters are used.
SEGMENTS = [
    ("quiet_acoustic", "quiet", 15,
     _mix(_tone("0.09*(sin(2*PI*220*t)*exp(-6*mod(t,0.5))+0.6*sin(2*PI*330*t)*exp(-5*mod(t+0.2,0.7))"
                "+0.4*sin(2*PI*495*t)*exp(-7*mod(t+0.1,0.9)))|0.03*(sin(2*PI*221.3*t)*exp(-6*mod(t,0.5))"
                "+0.6*sin(2*PI*331.1*t)*exp(-5*mod(t+0.2,0.7))+0.4*sin(2*PI*494.2*t)*exp(-7*mod(t+0.1,0.9)))", 15),
          _noise("pink", 0.02, 15, 1))),
    ("loud_dense", "loud", 20,
     _mix(_tone(f"{KICK}+0.2*sin(2*PI*41*t)|{KICK}+0.2*sin(2*PI*41*t)", 20),
          _noise("white", 0.5, 20, 2) + ",highpass=f=1500,lowpass=f=7000,"
          "aeval=exprs='val(0)*exp(-14*mod(t+0.5,1))|val(1)*exp(-14*mod(t+0.5,1))'",
          _noise("pink", 0.25, 20, 3) + ",highpass=f=80,lowpass=f=5000")
     + ",alimiter=limit=0.89:attack=1:release=50:level=false"),  # generator peak guard only
    ("sibilant_hf", "hf", 10,
     _mix(_noise("white", 0.4, 10, 4) + ",highpass=f=4500,aeval=exprs='val(0)*(0.5+0.5*sin(2*PI*3.5*t))^2"
          "|val(1)*(0.5+0.5*sin(2*PI*3.5*t))^2'",
          _tone("0.12*(sin(2*PI*180*t)+0.5*sin(2*PI*360*t)+0.3*sin(2*PI*540*t))*(0.5+0.5*sin(2*PI*3.5*t+1.2))"
                "|0.12*(sin(2*PI*180.4*t)+0.5*sin(2*PI*360.8*t)+0.3*sin(2*PI*541*t))*(0.5+0.5*sin(2*PI*3.5*t+1.2))",
                10))),
    ("mono_ish", "mono", 8,
     _mix(_tone("0.2*(sin(2*PI*110*t)+0.5*sin(2*PI*165*t)+0.3*sin(2*PI*330*t))*(0.6+0.4*sin(2*PI*0.25*t))"
                "|0.2*(sin(2*PI*110*t)+0.5*sin(2*PI*165*t)+0.3*sin(2*PI*330*t))*(0.6+0.4*sin(2*PI*0.25*t))*0.995"
                "+0.002*sin(2*PI*5000*t)", 8),
          _noise("pink", 0.02, 8, 5))),
    ("wide_stereo", "wide", 8,
     _mix(_tone("0.18*sin(2*PI*392*t)*(0.7+0.3*sin(2*PI*0.5*t))|0.18*sin(2*PI*659*t)*(0.7+0.3*cos(2*PI*0.5*t))", 8),
          _noise("pink", 0.03, 8, 6, "L"), _noise("pink", 0.03, 8, 7, "R"))),
    ("fade_out", "fade", 8,
     _mix(_tone(f"(0.15*sin(2*PI*262*t)+0.1*sin(2*PI*330*t)+0.08*sin(2*PI*392*t))*{FADE}"
                f"|(0.15*sin(2*PI*263*t)+0.1*sin(2*PI*331*t)+0.08*sin(2*PI*393*t))*{FADE}", 8),
          _noise("pink", 0.03, 8, 8) + f",aeval=exprs='val(0)*{FADE}|val(1)*{FADE}'")),
    ("silence", "silence", 3, f"anullsrc=r={RATE}:cl=stereo:d=3"),
]


def _level(pcm: bytes) -> tuple[float, float]:
    arr = samples(pcm)
    if not arr:
        return float("-inf"), float("-inf")
    rms = math.sqrt(sum(map(operator.mul, arr, arr)) / len(arr)) / 32768.0
    peak = max(abs(min(arr)), abs(max(arr))) / 32768.0
    return db(rms), db(peak)


def db(value: float) -> float:
    return 20.0 * math.log10(value) if value > 0 else float("-inf")


def cmd_gen(args: argparse.Namespace) -> int:
    out = args.outdir
    out.mkdir(parents=True, exist_ok=True)
    pcm = bytearray()
    segments = []
    start = 0.0
    print(f"{'segment':<16}{'role':<9}{'sec':>6}{'RMS dBFS':>11}{'peak dBFS':>11}")
    for name, role, seconds, graph in SEGMENTS:
        data = ffrun(["-filter_complex", graph, "-t", str(seconds), "-f", "s16le", "-ar", str(RATE),
                      "-ac", "2", "pipe:1"], loglevel="error").stdout
        frames = len(data) // 4
        if abs(frames - seconds * RATE) > 2:
            raise SystemExit(f"segment {name} has {frames / RATE:.3f} s, expected {seconds} s")
        rms, peak = _level(data)
        print(f"{name:<16}{role:<9}{frames / RATE:>6.2f}{rms:>11.1f}{peak:>11.1f}")
        segments.append({"name": name, "role": role, "start": round(start, 6),
                         "duration": round(frames / RATE, 6)})
        start += frames / RATE
        pcm.extend(data)
    write_wav(out / "programme.wav", bytes(pcm))
    (out / "segments.json").write_text(json.dumps(segments, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out / 'programme.wav'} ({start:.2f} s) and segments.json")
    return 0


# --- run / liq -------------------------------------------------------------

def production_module():
    """Load scripts/run.py so the lab builds the same filtergraph as the container."""
    spec = importlib.util.spec_from_file_location("jmm_run", ROOT / "scripts" / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_run(args: argparse.Namespace) -> int:
    production = production_module()
    graph = production.load_processing_graph(args.filter_script)
    if args.loud_lufs is not None:
        # Scale so the loud segment of the programme sits at --loud-lufs before the graph.
        loud = next(item for item in json.loads((MATERIAL / "segments.json").read_text(encoding="utf-8"))
                    if item["role"] == "loud")
        text = ffrun(["-ss", f"{loud['start']:.6f}", "-t", f"{loud['duration']:.6f}", "-i", str(args.inp),
                      "-af", "ebur128", "-f", "null", "-"], loglevel="info").stderr.decode("utf-8", "replace")
        measured = float(re.findall(r"I:\s+(\S+) LUFS", text[text.rfind("Summary:"):])[0])
        graph = graph.replace("[0:a:0]", f"[0:a:0]volume={args.loud_lufs - measured:.3f}dB,", 1)
    chain = production.audio_filtergraph(False, graph, safety=not args.no_production_limiter)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Same input as production: raw s16le 44.1 kHz stereo on stdin.
    ffrun(["-f", "s16le", "-ar", str(RATE), "-ac", "2", "-i", "pipe:0", "-filter_complex", chain,
           "-map", "[stream]", "-c:a", "pcm_s16le", "-ar", str(RATE), "-ac", "2", "-f", "wav", str(args.out)],
          data=read_pcm(args.inp))
    print(f"wrote {args.out}; graph: {chain}")
    return 0


def cmd_liq(args: argparse.Namespace) -> int:
    # Reuse the production test harness (real FIFO, paced input, raw stdout) instead of a copy.
    os.environ["RADIO_LIQ_PATH"] = str(args.script.resolve())
    spec = importlib.util.spec_from_file_location("test_processing", ROOT / "tests" / "test_processing.py")
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    if harness.LIQUIDSOAP is None:
        raise SystemExit("liquidsoap not found; run inside the project image")
    case = harness.LiquidsoapProcessingTest("run_processor")
    out_samples, seconds = case.run_processor(read_pcm(args.inp))
    if sys.byteorder == "big":
        out_samples.byteswap()
    write_wav(args.out, out_samples.tobytes())
    print(f"wrote {args.out} ({len(out_samples) / 2 / RATE:.2f} s from Liquidsoap, {seconds:.2f} s wall)")
    return 0


# --- measurement -----------------------------------------------------------

def astats(path: Path, chain: str = "", start: float | None = None, seconds: float | None = None) -> dict:
    pre: list[str] = []
    if start is not None:
        pre += ["-ss", f"{start:.6f}"]
    if seconds is not None:
        pre += ["-t", f"{seconds:.6f}"]
    af = ",".join(part for part in (chain, "astats=metadata=0") if part)
    text = ffrun([*pre, "-i", str(path), "-af", af, "-f", "null", "-"]).stderr.decode("utf-8", "replace")
    idx = text.rfind("Overall")
    overall = text[idx:] if idx >= 0 else ""

    def field(label: str) -> float:
        match = re.search(rf"{label}:\s*(\S+)", overall)
        return float(match.group(1)) if match else float("nan")

    return {"rms": field("RMS level dB"), "peak": field("Peak level dB")}


def loudness(path: Path) -> dict:
    text = ffrun(["-i", str(path), "-af", "ebur128=peak=sample+true:framelog=verbose", "-f", "null", "-"],
                 loglevel="verbose").stderr.decode("utf-8", "replace")
    cut = text.rfind("Summary:")
    frames, summary = (text[:cut], text[cut:]) if cut >= 0 else (text, "")

    def grab(pattern: str) -> float:
        match = re.search(pattern, summary)
        return float(match.group(1)) if match else float("nan")

    short_term = [float(s) for _, s in re.findall(r"M:\s*(\S+)\s+S:\s*(\S+)", frames)]
    return {
        "I": grab(r"I:\s+(\S+) LUFS"),
        "LRA": grab(r"LRA:\s+(\S+) LU"),
        "TP": grab(r"True peak:\s+Peak:\s+(\S+) dBFS"),
        "SP": grab(r"Sample peak:\s+Peak:\s+(\S+) dBFS"),
        "S": short_term,
    }


def band_chain(low: int, high: int) -> str:
    parts = []
    if low:
        parts.append(f"highpass=f={low}")
    if high:
        parts.append(f"lowpass=f={high}")
    return ",".join(parts)


def analyse(path: Path, segs: dict | None, shift: float = 0.0) -> dict:
    seconds = len(samples(read_pcm(path))) / 2 / RATE
    lo = loudness(path)
    full = astats(path)
    res: dict = {
        "duration_s": round(seconds, 2),
        "integrated_lufs": lo["I"],
        "lra_lu": lo["LRA"],
        "true_peak_dbtp": lo["TP"],
        "sample_peak_dbfs": lo["SP"],
        "plr_db": lo["TP"] - lo["I"],
        "rms_dbfs": full["rms"],
        "crest_db": full["peak"] - full["rms"],
    }
    gated = [v for v in lo["S"] if v > SILENT_LUFS]
    if gated:
        res.update(st_min=min(gated), st_median=statistics.median(gated), st_max=max(gated))
    for name, low, high in BANDS:
        res[f"band {name}"] = astats(path, band_chain(low, high))["rms"]
    mid = astats(path, "pan=mono|c0=0.5*c0+0.5*c1")["rms"]
    side = astats(path, "pan=mono|c0=0.5*c0-0.5*c1")["rms"]
    res["side_mid_db"] = side - mid
    if segs:
        if "quiet" in segs:
            q = segs["quiet"]
            res["quiet_rms_dbfs"] = astats(path, start=q["start"] + 1.0 + shift,
                                           seconds=q["duration"] - 2.0)["rms"]
        if "silence" in segs:
            s = segs["silence"]
            quiet_tail = astats(path, start=s["start"] + 0.3 + shift, seconds=s["duration"] - 0.6)
            res["silence_rms_dbfs"] = quiet_tail["rms"]
            res["silence_peak_dbfs"] = quiet_tail["peak"]
    return res


def mono_list(path: Path) -> list[int]:
    arr = samples(read_pcm(path))
    return [left + right for left, right in zip(arr[0::2], arr[1::2])]


def best_lag(a: list[int], b: list[int], start: int, length: int, lags: range) -> int:
    """Lag L maximising normalised correlation of b[start+L:] against a[start:]."""
    window = a[start:start + length]
    best_score, best = -math.inf, 0
    for lag in lags:
        if start + lag < 0:
            continue
        seg = b[start + lag:start + lag + length]
        if len(seg) != len(window):
            continue
        energy = sum(map(operator.mul, seg, seg))
        if energy <= 0:
            continue
        score = sum(map(operator.mul, window, seg)) / math.sqrt(energy)
        if score > best_score:
            best_score, best = score, lag
    return best


def estimate_lag(ref: Path, target: Path, t_ref: float) -> int:
    """Samples by which target lags ref (target[n + lag] matches ref[n]), at t_ref seconds."""
    a, b = mono_list(ref), mono_list(target)
    coarse_a = [sum(a[i:i + DEC]) for i in range(0, len(a), DEC)]
    coarse_b = [sum(b[i:i + DEC]) for i in range(0, len(b), DEC)]
    coarse = best_lag(coarse_a, coarse_b, int(t_ref * RATE / DEC), int(2 * RATE / DEC),
                      range(-int(0.5 * RATE / DEC), int(1.0 * RATE / DEC) + 1))
    centre = coarse * DEC
    return best_lag(a, b, int(t_ref * RATE), RATE // 2, range(centre - DEC, centre + DEC + 1))


def load_segments(explicit: Path | None, near: Path) -> dict | None:
    candidates = [explicit] if explicit else [near.parent / "segments.json", MATERIAL / "segments.json"]
    for path in candidates:
        if path and path.is_file():
            return {item["role"]: item for item in json.loads(path.read_text(encoding="utf-8"))}
    return None


def num(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        if math.isnan(value):
            return "n/a"
        if math.isinf(value):
            return "-inf" if value < 0 else "inf"
        return f"{value:.2f}"
    return str(value)


def print_table(names: list[Path], results: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps({str(n): r for n, r in zip(names, results)}, indent=2))
        return
    labels = [n.name[:24] for n in names]
    keys = list(dict.fromkeys(k for r in results for k in r))
    head = f"{'metric':<22}" + "".join(f"{label:>22}" for label in labels)
    if len(results) == 2:
        head += f"{'delta':>12}"
    print(head)
    for key in keys:
        line = f"{key:<22}" + "".join(f"{fmt(r.get(key)):>22}" for r in results)
        if len(results) == 2 and num(results[0].get(key)) and num(results[1].get(key)):
            line += f"{results[1][key] - results[0][key]:>12.2f}"
        print(line)


def cmd_measure(args: argparse.Namespace) -> int:
    segs = load_segments(args.segments, args.ref or args.wav)
    names: list[Path] = []
    results: list[dict] = []
    lag = 0
    if args.ref:
        if segs and "loud" in segs:
            lag = estimate_lag(args.ref, args.wav, segs["loud"]["start"] + 4.0)
        else:
            print("no segments.json found; latency not estimated", file=sys.stderr)
        names.append(args.ref)
        results.append(analyse(args.ref, segs))
    names.append(args.wav)
    results.append(analyse(args.wav, segs, lag / RATE))
    if args.ref:
        results[-1]["latency_ms"] = round(lag * 1000 / RATE, 2)
    print_table(names, results, args.json)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    segs = load_segments(None, args.a)
    results = [analyse(args.a, segs), analyse(args.b, segs)]
    print_table([args.a, args.b], results, args.json)
    return 0


def cmd_encode(args: argparse.Namespace) -> int:
    outdir = args.outdir or args.wav.parent
    aac = outdir / f"{args.wav.stem}.{args.bitrate}.adts"
    decoded = outdir / f"{args.wav.stem}.{args.bitrate}.decoded.wav"
    ffrun(["-i", str(args.wav), "-vn", "-c:a", "aac", "-b:a", args.bitrate, "-f", "adts", str(aac)])
    ffrun(["-i", str(aac), "-ar", str(RATE), "-ac", "2", "-c:a", "pcm_s16le", "-f", "wav", str(decoded)])
    names = [args.wav, decoded]
    print_table(names, [analyse(args.wav, None), analyse(decoded, None)], args.json)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Radio processing lab (see module docstring).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("gen", help="synthesise test programme")
    p.add_argument("outdir", type=Path, nargs="?", default=MATERIAL)
    p.set_defaults(func=cmd_gen)

    p = sub.add_parser("run", help="FFmpeg filter chain on a WAV")
    p.add_argument("--filter-script", type=Path, default=ROOT / "config" / "processing.ffmpeg")
    p.add_argument("--in", dest="inp", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--no-production-limiter", action="store_true",
                   help="skip the alimiter from scripts/run.py that production appends")
    p.add_argument("--loud-lufs", type=float, default=None,
                   help="scale the programme so its loud segment is at this loudness before the graph")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("liq", help="production Liquidsoap script on a WAV")
    p.add_argument("--in", dest="inp", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--script", type=Path, default=ROOT / "config" / "processing.liq")
    p.set_defaults(func=cmd_liq)

    p = sub.add_parser("measure", help="measure a WAV, optionally against a reference")
    p.add_argument("wav", type=Path)
    p.add_argument("--ref", type=Path)
    p.add_argument("--segments", type=Path)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_measure)

    p = sub.add_parser("compare", help="two WAVs side by side")
    p.add_argument("a", type=Path)
    p.add_argument("b", type=Path)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("encode", help="AAC round trip and decoded measurements")
    p.add_argument("wav", type=Path)
    p.add_argument("--bitrate", default="256k")
    p.add_argument("--outdir", type=Path)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_encode)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
