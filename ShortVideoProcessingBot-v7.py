#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Short Bot (FFmpeg + CUDA/NVENC) v8 — Tkinter GUI

Preserved from v7 (UNCHANGED):
- Speed rule: ≤10 min → base_speed; >10 min → 5×
- No-skip guarantee (fallback ladder 4→3→2 etc.)
- All encoding defaults and parameters

New in v8:
- Random Duration Toggle (20–30 s random target per file)
- Minimum Duration Toggle (controls no-skip fallback ladder)
- Loop Mode (continuous folder processing until manually stopped)
- Include / Exclude Folders (per-subfolder selection dialog)
- Workflow Mode: Normal (long videos) vs Short Clips (10–60 s, no forced trim)
- Pause / Resume button (waits between files, never corrupts current job)
- Improved H265/HEVC and mixed-format handling (auto software-decode)
- Config persistence (JSON file next to the script)
- Fixed use_hwaccel wiring (was hardcoded False in v7)
- FFmpeg/FFprobe also searched in tools/ subdirectory
"""

from __future__ import annotations

import os
import sys
import json
import time
import queue
import random
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

# --- Client speed rule (PRESERVED FROM v7) ---
LENGTH_THRESHOLD_SEC = 10 * 60  # 10 minutes
SPEED_LONG = 5.0                 # > 10 min
DEFAULT_SPEED_SHORT = 4.0        # <= 10 min (UI default)

# Skip logic threshold (kept for logging / decisions, BUT WE DO NOT SKIP ANYMORE)
MIN_EFFECTIVE_DURATION = 50.0  # seconds (after speed-up) — used only to decide fallback speeds

# Random duration range (v8)
RANDOM_DURATION_MIN = 20.0
RANDOM_DURATION_MAX = 30.0

# Codecs that benefit from software decode (skip hwaccel for these)
HEVC_LIKE_CODECS = {"hevc", "h265", "vp9", "av1", "vc1"}

# Config persistence file (sits next to the script)
CONFIG_FILE = Path(__file__).with_suffix(".json")

# Loop mode: wait periods between scans (in 0.1-second increments for stop responsiveness)
LOOP_RESCAN_WAIT_SEC = 5    # wait between loop iterations
LOOP_EMPTY_WAIT_SEC = 10    # wait when no new files are found (overwrite=OFF)

# Output duration validation (normal mode only).
# After encoding we measure the real file duration with ffprobe and compare it
# to the expected output.  If the actual duration is below this fraction of the
# expected duration we consider the file broken and delete it.  70 % allows a
# small encoding variance while still catching obviously wrong files (e.g. 4 s
# instead of ~60 s).  The floor of 5 s ensures a minimum sensible output even
# for very short target durations.
OUTPUT_DURATION_MIN_RATIO = 0.70   # fraction of expected output duration
OUTPUT_DURATION_FLOOR_SEC = 5.0    # absolute minimum acceptable output (seconds)


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
    overwrite: bool = True               # DEFAULT ON (client)
    use_hwaccel: bool = True             # DEFAULT ON (client)
    recurse: bool = True                 # DEFAULT ON (client)
    dry_run: bool = False
    # v8 additions
    random_duration: bool = False        # use random 20–30 s target per file
    use_min_duration: bool = True        # apply MIN_EFFECTIVE_DURATION ladder
    workflow_mode: str = "normal"        # "normal" | "short_clips"
    loop_mode: bool = False              # continuously re-process folders
    include_folders: Optional[List[str]] = None  # None = all subfolders


def which_ffmpeg() -> str:
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    local = Path(__file__).with_name(exe)
    if local.exists():
        return str(local)
    tools_local = Path(__file__).parent / "tools" / exe
    if tools_local.exists():
        return str(tools_local)
    return exe


def which_ffprobe() -> str:
    exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    local = Path(__file__).with_name(exe)
    if local.exists():
        return str(local)
    tools_local = Path(__file__).parent / "tools" / exe
    if tools_local.exists():
        return str(tools_local)
    return exe


def is_video_file(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in VIDEO_EXTS


def list_videos(
    root: Path,
    recurse: bool,
    include_folders: Optional[List[str]] = None,
) -> List[Path]:
    """
    List video files under root.
    include_folders: if set, only recurse into those immediate subfolders of root.
                     Files directly in root are included regardless.
    """
    inc: Optional[set] = set(include_folders) if include_folders is not None else None

    if recurse:
        files: List[Path] = []
        for p in root.rglob("*"):
            if not is_video_file(p):
                continue
            rel_parts = p.relative_to(root).parts
            if len(rel_parts) > 1 and inc is not None:
                # File is inside a subfolder — check if that top-level folder is selected
                if rel_parts[0] not in inc:
                    continue
            files.append(p)
    else:
        files = [p for p in root.iterdir() if is_video_file(p)]

    files.sort(key=lambda x: x.name.lower())
    return files


def get_immediate_subfolders(root: Path) -> List[str]:
    """Return sorted list of immediate subfolder names (excluding _-prefixed dirs)."""
    try:
        return sorted(
            d.name for d in root.iterdir()
            if d.is_dir() and not d.name.startswith("_")
        )
    except Exception:
        return []


def safe_out_name(src: Path) -> str:
    return f"{src.stem}_short.mp4"


def probe_duration_seconds(path: Path) -> Optional[float]:
    """Returns duration in seconds using ffprobe, or None if ffprobe fails.

    Reads the video stream duration first (more accurate for output validation:
    the container/format duration can be inflated by -t even when FFmpeg only
    encoded a few seconds of frames).  Falls back to format-level duration if
    the stream duration is unavailable (e.g. audio-only files).
    """
    # Primary: video stream duration — reflects actual encoded frames
    try:
        cmd = [
            which_ffprobe(),
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=duration",
            "-of", "default=nw=1:nk=1",
            str(path),
        ]
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.DEVNULL,
            text=True,
            **_subprocess_no_window_kwargs()
        ).strip()
        if out and out != "N/A":
            return float(out)
    except Exception:
        pass

    # Fallback: format/container duration
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


def probe_video_codec(path: Path) -> Optional[str]:
    """Returns the primary video codec name (e.g. 'h264', 'hevc') or None."""
    try:
        cmd = [
            which_ffprobe(),
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name",
            "-of", "default=nw=1:nk=1",
            str(path),
        ]
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.DEVNULL,
            text=True,
            **_subprocess_no_window_kwargs()
        ).strip()
        return out.lower() if out else None
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


def pick_speed_no_skip(
    src_duration_sec: float,
    initial_speed: float,
    use_min_duration: bool = True,
) -> Tuple[float, List[Tuple[float, float]]]:
    """
    Decide speed with fallback ladder (PRESERVED FROM v7).
    If use_min_duration=False (short-clips mode), returns initial_speed immediately
    without attempting the fallback ladder.
    Returns (chosen_speed, trials) where trials = [(speed, effective_duration_sec), ...]
    """
    trials: List[Tuple[float, float]] = []

    if not use_min_duration:
        # Short-clips mode: use the chosen initial speed directly, no fallback
        eff = src_duration_sec / initial_speed if initial_speed > 0 else 0.0
        trials.append((initial_speed, eff))
        return initial_speed, trials

    ladder = candidate_speeds_for(initial_speed)
    chosen = ladder[-1]
    for s in ladder:
        eff = src_duration_sec / s if s > 0 else 0.0
        trials.append((s, eff))
        if eff >= MIN_EFFECTIVE_DURATION:
            chosen = s
            break
    return chosen, trials


def build_ffmpeg_cmd(
    cfg: JobConfig,
    src: Path,
    dst_tmp: Path,
    speed: float,
    src_codec: Optional[str] = None,
) -> List[str]:
    """
    Build the FFmpeg command.

    v8 changes:
    - src_codec: if HEVC/problematic, hwaccel is skipped automatically.
    - workflow_mode=="short_clips": the -t (duration trim) flag is omitted.

    FFmpeg graph:
      video: setpts=PTS/speed
      audio: atempo chain to support >2.0 speed (0.5..2.0 each)
    Cut: -t cfg.out_duration (after speed-up) — skipped in short_clips mode.
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

    # video speed-up: (PTS-STARTPTS) normalises start timestamp to 0 so that
    # sources with non-zero initial PTS (e.g. MPEG-TS, broadcast files) are
    # handled correctly and the output -t trim lands at the right point.
    speed_str = f"{speed:.6f}".rstrip("0").rstrip(".")
    v_filter = f"setpts=(PTS-STARTPTS)/{speed_str}"
    if cfg.out_fps:
        v_filter = f"{v_filter},fps={int(cfg.out_fps)}"

    # HEVC/problematic codecs: skip hwaccel to avoid decode failures.
    # If src_codec is None (probe failed), honour the user's hwaccel setting.
    use_hw = cfg.use_hwaccel and (
        src_codec is None or src_codec not in HEVC_LIKE_CODECS
    )

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "warning",
        "-y" if cfg.overwrite else "-n",
    ]

    if use_hw:
        # Use -hwaccel cuda for hardware decode but do NOT add
        # -hwaccel_output_format cuda.  Keeping decoded frames in GPU memory
        # (AV_PIX_FMT_CUDA) prevents software filters like setpts from working
        # correctly and produces extremely short outputs (3–15 s instead of the
        # expected ~60 s).  Without -hwaccel_output_format, frames are downloaded
        # to CPU after hardware decode so all software filters work normally, and
        # h264_nvenc / hevc_nvenc still accept CPU frames for encoding.
        cmd += ["-hwaccel", "cuda"]

    cmd += ["-i", str(src)]

    # Short-clips mode: no forced duration trim
    if cfg.workflow_mode != "short_clips":
        cmd += ["-t", f"{cfg.out_duration:.3f}"]

    cmd += [
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


# ─── Config persistence ────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load saved GUI settings from JSON file next to the script."""
    try:
        if CONFIG_FILE.exists():
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_config(data: dict) -> None:
    """Persist GUI settings to JSON file."""
    try:
        with CONFIG_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ─── Folder Selection Dialog ───────────────────────────────────────────────────

class FolderSelectDialog(tk.Toplevel):
    """
    Modal dialog listing all immediate subfolders of input_dir as checkboxes.
    result is a list of selected folder names, or None if cancelled.
    """

    def __init__(
        self,
        parent: tk.Tk,
        root_dir: Path,
        current_selection: Optional[List[str]],
    ):
        super().__init__(parent)
        self.title("Select Folders to Process")
        self.resizable(True, True)
        self.grab_set()
        self.result: Optional[List[str]] = None

        subfolders = get_immediate_subfolders(root_dir)
        if not subfolders:
            tk.Label(
                self,
                text="No subfolders found. Root-level files will be processed.",
                padx=20, pady=14,
            ).pack()
            ttk.Button(self, text="Close", command=self.destroy).pack(pady=6)
            return

        tk.Label(
            self,
            text="Check the folders you want to process (uncheck to skip):",
            padx=10, pady=6,
        ).pack(anchor="w")

        # Scrollable checkbox list
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True, padx=10)

        canvas = tk.Canvas(container, width=420, height=300)
        sb = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        sel_set = set(current_selection) if current_selection is not None else set(subfolders)
        self._vars: dict = {}
        for name in subfolders:
            var = tk.BooleanVar(value=(name in sel_set))
            self._vars[name] = var
            ttk.Checkbutton(inner, text=name, variable=var).pack(anchor="w", padx=4, pady=1)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=8)
        ttk.Button(btn_frame, text="Select All", command=self._select_all).pack(side="left")
        ttk.Button(btn_frame, text="Deselect All", command=self._deselect_all).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="OK", command=self._ok).pack(side="right")
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="right", padx=4)

    def _select_all(self):
        for v in self._vars.values():
            v.set(True)

    def _deselect_all(self):
        for v in self._vars.values():
            v.set(False)

    def _ok(self):
        self.result = [name for name, var in self._vars.items() if var.get()]
        self.destroy()


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

        self.title("Short Bot v8 — FFmpeg + CUDA (NVENC)")
        self.geometry("920x720")

        self.msg_q: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()  # set = running; clear = paused
        self._is_paused = False

        # Load persisted settings
        saved = load_config()

        self.input_dir = tk.StringVar(value=saved.get("input_dir", ""))
        self.output_dir = tk.StringVar(value=saved.get("output_dir", ""))

        # UI speed is "default speed for ≤10m" (client default 4.0)
        self.base_speed = tk.DoubleVar(value=saved.get("base_speed", DEFAULT_SPEED_SHORT))

        self.duration = tk.DoubleVar(value=saved.get("duration", 60.0))
        self.encoder = tk.StringVar(value=saved.get("encoder", "h264_nvenc"))
        self.preset = tk.StringVar(value=saved.get("preset", "p5"))
        self.cq = tk.IntVar(value=saved.get("cq", 23))
        self.out_fps = tk.StringVar(value=saved.get("out_fps", ""))  # optional

        # DEFAULTS per client request:
        self.recurse = tk.BooleanVar(value=saved.get("recurse", True))
        self.hwaccel = tk.BooleanVar(value=saved.get("hwaccel", True))
        self.overwrite = tk.BooleanVar(value=saved.get("overwrite", True))

        # v8 additions
        self.random_duration = tk.BooleanVar(value=saved.get("random_duration", False))
        self.use_min_duration = tk.BooleanVar(value=saved.get("use_min_duration", True))
        self.workflow_mode = tk.StringVar(value=saved.get("workflow_mode", "normal"))
        self.loop_mode = tk.BooleanVar(value=saved.get("loop_mode", False))

        # Include folder filter: list of subfolder names, or None = process all
        self._include_folders: Optional[List[str]] = saved.get("include_folders", None)

        self.total_files = 0
        self.done_files = 0

        self._build_ui()
        self._apply_workflow_mode()  # set initial widget enable/disable states
        self._tick()

    def _save_config(self):
        save_config({
            "input_dir": self.input_dir.get(),
            "output_dir": self.output_dir.get(),
            "base_speed": self.base_speed.get(),
            "duration": self.duration.get(),
            "encoder": self.encoder.get(),
            "preset": self.preset.get(),
            "cq": self.cq.get(),
            "out_fps": self.out_fps.get(),
            "recurse": self.recurse.get(),
            "hwaccel": self.hwaccel.get(),
            "overwrite": self.overwrite.get(),
            "random_duration": self.random_duration.get(),
            "use_min_duration": self.use_min_duration.get(),
            "workflow_mode": self.workflow_mode.get(),
            "loop_mode": self.loop_mode.get(),
            "include_folders": self._include_folders,
        })

    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}

        # ── Folder selection ──────────────────────────────────────────────────
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Input folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.input_dir, width=62).grid(row=0, column=1, sticky="we", padx=6)
        ttk.Button(top, text="Browse…", command=self._browse_input).grid(row=0, column=2)
        ttk.Button(top, text="Select Folders…", command=self._open_folder_select).grid(row=0, column=3, padx=4)

        ttk.Label(top, text="Output folder:").grid(row=1, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.output_dir, width=62).grid(row=1, column=1, sticky="we", padx=6)
        ttk.Button(top, text="Browse…", command=self._browse_output).grid(row=1, column=2)

        top.columnconfigure(1, weight=1)

        # ── Workflow Mode ─────────────────────────────────────────────────────
        wf_frame = ttk.LabelFrame(self, text="Workflow Mode")
        wf_frame.pack(fill="x", **pad)

        self._rb_normal = ttk.Radiobutton(
            wf_frame,
            text="Normal  — speed-up + trim to target duration  (for long source videos)",
            variable=self.workflow_mode, value="normal",
            command=self._apply_workflow_mode,
        )
        self._rb_normal.grid(row=0, column=0, columnspan=4, sticky="w", padx=8, pady=2)

        self._rb_short = ttk.Radiobutton(
            wf_frame,
            text="Short Clips  — speed only, no forced trim  (for 10–60 s source clips)",
            variable=self.workflow_mode, value="short_clips",
            command=self._apply_workflow_mode,
        )
        self._rb_short.grid(row=1, column=0, columnspan=4, sticky="w", padx=8, pady=2)

        # ── Encoding settings ─────────────────────────────────────────────────
        opts = ttk.LabelFrame(self, text="Settings")
        opts.pack(fill="x", **pad)

        ttk.Label(opts, text="Default speed (≤10 min):").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(opts, from_=1.0, to=10.0, increment=0.1, textvariable=self.base_speed, width=8)\
            .grid(row=0, column=1, sticky="w", padx=6)

        ttk.Label(opts, text="Output duration (s):").grid(row=0, column=2, sticky="w")
        self._spin_duration = ttk.Spinbox(
            opts, from_=5.0, to=90.0, increment=1.0, textvariable=self.duration, width=8)
        self._spin_duration.grid(row=0, column=3, sticky="w", padx=6)

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

        ttk.Label(
            opts,
            text=(
                f"Rule: if video > 10 min, speed auto-switches to {SPEED_LONG:.1f}×.\n"
                f"No-skip: if clip is too short after speed-up, bot tries lower speeds (…→3×→2×) and still renders."
            ),
        ).grid(row=3, column=0, columnspan=6, sticky="w", padx=2, pady=(4, 0))

        # ── Processing Options ────────────────────────────────────────────────
        po = ttk.LabelFrame(self, text="Processing Options")
        po.pack(fill="x", **pad)

        self._cb_random_dur = ttk.Checkbutton(
            po,
            text="Random output duration (20–30 s per file)",
            variable=self.random_duration,
            command=self._apply_workflow_mode,
        )
        self._cb_random_dur.grid(row=0, column=0, sticky="w", padx=8, pady=2)

        self._cb_min_dur = ttk.Checkbutton(
            po,
            text="Use minimum duration rule (speed fallback ladder)",
            variable=self.use_min_duration,
        )
        self._cb_min_dur.grid(row=0, column=1, sticky="w", padx=16, pady=2)

        ttk.Checkbutton(
            po, text="Loop mode (re-scan and process continuously)", variable=self.loop_mode,
        ).grid(row=1, column=0, sticky="w", padx=8, pady=2)

        self._lbl_folders = ttk.Label(po, text="", foreground="gray")
        self._lbl_folders.grid(row=1, column=1, sticky="w", padx=16)
        self._update_folder_label()

        # ── Controls ──────────────────────────────────────────────────────────
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", **pad)

        self.btn_start = ttk.Button(ctrl, text="▶  Start", command=self._start)
        self.btn_stop = ttk.Button(ctrl, text="■  Stop", command=self._stop, state="disabled")
        self.btn_pause = ttk.Button(ctrl, text="⏸  Pause", command=self._toggle_pause, state="disabled")
        self.btn_start.pack(side="left")
        self.btn_stop.pack(side="left", padx=4)
        self.btn_pause.pack(side="left")

        self.prog = ttk.Progressbar(ctrl, orient="horizontal", mode="determinate")
        self.prog.pack(side="left", fill="x", expand=True, padx=10)

        self.lbl = ttk.Label(ctrl, text="Idle")
        self.lbl.pack(side="right")

        # ── Log ───────────────────────────────────────────────────────────────
        logf = ttk.LabelFrame(self, text="Log")
        logf.pack(fill="both", expand=True, **pad)

        self.log = tk.Text(logf, height=12, wrap="word")
        sb_log = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb_log.set)
        self.log.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        sb_log.pack(side="right", fill="y", pady=8, padx=(0, 8))

    def _apply_workflow_mode(self):
        """Enable/disable widgets based on the current workflow mode and toggles."""
        mode = self.workflow_mode.get()
        if mode == "short_clips":
            # Short-clips forces random_duration and use_min_duration OFF
            self.random_duration.set(False)
            self.use_min_duration.set(False)
            self._cb_random_dur.config(state="disabled")
            self._cb_min_dur.config(state="disabled")
            self._spin_duration.config(state="disabled")
        else:
            self._cb_random_dur.config(state="normal")
            self._cb_min_dur.config(state="normal")
            # Duration spinbox is irrelevant when random_duration is on
            if self.random_duration.get():
                self._spin_duration.config(state="disabled")
            else:
                self._spin_duration.config(state="normal")

    def _update_folder_label(self):
        if self._include_folders is None:
            self._lbl_folders.config(text="Folders: all subfolders", foreground="gray")
        else:
            n = len(self._include_folders)
            if n == 0:
                self._lbl_folders.config(text="Folders: none selected — root files only", foreground="orange")
            else:
                self._lbl_folders.config(text=f"Folders: {n} selected", foreground="black")

    def _browse_input(self):
        d = filedialog.askdirectory(title="Select input folder")
        if d:
            self.input_dir.set(d)
            # Auto-suggest output folder on first selection
            if not self.output_dir.get():
                self.output_dir.set(str(Path(d) / "_SHORTS_OUT"))
            # Reset folder filter when input changes
            self._include_folders = None
            self._update_folder_label()

    def _browse_output(self):
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self.output_dir.set(d)

    def _open_folder_select(self):
        in_dir = Path(self.input_dir.get().strip() or "")
        if not in_dir.exists() or not in_dir.is_dir():
            messagebox.showerror("Error", "Please select a valid input folder first.")
            return
        dlg = FolderSelectDialog(self, in_dir, self._include_folders)
        self.wait_window(dlg)
        if dlg.result is not None:
            # If the user selected every subfolder, treat as "process all"
            all_subs = set(get_immediate_subfolders(in_dir))
            if all_subs and set(dlg.result) == all_subs:
                self._include_folders = None
            else:
                self._include_folders = dlg.result
            self._update_folder_label()

    def _append_log(self, s: str):
        self.log.insert("end", s + "\n")
        self.log.see("end")

    def _set_status(self, s: str):
        self.lbl.config(text=s)

    def _toggle_pause(self):
        if self._is_paused:
            self._is_paused = False
            self.pause_event.set()
            self.btn_pause.config(text="⏸  Pause")
            self._append_log("Resumed.")
        else:
            self._is_paused = True
            self.pause_event.clear()
            self.btn_pause.config(text="▶  Resume")
            self._append_log("Paused — will finish current file, then wait…")

    def _start(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Running", "Already running.")
            return

        in_dir = Path(self.input_dir.get().strip() or "")
        out_dir_str = self.output_dir.get().strip()
        out_dir = Path(out_dir_str) if out_dir_str else Path("")

        if not in_dir.exists() or not in_dir.is_dir():
            messagebox.showerror("Error", "Please select a valid input folder.")
            return

        if not out_dir_str:
            messagebox.showerror("Error", "Please specify an output folder.")
            return

        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Error", f"Cannot create output folder:\n{e}")
            return

        fps_txt = self.out_fps.get().strip()
        try:
            fps_val = int(fps_txt) if fps_txt else None
        except ValueError:
            messagebox.showerror("Error", "Force FPS must be an integer or blank.")
            return

        base_speed = float(self.base_speed.get())
        if base_speed <= 0:
            messagebox.showerror("Error", "Speed must be > 0.")
            return

        mode = self.workflow_mode.get()
        # Short-clips mode forces these off regardless of checkbox state
        use_rand_dur = self.random_duration.get() and mode == "normal"
        use_min_dur = self.use_min_duration.get() and mode != "short_clips"

        cfg = JobConfig(
            input_dir=in_dir,
            output_dir=out_dir,
            out_duration=float(self.duration.get()),
            encoder=self.encoder.get().strip(),
            preset=self.preset.get().strip(),
            cq=int(self.cq.get()),
            out_fps=fps_val,
            recurse=bool(self.recurse.get()),
            use_hwaccel=bool(self.hwaccel.get()),
            overwrite=bool(self.overwrite.get()),
            random_duration=use_rand_dur,
            use_min_duration=use_min_dur,
            workflow_mode=mode,
            loop_mode=bool(self.loop_mode.get()),
            include_folders=self._include_folders,
        )

        self.stop_event.clear()
        self.pause_event.set()
        self._is_paused = False

        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_pause.config(state="normal", text="⏸  Pause")
        self.prog["value"] = 0

        self._append_log(f"FFmpeg:  {which_ffmpeg()}")
        self._append_log(f"FFprobe: {which_ffprobe()}")
        self._append_log(f"Input:   {cfg.input_dir}")
        self._append_log(f"Output:  {cfg.output_dir}")
        self._append_log(
            f"Mode: {cfg.workflow_mode}  speed(≤10m)={base_speed:.1f}×  speed(>10m)={SPEED_LONG:.1f}×  "
            f"duration={cfg.out_duration}s  rand_dur={cfg.random_duration}  "
            f"min_dur={cfg.use_min_duration}  loop={cfg.loop_mode}  "
            f"hwaccel={cfg.use_hwaccel}  overwrite={cfg.overwrite}"
        )
        self._append_log("----")

        self._save_config()

        self.worker_thread = threading.Thread(target=self._worker, args=(cfg, base_speed), daemon=True)
        self.worker_thread.start()

    def _stop(self):
        self.stop_event.set()
        # Unblock pause so the worker can see stop_event
        self.pause_event.set()
        self._is_paused = False
        self._append_log("Stop requested… (finishing current file / terminating FFmpeg)")
        self.btn_stop.config(state="disabled")
        self.btn_pause.config(state="disabled")

    def _worker(self, cfg: JobConfig, base_speed: float):
        """
        Main processing loop.  Supports loop mode, include/exclude folders,
        pause/resume, random duration, and short-clips mode.
        All core speed logic is preserved unchanged from v7.
        """
        ffmpeg_fail_hwaccel = 0
        loop_count = 0

        while True:
            loop_count += 1
            if cfg.loop_mode and loop_count > 1:
                self.msg_q.put(("log", f"── Loop iteration {loop_count} ──"))

            videos = list_videos(
                cfg.input_dir,
                cfg.recurse,
                include_folders=cfg.include_folders,
            )

            # In loop mode without overwrite: skip already-produced files
            if cfg.loop_mode and not cfg.overwrite:
                videos = [
                    v for v in videos
                    if not (cfg.output_dir / safe_out_name(v)).exists()
                ]
                if not videos:
                    self.msg_q.put(("log", f"Loop: no new files found. Waiting {LOOP_EMPTY_WAIT_SEC} s before next scan…"))
                    iterations = int(LOOP_EMPTY_WAIT_SEC / 0.1)
                    for _ in range(iterations):
                        if self.stop_event.is_set():
                            break
                        time.sleep(0.1)
                    if self.stop_event.is_set():
                        break
                    continue

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

            for idx, src in enumerate(videos, start=1):
                if self.stop_event.is_set():
                    break

                # ── Pause support: blocks between files, never mid-encode ──
                if not self.pause_event.is_set():
                    self.msg_q.put(("status", "Paused"))
                    self.pause_event.wait()
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

                # ── Probe source ───────────────────────────────────────────
                src_duration = probe_duration_seconds(src)
                src_codec = probe_video_codec(src)

                if src_codec and src_codec in HEVC_LIKE_CODECS:
                    self.msg_q.put(("log", f"  Codec: {src_codec} — using software decode for reliability"))

                # ── Random duration (normal mode only) ────────────────────
                if cfg.random_duration and cfg.workflow_mode == "normal":
                    target_dur = random.uniform(RANDOM_DURATION_MIN, RANDOM_DURATION_MAX)
                    self.msg_q.put(("log", f"  Random duration: {target_dur:.1f} s"))
                else:
                    target_dur = cfg.out_duration

                # ── Speed selection ────────────────────────────────────────
                if src_duration is None:
                    # Cannot probe duration — do not skip, render at base speed
                    initial_speed = base_speed
                    speed_for_file = initial_speed
                    trials: List[Tuple[float, float]] = []
                    self.msg_q.put((
                        "log",
                        f"  WARN: Cannot read duration via ffprobe. "
                        f"Rendering anyway at {speed_for_file:.1f}× (no-skip).",
                    ))
                    log_event({
                        "file": str(src),
                        "status": "duration_unreadable_render_anyway",
                        "speed": speed_for_file,
                    })
                else:
                    # Apply client speed rule + no-skip fallback ladder (PRESERVED)
                    initial_speed = choose_initial_speed(src_duration, base_speed)
                    speed_for_file, trials = pick_speed_no_skip(
                        src_duration, initial_speed, cfg.use_min_duration
                    )

                    rule_tag = "≤10m" if src_duration <= LENGTH_THRESHOLD_SEC else ">10m"
                    self.msg_q.put((
                        "log",
                        f"  Rule: duration={src_duration / 60:.2f} min ({rule_tag})"
                        f" → initial speed={initial_speed:.1f}×",
                    ))

                    if trials and cfg.use_min_duration:
                        qualifies = any(eff >= MIN_EFFECTIVE_DURATION for _, eff in trials)
                        if qualifies:
                            for s, eff in trials:
                                if eff >= MIN_EFFECTIVE_DURATION:
                                    self.msg_q.put((
                                        "log",
                                        f"  Length check: {s:.1f}× gives {eff:.2f}s"
                                        f" (≥ {MIN_EFFECTIVE_DURATION:.0f}s) → using {s:.1f}×",
                                    ))
                                    break
                        else:
                            last_s, last_eff = trials[-1]
                            self.msg_q.put((
                                "log",
                                f"  Length check: even at {last_s:.1f}× effective={last_eff:.2f}s"
                                f" (< {MIN_EFFECTIVE_DURATION:.0f}s). Rendering anyway (no-skip) at {last_s:.1f}×.",
                            ))

                # ── Build effective config with resolved duration ───────────
                eff_cfg = JobConfig(**{
                    **cfg.__dict__,
                    "out_duration": target_dur,
                })

                # ── Build FFmpeg command ────────────────────────────────────
                try:
                    cmd = build_ffmpeg_cmd(eff_cfg, src, dst_tmp, speed_for_file, src_codec=src_codec)
                except Exception as e:
                    self.msg_q.put(("log", f"  ERROR building cmd: {e}"))
                    log_event({"file": str(src), "status": "cmd_error", "error": str(e)})
                    self.msg_q.put(("done", 1))
                    continue

                started = time.time()
                rc, prog = run_ffmpeg_with_progress(cmd, self.stop_event)

                # ── HWACCEL fallback (if hwaccel was used and failed) ──────
                if rc != 0 and eff_cfg.use_hwaccel:
                    ffmpeg_fail_hwaccel += 1
                    self.msg_q.put(("log", "  HWACCEL failed; retrying without -hwaccel cuda…"))
                    # Remove any partial file left by the failed HWACCEL run so
                    # the software-decode retry always starts with a clean slate.
                    try:
                        if dst_tmp.exists():
                            dst_tmp.unlink()
                    except Exception:
                        pass
                    cfg_sw = JobConfig(**{**eff_cfg.__dict__, "use_hwaccel": False})
                    cmd2 = build_ffmpeg_cmd(cfg_sw, src, dst_tmp, speed_for_file, src_codec=src_codec)
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
                    # IMPORTANT: do NOT skip outputs shorter than MIN_EFFECTIVE_DURATION.
                    # Short sources must still produce an output (preserved from v7).
                    try:
                        if dst.exists() and cfg.overwrite:
                            dst.unlink()
                        dst_tmp.replace(dst)

                        # Always measure the REAL output duration via ffprobe so that
                        # the log reflects actual content, not the requested -t value.
                        out_dur = probe_duration_seconds(dst)

                        # ── Validate output duration ───────────────────────
                        # Compute the maximum sensible output we could get:
                        # whichever is smaller — the requested target or the
                        # full source played at the chosen speed.
                        if src_duration is not None:
                            expected_out = min(target_dur, src_duration / speed_for_file)
                        else:
                            expected_out = target_dur

                        # In normal mode, require at least OUTPUT_DURATION_MIN_RATIO
                        # of expected_out (with an absolute floor).  This catches
                        # broken outputs (e.g. 4 s instead of ~60 s) that FFmpeg
                        # can silently produce when the filter chain misbehaves.
                        duration_ok = True
                        min_acceptable = max(
                            expected_out * OUTPUT_DURATION_MIN_RATIO,
                            OUTPUT_DURATION_FLOOR_SEC,
                        )
                        if cfg.workflow_mode == "normal" and out_dur is not None:
                            if out_dur < min_acceptable:
                                duration_ok = False

                        if duration_ok:
                            if out_dur is not None:
                                dur_str = (
                                    f"  expected={expected_out:.2f}s "
                                    f" actual={out_dur:.2f}s"
                                )
                            else:
                                dur_str = f"  expected={expected_out:.2f}s"
                            self.msg_q.put(("log", f"  OK → {dst.name}  ({elapsed:.1f}s){dur_str}"))
                            log_event({
                                "file": str(src),
                                "status": "ok",
                                "elapsed_sec": elapsed,
                                "speed": speed_for_file,
                                "src_duration": src_duration,
                                "expected_output_duration": expected_out,
                                "trials": trials,
                                "output_duration": out_dur,
                                "progress": json.loads(prog) if prog else {},
                            })
                        else:
                            # Output is too short — broken file, remove it
                            fail_msg = (
                                f"  FAIL: output too short "
                                f"(actual={out_dur:.2f}s, "
                                f"expected≥{min_acceptable:.1f}s)  ({elapsed:.1f}s)"
                            )
                            self.msg_q.put(("log", fail_msg))
                            try:
                                if dst.exists():
                                    dst.unlink()
                            except Exception:
                                pass
                            log_event({
                                "file": str(src),
                                "status": "fail_short_output",
                                "elapsed_sec": elapsed,
                                "speed": speed_for_file,
                                "src_duration": src_duration,
                                "expected_output_duration": expected_out,
                                "output_duration": out_dur,
                                "expected_min": min_acceptable,
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
                    log_event({
                        "file": str(src), "status": "fail", "rc": rc,
                        "elapsed_sec": elapsed, "speed": speed_for_file, "progress": prog,
                    })

                self.msg_q.put(("done", 1))

            if log_fp:
                log_fp.close()

            if self.stop_event.is_set() or not cfg.loop_mode:
                break

            # Brief sleep between loop iterations (responsive to stop)
            self.msg_q.put(("log", f"── Loop batch done. Rescanning in {LOOP_RESCAN_WAIT_SEC} s… ──"))
            iterations = int(LOOP_RESCAN_WAIT_SEC / 0.1)
            for _ in range(iterations):
                if self.stop_event.is_set():
                    break
                time.sleep(0.1)
            if self.stop_event.is_set():
                break

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
                elif typ == "status":
                    self._set_status(str(payload))
                elif typ == "finished":
                    info = payload if isinstance(payload, dict) else {}
                    self._append_log("----")
                    self._append_log(f"Finished. Done: {self.done_files}/{self.total_files}.")
                    if info.get("hwaccel_failures"):
                        self._append_log(
                            f"Note: {info['hwaccel_failures']} file(s) failed CUDA decode "
                            f"and were retried in software."
                        )
                    self.btn_start.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self.btn_pause.config(state="disabled")
                    self._is_paused = False
                    self.btn_pause.config(text="⏸  Pause")
                    self._set_status("Idle")
        except queue.Empty:
            pass

        self.after(200, self._tick)


def main():
    app = ShortBotApp()
    app.mainloop()


if __name__ == "__main__":
    main()
