#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Short Bot (FFmpeg + CUDA/NVENC) — minimal Tkinter GUI

Defaults (client):
- Default speed (≤10 min): 4.0×
- Recurse subfolders: ON
- Use CUDA HWAccel decode: ON
- Overwrite outputs: ON

Speed rule based on video length:
- Videos ≤ 10 minutes → use default speed (UI, default 4×)
- Videos > 10 minutes → automatically switch to 5×

IMPORTANT behavior change (client testing request):
- NEVER skip a source video just because it becomes too short after speed-up.
- If effective duration after speed-up is too short to reach MIN_EFFECTIVE_DURATION:
    try lower speeds: 4× -> 3× -> 2× (or 5× -> 4× -> 3× -> 2×)
  If still too short even at 2×:
    keep the clip as-is (use best/lowest speed attempted) and render anyway.
- Also, do NOT post-skip outputs that are shorter than MIN_EFFECTIVE_DURATION.
  We keep them (especially for truly short source clips).

Everything else stays the same (output duration, encoder, etc.).
"""

from __future__ import annotations

import os
import sys
import json
import time
import queue
import signal
import threading
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple

import tkinter as tk
from tkinter import ttk, filedialog, messagebox


VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".mxf", ".avi", ".m4v", ".webm",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".mts"
}

# --- Client speed rule ---
LENGTH_THRESHOLD_SEC = 10 * 60  # 10 minutes
SPEED_LONG = 5.0                # > 10 min
DEFAULT_SPEED_SHORT = 4.0        # <= 10 min (UI default)

# Skip logic threshold (kept for logging / decisions, BUT WE DO NOT SKIP ANYMORE)
MIN_EFFECTIVE_DURATION = 50.0  # seconds (after speed-up) — used only to decide fallback speeds


def _subprocess_no_window_kwargs() -> dict:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startupinfo, "creationflags": subprocess.CREATE_NO_WINDOW}


@dataclass
class JobConfig:
    input_dir: Path
    output_dir: Path
    out_duration: float = 60.0           # seconds (50–60)
    encoder: str = "h264_nvenc"          # h264_nvenc | hevc_nvenc
    preset: str = "p5"                   # NVENC preset (p1..p7; p5 good default)
    cq: int = 23                         # constant quality (lower=better, larger=smaller)
    maxrate: str = "25M"
    bufsize: str = "50M"
    audio_bitrate: str = "160k"
    out_fps: Optional[int] = None        # e.g. 30; None keeps source cadence
    overwrite: bool = True              # DEFAULT ON (client)
    use_hwaccel: bool = True            # DEFAULT ON (client)
    recurse: bool = True                # DEFAULT ON (client)
    dry_run: bool = False


def which_ffmpeg() -> str:
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    local = Path(__file__).with_name(exe)
    if local.exists():
        return str(local)
    return exe


def which_ffprobe() -> str:
    exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    local = Path(__file__).with_name(exe)
    if local.exists():
        return str(local)
    return exe


def is_video_file(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in VIDEO_EXTS


def list_videos(root: Path, recurse: bool) -> List[Path]:
    if recurse:
        files = [p for p in root.rglob("*") if is_video_file(p)]
    else:
        files = [p for p in root.iterdir() if is_video_file(p)]
    files.sort(key=lambda x: x.name.lower())
    return files


def safe_out_name(src: Path) -> str:
    return f"{src.stem}_short.mp4"


def probe_duration_seconds(path: Path) -> Optional[float]:
    """Returns duration in seconds using ffprobe, or None if ffprobe fails."""
    try:
        cmd = [
            which_ffprobe(),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(path),
        ]
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.DEVNULL,
            text=True,
            **_subprocess_no_window_kwargs()
        ).strip()
        if not out:
            return None
        return float(out)
    except Exception:
        return None


def choose_initial_speed(src_duration_sec: float, base_speed_short: float) -> float:
    """Client rule: <=10 min uses base_speed_short, >10 min uses 5x."""
    return float(base_speed_short) if src_duration_sec <= LENGTH_THRESHOLD_SEC else SPEED_LONG


def candidate_speeds_for(initial_speed: float) -> List[float]:
    """
    Fallback ladder to ensure we DON'T SKIP.
    - If initial is 5x: try 5,4,3,2
    - If initial is 4x: try 4,3,2
    - If initial is 3x: try 3,2
    - Otherwise: try initial,2 (unique, descending-ish)
    """
    ladder = []
    if initial_speed >= 5.0 - 1e-9:
        ladder = [5.0, 4.0, 3.0, 2.0]
    elif initial_speed >= 4.0 - 1e-9:
        ladder = [4.0, 3.0, 2.0]
    elif initial_speed >= 3.0 - 1e-9:
        ladder = [3.0, 2.0]
    else:
        ladder = [float(initial_speed), 2.0]
    # Deduplicate while preserving order
    out = []
    for s in ladder:
        if s not in out:
            out.append(s)
    return out


def pick_speed_no_skip(src_duration_sec: float, initial_speed: float) -> Tuple[float, List[Tuple[float, float]]]:
    """
    Decide speed with fallback:
    Try candidate speeds until effective_duration >= MIN_EFFECTIVE_DURATION.
    If none qualify, return the LAST speed in ladder (lowest / slowest) anyway.
    Returns (chosen_speed, trials) where trials are [(speed, effective_duration_sec), ...]
    """
    trials: List[Tuple[float, float]] = []
    ladder = candidate_speeds_for(initial_speed)

    chosen = ladder[-1]
    for s in ladder:
        eff = src_duration_sec / s if s > 0 else 0.0
        trials.append((s, eff))
        if eff >= MIN_EFFECTIVE_DURATION:
            chosen = s
            break
    return chosen, trials


def build_ffmpeg_cmd(cfg: JobConfig, src: Path, dst_tmp: Path, speed: float) -> List[str]:
    """
    FFmpeg graph:
      video: setpts=PTS/speed
      audio: atempo chain to support >2.0 speed (0.5..2.0 each)
    Cut: -t cfg.out_duration (after speed-up).
    """
    ffmpeg = which_ffmpeg()

    speed = float(speed)
    if speed <= 0:
        raise ValueError("Speed must be > 0")

    # audio atempo supports 0.5..2.0 per filter; chain to reach >2.0
    atempo_filters = []
    remaining = speed
    while remaining > 2.0 + 1e-9:
        atempo_filters.append("atempo=2.0")
        remaining /= 2.0
    atempo_filters.append(f"atempo={remaining:.6f}".rstrip("0").rstrip("."))

    # video speed-up
    v_filter = f"setpts=PTS/{speed:.6f}".rstrip("0").rstrip(".")
    if cfg.out_fps:
        v_filter = f"{v_filter},fps={int(cfg.out_fps)}"

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "warning",
        "-y" if cfg.overwrite else "-n",
    ]

    if cfg.use_hwaccel:
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]

    cmd += [
        "-i", str(src),
        "-t", f"{cfg.out_duration:.3f}",
        "-map", "0:v:0?",
        "-map", "0:a:0?",
        "-vf", v_filter,
        "-af", ",".join(atempo_filters),
    ]

    if cfg.encoder not in {"h264_nvenc", "hevc_nvenc"}:
        raise ValueError("Encoder must be h264_nvenc or hevc_nvenc")

    cmd += [
        "-c:v", cfg.encoder,
        "-preset", cfg.preset,
        "-rc", "vbr",
        "-cq", str(int(cfg.cq)),
        "-maxrate", cfg.maxrate,
        "-bufsize", cfg.bufsize,
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
    ]

    cmd += [
        "-c:a", "aac",
        "-b:a", cfg.audio_bitrate,
        "-ac", "2",
        "-ar", "48000",
    ]

    cmd += [
        "-progress", "pipe:1",
        "-nostats",
        "-f", "mp4",
        str(dst_tmp),
    ]
    return cmd


def run_ffmpeg_with_progress(cmd: List[str], stop_event: threading.Event) -> Tuple[int, str]:
    """
    Runs FFmpeg, parses -progress lines, returns (returncode, last_progress_json_str).
    Continuously drains stderr to avoid deadlocks.
    """
    startupinfo = None
    creationflags = 0
    preexec_fn = None

    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        creationflags = subprocess.CREATE_NO_WINDOW
    else:
        preexec_fn = os.setsid

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        universal_newlines=True,
        startupinfo=startupinfo,
        creationflags=creationflags,
        preexec_fn=preexec_fn,
    )

    last = {}
    stderr_tail: List[str] = []
    stderr_lock = threading.Lock()

    def drain_stderr():
        try:
            if not proc.stderr:
                return
            for line in proc.stderr:
                line = line.rstrip("\n")
                with stderr_lock:
                    stderr_tail.append(line)
                    if len(stderr_tail) > 200:
                        stderr_tail[:] = stderr_tail[-200:]
        except Exception:
            pass

    t = threading.Thread(target=drain_stderr, daemon=True)
    t.start()

    def kill_proc():
        try:
            if os.name == "nt":
                proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
                time.sleep(0.3)
                proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    try:
        while True:
            if stop_event.is_set():
                kill_proc()
                break

            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                break

            line = line.strip()
            if not line:
                continue

            if "=" in line:
                k, v = line.split("=", 1)
                last[k.strip()] = v.strip()

        rc = proc.wait(timeout=None)

        with stderr_lock:
            if stderr_tail:
                last["stderr_tail"] = "\n".join(stderr_tail[-60:])

        return rc, json.dumps(last, ensure_ascii=False)

    finally:
        try:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass


class ShortBotApp(tk.Tk):
    def __init__(self):
        super().__init__()

        def resource_path(rel: str) -> str:
            base = getattr(sys, "_MEIPASS", Path(__file__).parent)
            return str(Path(base) / rel)

        try:
            self.iconbitmap(resource_path("app.ico"))
        except Exception:
            pass

        self.title("Short Bot — FFmpeg + CUDA (NVENC)")
        self.geometry("860x520")

        self.msg_q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

        self.input_dir = tk.StringVar()
        self.output_dir = tk.StringVar()

        # UI speed is "default speed for ≤10m" (client default 4.0)
        self.base_speed = tk.DoubleVar(value=DEFAULT_SPEED_SHORT)

        self.duration = tk.DoubleVar(value=60.0)
        self.encoder = tk.StringVar(value="h264_nvenc")
        self.preset = tk.StringVar(value="p5")
        self.cq = tk.IntVar(value=23)
        self.out_fps = tk.StringVar(value="")  # optional

        # DEFAULTS per client request:
        self.recurse = tk.BooleanVar(value=True)
        self.hwaccel = tk.BooleanVar(value=True)
        self.overwrite = tk.BooleanVar(value=True)

        self.total_files = 0
        self.done_files = 0

        self._build_ui()
        self._tick()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Input folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.input_dir, width=70).grid(row=0, column=1, sticky="we", padx=8)
        ttk.Button(top, text="Browse...", command=self._browse_input).grid(row=0, column=2)

        ttk.Label(top, text="Output folder:").grid(row=1, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.output_dir, width=70).grid(row=1, column=1, sticky="we", padx=8)
        ttk.Button(top, text="Browse...", command=self._browse_output).grid(row=1, column=2)

        top.columnconfigure(1, weight=1)

        opts = ttk.LabelFrame(self, text="Settings")
        opts.pack(fill="x", **pad)

        ttk.Label(opts, text="Default speed (≤10 min):").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(opts, from_=1.0, to=10.0, increment=0.1, textvariable=self.base_speed, width=8)\
            .grid(row=0, column=1, sticky="w", padx=6)

        ttk.Label(opts, text="Output duration (s):").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(opts, from_=5.0, to=90.0, increment=1.0, textvariable=self.duration, width=8)\
            .grid(row=0, column=3, sticky="w", padx=6)

        ttk.Label(opts, text="Encoder:").grid(row=0, column=4, sticky="w")
        ttk.Combobox(opts, textvariable=self.encoder, values=["h264_nvenc", "hevc_nvenc"], width=12, state="readonly")\
            .grid(row=0, column=5, sticky="w", padx=6)

        ttk.Label(opts, text="NVENC preset:").grid(row=1, column=0, sticky="w")
        ttk.Combobox(opts, textvariable=self.preset, values=["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
                     width=8, state="readonly").grid(row=1, column=1, sticky="w", padx=6)

        ttk.Label(opts, text="CQ:").grid(row=1, column=2, sticky="w")
        ttk.Spinbox(opts, from_=15, to=35, increment=1, textvariable=self.cq, width=8)\
            .grid(row=1, column=3, sticky="w", padx=6)

        ttk.Label(opts, text="Force FPS (optional):").grid(row=1, column=4, sticky="w")
        ttk.Entry(opts, textvariable=self.out_fps, width=12).grid(row=1, column=5, sticky="w", padx=6)

        ttk.Checkbutton(opts, text="Recurse subfolders", variable=self.recurse)\
            .grid(row=2, column=0, columnspan=2, sticky="w", padx=2)
        ttk.Checkbutton(opts, text="Use CUDA hwaccel decode", variable=self.hwaccel)\
            .grid(row=2, column=2, columnspan=2, sticky="w", padx=2)
        ttk.Checkbutton(opts, text="Overwrite outputs", variable=self.overwrite)\
            .grid(row=2, column=4, columnspan=2, sticky="w", padx=2)

        note = ttk.Label(
            opts,
            text=(
                f"Rule: if video > 10 minutes, speed auto-switches to {SPEED_LONG:.1f}×.\n"
                f"No-skip: if clip is too short after speed-up, bot tries lower speeds (…→3×→2×) and still renders."
            )
        )
        note.grid(row=3, column=0, columnspan=6, sticky="w", padx=2, pady=(6, 0))

        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", **pad)

        self.btn_start = ttk.Button(ctrl, text="Start", command=self._start)
        self.btn_stop = ttk.Button(ctrl, text="Stop", command=self._stop, state="disabled")
        self.btn_start.pack(side="left")
        self.btn_stop.pack(side="left", padx=8)

        self.prog = ttk.Progressbar(ctrl, orient="horizontal", mode="determinate")
        self.prog.pack(side="left", fill="x", expand=True, padx=10)

        self.lbl = ttk.Label(ctrl, text="Idle")
        self.lbl.pack(side="right")

        logf = ttk.LabelFrame(self, text="Log")
        logf.pack(fill="both", expand=True, **pad)

        self.log = tk.Text(logf, height=14, wrap="word")
        self.log.pack(fill="both", expand=True, padx=8, pady=8)

    def _browse_input(self):
        d = filedialog.askdirectory(title="Select input folder")
        if d:
            self.input_dir.set(d)
            out = Path(d) / "_SHORTS_OUT"
            self.output_dir.set(str(out))

    def _browse_output(self):
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self.output_dir.set(d)

    def _append_log(self, s: str):
        self.log.insert("end", s + "\n")
        self.log.see("end")

    def _set_status(self, s: str):
        self.lbl.config(text=s)

    def _start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Running", "Already running.")
            return

        in_dir = Path(self.input_dir.get().strip() or "")
        out_dir = Path(self.output_dir.get().strip() or "")

        if not in_dir.exists() or not in_dir.is_dir():
            messagebox.showerror("Error", "Please select a valid input folder.")
            return

        if not out_dir:
            messagebox.showerror("Error", "Please select a valid output folder.")
            return

        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Error", f"Cannot create output folder:\n{e}")
            return

        fps_txt = self.out_fps.get().strip()
        fps_val = int(fps_txt) if fps_txt else None

        base_speed = float(self.base_speed.get())
        if base_speed <= 0:
            messagebox.showerror("Error", "Speed must be > 0")
            return

        cfg = JobConfig(
            input_dir=in_dir,
            output_dir=out_dir,
            out_duration=float(self.duration.get()),
            encoder=self.encoder.get().strip(),
            preset=self.preset.get().strip(),
            cq=int(self.cq.get()),
            out_fps=fps_val,
            recurse=bool(self.recurse.get()),
            use_hwaccel=False,
            overwrite=bool(self.overwrite.get()),
        )

        self.stop_event.clear()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.prog["value"] = 0

        self._append_log(f"FFmpeg:  {which_ffmpeg()}")
        self._append_log(f"FFprobe: {which_ffprobe()}")
        self._append_log(f"Input:   {cfg.input_dir}")
        self._append_log(f"Output:  {cfg.output_dir}")
        self._append_log(
            f"Defaults: speed(≤10m)={base_speed:.1f}x, speed(>10m)={SPEED_LONG:.1f}x, "
            f"duration={cfg.out_duration}s, enc={cfg.encoder}, preset={cfg.preset}, cq={cfg.cq}, "
            f"fps={cfg.out_fps or 'auto'}, recurse={cfg.recurse}, hwaccel={cfg.use_hwaccel}, overwrite={cfg.overwrite}"
        )
        self._append_log("----")

        self.worker_thread = threading.Thread(target=self._worker, args=(cfg, base_speed), daemon=True)
        self.worker_thread.start()

    def _stop(self):
        self.stop_event.set()
        self._append_log("Stop requested… (finishing current file / terminating FFmpeg)")
        self.btn_stop.config(state="disabled")

    def _worker(self, cfg: JobConfig, base_speed: float):
        videos = list_videos(cfg.input_dir, cfg.recurse)
        self.msg_q.put(("total", len(videos)))

        run_log = cfg.output_dir / f"_shortbot_run_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
        try:
            log_fp = run_log.open("w", encoding="utf-8")
        except Exception:
            log_fp = None

        def log_event(obj: dict):
            if log_fp:
                log_fp.write(json.dumps(obj, ensure_ascii=False) + "\n")
                log_fp.flush()

        ffmpeg_fail_hwaccel = 0

        for idx, src in enumerate(videos, start=1):
            if self.stop_event.is_set():
                break

            dst = cfg.output_dir / safe_out_name(src)
            dst_tmp = cfg.output_dir / (dst.stem + ".tmp.mp4")

            if dst.exists() and not cfg.overwrite:
                self.msg_q.put(("log", f"[{idx}/{len(videos)}] SKIP exists: {dst.name}"))
                self.msg_q.put(("done", 1))
                continue

            try:
                if dst_tmp.exists():
                    dst_tmp.unlink()
            except Exception:
                pass

            self.msg_q.put(("log", f"[{idx}/{len(videos)}] Processing: {src.name}"))
            self.msg_q.put(("status", f"{idx}/{len(videos)}"))

            src_duration = probe_duration_seconds(src)

            # If we cannot probe duration, DO NOT SKIP.
            # Just use base speed (or 5x rule can't be applied), and render.
            if src_duration is None:
                initial_speed = base_speed
                speed_for_file = initial_speed
                trials = []
                self.msg_q.put(("log", f"  WARN: Cannot read duration via ffprobe. Rendering anyway at {speed_for_file:.1f}x (no-skip)."))
                log_event({
                    "file": str(src),
                    "status": "duration_unreadable_render_anyway",
                    "speed": speed_for_file,
                })
            else:
                # Apply rule + no-skip fallback ladder
                initial_speed = choose_initial_speed(src_duration, base_speed)
                speed_for_file, trials = pick_speed_no_skip(src_duration, initial_speed)

                rule_tag = "≤10m" if src_duration <= LENGTH_THRESHOLD_SEC else ">10m"
                self.msg_q.put(("log", f"  Rule: duration={src_duration/60:.2f} min ({rule_tag}) -> initial speed={initial_speed:.1f}x"))

                if trials:
                    # log ladder decisions
                    # If none qualified, we still keep the last speed and render (no skip).
                    qualifies = any(eff >= MIN_EFFECTIVE_DURATION for _, eff in trials)
                    if qualifies:
                        # Find first qualifying
                        for s, eff in trials:
                            if eff >= MIN_EFFECTIVE_DURATION:
                                self.msg_q.put(("log", f"  Length check: {s:.1f}x gives {eff:.2f}s (>= {MIN_EFFECTIVE_DURATION:.0f}s) -> using {s:.1f}x"))
                                break
                    else:
                        last_s, last_eff = trials[-1]
                        self.msg_q.put((
                            "log",
                            f"  Length check: even at {last_s:.1f}x effective={last_eff:.2f}s (< {MIN_EFFECTIVE_DURATION:.0f}s). "
                            f"Rendering anyway (no-skip) at {last_s:.1f}x."
                        ))

            # Build command
            try:
                cmd = build_ffmpeg_cmd(cfg, src, dst_tmp, speed_for_file)
            except Exception as e:
                self.msg_q.put(("log", f"  ERROR building cmd: {e}"))
                log_event({"file": str(src), "status": "cmd_error", "error": str(e)})
                self.msg_q.put(("done", 1))
                continue

            started = time.time()
            rc, prog = run_ffmpeg_with_progress(cmd, self.stop_event)

            if rc != 0 and cfg.use_hwaccel:
                ffmpeg_fail_hwaccel += 1
                self.msg_q.put(("log", "  HWACCEL failed; retrying without -hwaccel cuda…"))
                cfg2 = JobConfig(**{**cfg.__dict__, "use_hwaccel": False})
                cmd2 = build_ffmpeg_cmd(cfg2, src, dst_tmp, speed_for_file)
                rc, prog = run_ffmpeg_with_progress(cmd2, self.stop_event)

            elapsed = time.time() - started

            if self.stop_event.is_set():
                try:
                    if dst_tmp.exists():
                        dst_tmp.unlink()
                except Exception:
                    pass
                break

            if rc == 0 and dst_tmp.exists():
                # IMPORTANT CHANGE: do NOT delete/skip if output is shorter than MIN_EFFECTIVE_DURATION.
                # Short sources must still produce output.
                try:
                    if dst.exists() and cfg.overwrite:
                        dst.unlink()
                    dst_tmp.replace(dst)
                    out_dur = probe_duration_seconds(dst)
                    self.msg_q.put(("log", f"  OK -> {dst.name}  ({elapsed:.1f}s)  out_dur={out_dur:.2f}s" if out_dur is not None else f"  OK -> {dst.name}  ({elapsed:.1f}s)"))
                    log_event({
                        "file": str(src),
                        "status": "ok",
                        "elapsed_sec": elapsed,
                        "speed": speed_for_file,
                        "src_duration": src_duration,
                        "trials": trials,
                        "output_duration": out_dur,
                        "progress": json.loads(prog) if prog else {},
                    })
                except Exception as e:
                    self.msg_q.put(("log", f"  ERROR finalizing output: {e}"))
                    log_event({"file": str(src), "status": "finalize_error", "error": str(e), "progress": prog})
                    try:
                        if dst_tmp.exists():
                            dst_tmp.unlink()
                    except Exception:
                        pass
            else:
                self.msg_q.put(("log", f"  FAIL (rc={rc})  ({elapsed:.1f}s)"))
                try:
                    if dst_tmp.exists():
                        dst_tmp.unlink()
                except Exception:
                    pass
                log_event({"file": str(src), "status": "fail", "rc": rc, "elapsed_sec": elapsed, "speed": speed_for_file, "progress": prog})

            self.msg_q.put(("done", 1))

        if log_fp:
            log_fp.close()

        self.msg_q.put(("finished", {"hwaccel_failures": ffmpeg_fail_hwaccel}))

    def _tick(self):
        try:
            while True:
                typ, payload = self.msg_q.get_nowait()
                if typ == "total":
                    self.total_files = int(payload)
                    self.done_files = 0
                    self.prog["maximum"] = max(1, self.total_files)
                    self.prog["value"] = 0
                    self._set_status(f"0/{self.total_files}")
                    self._append_log(f"Found {self.total_files} video(s).")
                elif typ == "done":
                    self.done_files += int(payload)
                    self.prog["value"] = self.done_files
                    self._set_status(f"{self.done_files}/{self.total_files}")
                elif typ == "log":
                    self._append_log(str(payload))
                elif typ == "finished":
                    info = payload if isinstance(payload, dict) else {}
                    self._append_log("----")
                    self._append_log(f"Finished. Done: {self.done_files}/{self.total_files}.")
                    if info.get("hwaccel_failures"):
                        self._append_log(
                            f"Note: {info['hwaccel_failures']} file(s) failed CUDA decode and were retried without hwaccel."
                        )
                    self.btn_start.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self._set_status("Idle")
        except queue.Empty:
            pass

        self.after(200, self._tick)


def main():
    app = ShortBotApp()
    app.mainloop()


if __name__ == "__main__":
    main()
