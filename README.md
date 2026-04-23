# Short Bot — FFmpeg + CUDA/NVENC

A desktop GUI tool that batch-converts long source videos into short-form clips (≈60 s) by speeding them up, using NVIDIA NVENC hardware encoding via FFmpeg. Purpose-built for producing content in the style of YouTube Shorts, Instagram Reels, and TikTok.

---

## Table of Contents

1. [Project Overview](#project-overview)
2. [Main Features](#main-features)
3. [How It Works](#how-it-works)
4. [Repository Structure](#repository-structure)
5. [Requirements & Dependencies](#requirements--dependencies)
6. [Installation](#installation)
7. [Configuration](#configuration)
8. [How to Run](#how-to-run)
9. [Processing Pipeline](#processing-pipeline)
10. [Output Description](#output-description)
11. [Logging & Troubleshooting](#logging--troubleshooting)
12. [Known Limitations](#known-limitations)
13. [Notes for Future Improvements](#notes-for-future-improvements)
14. [Assumptions & Unclear Points](#assumptions--unclear-points)

---

## Project Overview

**Short Bot** takes a folder of source videos, speeds each one up by a configurable multiplier (default 4×), and cuts the result to a target duration (default 60 s). It runs entirely on the local machine using FFmpeg and exposes all key settings through a minimal Tkinter GUI. No cloud service, no subscription — all processing is local.

Encoding is delegated to NVIDIA NVENC (`h264_nvenc` or `hevc_nvenc`) for fast GPU-accelerated output. A software fallback is performed automatically when CUDA hardware decode fails for a given file.

---

## Main Features

- **Batch processing** — scans an entire folder tree (or flat folder) for video files and processes them one by one.
- **Configurable speed-up** — default 4× for clips ≤ 10 minutes, automatically 5× for clips > 10 minutes.
- **No-skip guarantee** — if a clip would be too short after speed-up, the bot tries progressively lower speeds (→ 3× → 2×) and renders regardless. No source file is ever silently skipped.
- **Target duration cut** — each output is trimmed to a user-defined length (default 60 s, range 5–90 s).
- **NVIDIA NVENC encoding** — GPU-accelerated H.264 or HEVC output with VBR quality control.
- **CUDA hwaccel decode** — optional hardware-accelerated decode; automatically retried without hwaccel if the first attempt fails.
- **Atomic output** — each file is written to a `.tmp.mp4` first and renamed on success, preventing corrupt partial outputs.
- **Overwrite control** — configurable; on by default.
- **Recursive subfolder scan** — on by default.
- **JSONL run log** — every batch writes a timestamped `.jsonl` log to the output folder.
- **Real-time GUI log** — all events (speeds chosen, fallback decisions, durations, errors) are shown in the GUI log pane.
- **Stop mid-batch** — a Stop button terminates the current FFmpeg process cleanly and removes the incomplete temp file.
- **Bundleable as standalone `.exe`** — PyInstaller build script included.

---

## How It Works

```
Source folder
    │
    ├─ video_A.mp4 (3 min)  →  speed-up 4×  →  cut to 60 s  →  video_A_short.mp4
    ├─ video_B.mkv (15 min) →  speed-up 5×  →  cut to 60 s  →  video_B_short.mp4
    └─ video_C.mp4 (8 s)    →  speed-up 4×  →  no cut needed →  video_C_short.mp4
                                              (too short even at 2×, rendered anyway)
```

**Speed selection logic:**
- Source ≤ 10 min → use "default speed" (UI spinbox, default 4.0×)
- Source > 10 min → use 5.0× (hardcoded)
- After choosing initial speed, check if `source_duration / speed >= 50 s`
  - If yes → use that speed
  - If no → try next lower speed in ladder (5→4→3→2) until threshold is met
  - If still no at 2× → render at 2× anyway (no-skip)

**Audio handling:**
FFmpeg's `atempo` filter only supports 0.5×–2.0× per stage. For speeds above 2×, the bot chains multiple `atempo=2.0` stages automatically. For example, 4× speed becomes `atempo=2.0,atempo=2.0`.

---

## Repository Structure

```
short-bot-ffmpeg/
├── ShortVideoProcessingBot-v7.py   # Main application (GUI + processing logic)
├── app.ico                         # Window icon (used by GUI and PyInstaller)
├── app.png                         # Application image asset
├── tray.png                        # Tray/toolbar image asset
├── build-full.txt                  # PyInstaller build command (references v5, see notes)
├── tools/
│   ├── ffmpeg.exe.placeholder      # Placeholder — replace with real ffmpeg.exe
│   └── ffprobe.exe.placeholder     # Placeholder — replace with real ffprobe.exe
└── README.md
```

> **Note:** The `tools/` folder contains placeholder files. The actual `ffmpeg.exe` and `ffprobe.exe` binaries must be provided by the user (see [Installation](#installation)).

---

## Requirements & Dependencies

### Python

- Python **3.9+** (uses `from __future__ import annotations`, dataclasses, `Path.rglob`)
- Standard library only — **no third-party Python packages required**
  - `tkinter` (included with standard CPython distributions)
  - `subprocess`, `threading`, `json`, `pathlib`, `dataclasses`, `queue`, `signal`, `time`

### System

| Dependency | Version | Notes |
|---|---|---|
| **FFmpeg** | 5.x or 6.x recommended | Must support `h264_nvenc` / `hevc_nvenc` |
| **FFprobe** | same build as FFmpeg | Used for duration probing |
| **NVIDIA GPU** | with NVENC support | Required for GPU encoding; CPU fallback is not implemented |
| **NVIDIA driver** | up-to-date | Must support CUDA and NVENC |

> FFmpeg and FFprobe are auto-detected: the script first looks for `ffmpeg.exe` / `ffprobe.exe` in the same directory as the script, then falls back to the system `PATH`.

### Supported Input Formats

`.mp4`, `.mov`, `.mkv`, `.mxf`, `.avi`, `.m4v`, `.webm`, `.mpg`, `.mpeg`, `.ts`, `.m2ts`, `.mts`

---

## Installation

### Running from source

1. **Install Python 3.9+**  
   Download from [python.org](https://www.python.org/). Ensure `tkinter` is included (it is by default on Windows; on Linux install `python3-tk`).

2. **Obtain FFmpeg + FFprobe**  
   Download a pre-built binary (e.g., from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) on Windows or via your package manager on Linux/macOS).  
   Place `ffmpeg.exe` (or `ffmpeg`) and `ffprobe.exe` (or `ffprobe`) either:
   - In the **same folder** as `ShortVideoProcessingBot-v7.py`, **or**
   - Anywhere on your system `PATH`.

3. **Clone the repository**

   ```bash
   git clone https://github.com/proditserbia/short-bot-ffmpeg.git
   cd short-bot-ffmpeg
   ```

4. **No additional pip installs are required.**

### Building a standalone `.exe` (Windows)

The repository includes `build-full.txt` with a PyInstaller command.

> **Note:** `build-full.txt` currently references `ShortVideoProcessingBot-v5.py`. Update the filename to `ShortVideoProcessingBot-v7.py` before running.

```bat
pip install pyinstaller
pyinstaller ^
  --onefile ^
  --noconsole ^
  --windowed ^
  --name ShortBot ^
  --icon app.ico ^
  --add-data "app.ico;." ^
  --add-data "app.png;." ^
  ShortVideoProcessingBot-v7.py
```

The resulting `ShortBot.exe` will be in the `dist/` folder. Place `ffmpeg.exe` and `ffprobe.exe` alongside the `.exe`.

---

## Configuration

All settings are available in the GUI. The following table lists every option, its default value, and its meaning.

| Setting | Default | Range / Options | Description |
|---|---|---|---|
| **Input folder** | — | any path | Folder containing source videos |
| **Output folder** | `<input>/_SHORTS_OUT` | any path | Destination for processed clips (auto-suggested) |
| **Default speed (≤10 min)** | `4.0` | 1.0 – 10.0 (step 0.1) | Speed multiplier for clips up to 10 minutes |
| **Output duration (s)** | `60.0` | 5 – 90 | Maximum length of each output clip in seconds |
| **Encoder** | `h264_nvenc` | `h264_nvenc`, `hevc_nvenc` | NVENC codec to use |
| **NVENC preset** | `p5` | `p1` – `p7` | Encoding speed/quality preset (p1=fastest, p7=slowest/best) |
| **CQ** | `23` | 15 – 35 | Constant quality value (lower = higher quality, larger file) |
| **Force FPS** | *(empty)* | integer or blank | Optional: force output frame rate; blank = keep source cadence |
| **Recurse subfolders** | ON | checkbox | Scan subdirectories for videos |
| **Use CUDA hwaccel decode** | ON | checkbox | Enable CUDA hardware-accelerated decoding |
| **Overwrite outputs** | ON | checkbox | Overwrite existing `_short.mp4` files |

**Fixed encoding parameters (not exposed in UI):**

| Parameter | Value |
|---|---|
| Rate control | VBR |
| Max bitrate | 25 Mbps |
| Buffer size | 50 Mbps |
| Audio codec | AAC |
| Audio bitrate | 160 kbps |
| Audio channels | Stereo (2) |
| Audio sample rate | 48 000 Hz |
| Pixel format | `yuv420p` |
| MP4 faststart | enabled (`+faststart`) |

---

## How to Run

```bash
python ShortVideoProcessingBot-v7.py
```

Or double-click the built `ShortBot.exe`.

**Typical workflow:**

1. Launch the application.
2. Click **Browse…** next to "Input folder" and select the folder containing your source videos.  
   The output folder is automatically set to `<input>/_SHORTS_OUT`.
3. Adjust settings if needed (speed, duration, encoder, etc.).
4. Click **Start**.
5. Monitor progress in the log pane and the progress bar.
6. Click **Stop** at any time to abort after the current file finishes (or terminates FFmpeg immediately).
7. Processed files appear in the output folder. A JSONL log is also written there.

---

## Processing Pipeline

For each source video file, the bot performs the following steps:

```
1. Enumerate videos
   └─ Recursively (or flat) scan input folder for supported extensions

2. Check overwrite
   └─ If <name>_short.mp4 already exists and Overwrite=OFF → skip

3. Probe duration
   └─ ffprobe reads the container duration in seconds
   └─ If probe fails → use base_speed, render anyway (no-skip)

4. Choose speed
   ├─ src_duration ≤ 10 min → initial_speed = base_speed (UI)
   └─ src_duration > 10 min → initial_speed = 5.0

5. No-skip speed ladder
   └─ Try speeds in ladder (e.g. 4→3→2) until (src_duration / speed) ≥ 50 s
   └─ If none qualify → use lowest speed and render anyway

6. Build FFmpeg command
   ├─ Input: -hwaccel cuda -hwaccel_output_format cuda  (if hwaccel ON)
   ├─ Video filter: setpts=PTS/<speed>  [,fps=<out_fps>]
   ├─ Audio filter: chained atempo stages (e.g. atempo=2.0,atempo=2.0 for 4×)
   ├─ Duration cut: -t <out_duration>
   ├─ Video encoder: h264_nvenc / hevc_nvenc, VBR, CQ, maxrate, bufsize
   ├─ Audio encoder: aac, 160k, stereo, 48kHz
   └─ Output: <name>_short.tmp.mp4

7. Run FFmpeg
   └─ Progress lines parsed from stdout (-progress pipe:1)
   └─ stderr drained in a background thread (last 200 lines kept)

8. CUDA fallback
   └─ If FFmpeg exits non-zero and hwaccel was ON → retry without -hwaccel cuda

9. Finalize
   ├─ rc=0 and tmp file exists → rename tmp → <name>_short.mp4
   └─ rc≠0 → delete tmp, log failure

10. Log event
    └─ Write JSON record to _shortbot_run_<timestamp>.jsonl
```

---

## Output Description

| File | Location | Description |
|---|---|---|
| `<stem>_short.mp4` | output folder | Processed clip: sped up, cut to target duration, NVENC-encoded |
| `<stem>_short.tmp.mp4` | output folder | Temporary file during encoding; deleted on success or failure |
| `_shortbot_run_YYYYMMDD_HHMMSS.jsonl` | output folder | Per-file JSON log for the entire batch run |

**Output naming:** The output filename is always `<original_stem>_short.mp4` regardless of the source extension.

**JSONL log fields** (one JSON object per line):

```json
{
  "file": "/path/to/source.mp4",
  "status": "ok",                   // ok | fail | cmd_error | finalize_error | duration_unreadable_render_anyway
  "elapsed_sec": 4.37,
  "speed": 4.0,
  "src_duration": 240.5,            // seconds (null if probe failed)
  "trials": [[4.0, 60.12]],         // [[speed, effective_duration_sec], ...]
  "output_duration": 60.0,          // seconds (probed from output file)
  "progress": { ... }               // last FFmpeg -progress key-value pairs
}
```

---

## Logging & Troubleshooting

**GUI log pane** — all activity is printed in real time:
- Which speed was chosen and why (including fallback ladder decisions)
- FFmpeg path resolved
- Output duration as probed after encoding
- CUDA hwaccel failures and retries
- Errors building the command or finalizing the output

**JSONL log file** — written to the output folder at the end of every batch. Useful for post-processing, auditing, or debugging.

**Common issues:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `ffmpeg not found` | FFmpeg binary not found | Place `ffmpeg.exe`/`ffmpeg` next to the script or add to PATH |
| `FAIL (rc=1)` with NVENC error | No compatible NVIDIA GPU / driver | Update driver; ensure NVENC support |
| All files show CUDA fallback retry | CUDA hwaccel decode not supported | Uncheck "Use CUDA hwaccel decode" in UI |
| Output file has no audio | Source has no audio stream | Expected; audio mapping is optional (`0:a:0?`) |
| Output is very short | Source clip is very short (e.g., < 10 s) | Bot renders anyway; the output will reflect the actual short duration |

---

## Known Limitations

- **NVIDIA GPU required** — both CUDA decode and NVENC encode require an NVIDIA GPU. There is no software (CPU) encoding path in the UI.
- **No resolution normalization** — aspect ratio, resolution, and orientation are preserved as-is from the source. No cropping to 9:16 for vertical shorts is performed.
- **No intro/outro merging** — the bot does not concatenate intro or outro clips.
- **No subtitle/caption support** — no subtitle burning or overlay.
- **No background music** — no audio mixing with external music tracks.
- **No metadata generation** — no title, description, or platform-specific metadata is written.
- **No profanity filtering** — no audio censorship.
- **Single audio stream** — only the first audio stream (`0:a:0`) is included; multi-audio sources are not handled.
- **Output always `.mp4`** — regardless of source format.
- **`build-full.txt` references v5** — the PyInstaller build command references `ShortVideoProcessingBot-v5.py`; must be updated manually for v7.
- **`use_hwaccel` is hardcoded to `False` in `_start()`** — despite the "Use CUDA hwaccel decode" checkbox existing and defaulting to ON, the `JobConfig` is constructed with `use_hwaccel=False` unconditionally. The checkbox has no effect at runtime. *(Confirmed by inspecting line 546.)*

---

## Notes for Future Improvements

- Fix the `use_hwaccel` bug: pass `bool(self.hwaccel.get())` instead of `False` in `_start()`.
- Update `build-full.txt` to reference `v7`.
- Add a software (CPU libx264/libx265) encoder fallback for machines without NVIDIA GPUs.
- Add optional resolution/aspect-ratio normalization (e.g., auto-crop/pad to 1080×1920 for vertical Shorts).
- Add optional intro/outro concatenation via `ffmpeg concat` demuxer.
- Add background music mixing (`amix` filter).
- Expose `maxrate` and `bufsize` in the UI.
- Add a "dry run" mode (the `dry_run` field exists on `JobConfig` but is never wired into the processing logic).
- Consider a CLI mode for headless/server usage (the GUI dependency could be made optional).

---

## Assumptions & Unclear Points

The following points should be confirmed by the repository owner:

1. **`use_hwaccel` hardcoded to `False`** — Line 546 reads `use_hwaccel=False` regardless of the checkbox value. This appears to be a bug introduced during development. *Needs confirmation: was hwaccel intentionally disabled for stability reasons?*
2. **`build-full.txt` filename mismatch** — References `ShortVideoProcessingBot-v5.py`; likely a copy-paste oversight from a previous version.
3. **`dry_run` field** — Defined in `JobConfig` but never read or acted upon anywhere in the code. *Needs confirmation: was this planned but not implemented?*
4. **`tools/` folder** — Contains only placeholder files; the actual FFmpeg binaries are not distributed. *Is the intent to bundle them in a release? The script already looks for binaries next to itself.*
5. **Target audience** — The script does not enforce 9:16 aspect ratio or any specific resolution, so it can produce clips for any platform format, not just vertical Shorts. *Inferred from code: the "Shorts" in the name refers to duration, not necessarily vertical format.*
6. **Minimum effective duration threshold (50 s)** — Hardcoded as `MIN_EFFECTIVE_DURATION = 50.0`. This means a clip that speeds up to, say, 45 s will still be rendered, but the threshold governs which speed level is preferred. *May need adjustment depending on target platform minimum duration requirements.*