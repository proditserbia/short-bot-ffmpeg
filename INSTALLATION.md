# Short Bot v8 — Installation & Setup Guide

Complete instructions for setting up and running Short Bot on Windows (including RDP environments).

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Install Python](#2-install-python)
3. [Get the Code](#3-get-the-code)
4. [Install FFmpeg](#4-install-ffmpeg)
5. [Verify the Setup](#5-verify-the-setup)
6. [Launch the Bot](#6-launch-the-bot)
7. [Configure Input & Output Folders](#7-configure-input--output-folders)
8. [Using the Workflow Modes](#8-using-the-workflow-modes)
9. [New Toggles and Controls (v8)](#9-new-toggles-and-controls-v8)
10. [Building a Standalone .exe (Optional)](#10-building-a-standalone-exe-optional)
11. [RDP / Server-Specific Notes](#11-rdp--server-specific-notes)
12. [Common Troubleshooting](#12-common-troubleshooting)

---

## 1. Prerequisites

| Requirement | Minimum Version | Notes |
|---|---|---|
| Windows | 10 / 11 or Windows Server 2016+ | RDP/VPS fully supported |
| Python | **3.9 or newer** | 3.11+ recommended |
| FFmpeg | 5.x or 6.x | Must include NVENC support |
| NVIDIA GPU | Any with NVENC | Required for GPU-accelerated encoding |
| NVIDIA driver | 471.11+ | For NVENC on CUDA; keep up to date |

> **No third-party Python packages** are required. The bot uses only the Python standard library.

---

## 2. Install Python

1. Go to [https://www.python.org/downloads/](https://www.python.org/downloads/)
2. Download the latest **Python 3.11** (or 3.9+) Windows installer.
3. Run the installer. **Important:** tick both:
   - ✅ **Add Python to PATH**
   - ✅ **Install Tcl/Tk and IDLE** (this provides `tkinter`, needed for the GUI)
4. Click "Install Now" and wait for completion.
5. Verify in a command prompt:
   ```
   python --version
   ```
   You should see `Python 3.11.x` (or whichever version you installed).

---

## 3. Get the Code

### Option A — Download ZIP (easiest)

1. Go to [https://github.com/proditserbia/short-bot-ffmpeg](https://github.com/proditserbia/short-bot-ffmpeg)
2. Click **Code → Download ZIP**
3. Extract the ZIP to a convenient folder, e.g. `C:\Tools\ShortBot\`

### Option B — Git clone

```cmd
git clone https://github.com/proditserbia/short-bot-ffmpeg.git
cd short-bot-ffmpeg
```

---

## 4. Install FFmpeg

FFmpeg is **not included** in the repository. You must obtain it separately.

### Windows (recommended source: gyan.dev)

1. Go to [https://www.gyan.dev/ffmpeg/builds/](https://www.gyan.dev/ffmpeg/builds/)
2. Download the latest **release full build** (e.g. `ffmpeg-release-full.7z`)
3. Extract the archive.
4. Inside the extracted folder, find `bin\ffmpeg.exe` and `bin\ffprobe.exe`.

### Place the binaries

Put `ffmpeg.exe` and `ffprobe.exe` in **one of these locations** (the bot checks in this order):

1. **Same folder as the script** — e.g. `C:\Tools\ShortBot\ffmpeg.exe` *(recommended)*
2. **`tools\` subfolder** — e.g. `C:\Tools\ShortBot\tools\ffmpeg.exe`
3. **Anywhere on your system PATH** — e.g. `C:\ffmpeg\bin\` added to Windows PATH

> The `tools\` folder already exists in the repository (it contains placeholder files). Simply replace the `.placeholder` files with the real binaries.

---

## 5. Verify the Setup

Open a command prompt in the bot's folder and run:

```cmd
python --version
python -c "import tkinter; print('tkinter OK')"
ffmpeg -version
ffprobe -version
```

All four commands should succeed without errors. If `ffmpeg` is not on PATH, test it with the full path:

```cmd
C:\Tools\ShortBot\ffmpeg.exe -version
```

---

## 6. Launch the Bot

In the folder containing `ShortVideoProcessingBot-v7.py`, run:

```cmd
python ShortVideoProcessingBot-v7.py
```

Or if you want to open a new command prompt in that folder:

```cmd
cd C:\Tools\ShortBot
python ShortVideoProcessingBot-v7.py
```

The GUI window will open. Settings are automatically saved to `ShortVideoProcessingBot-v7.json` in the same folder so they persist between sessions.

---

## 7. Configure Input & Output Folders

### Input Folder

1. Click **Browse…** next to "Input folder".
2. Select the folder containing your source video files.
3. The output folder is auto-suggested as `<input>/_SHORTS_OUT` on first selection.

### Output Folder

- The output folder is set automatically when you browse for input.
- You can change it at any time by clicking **Browse…** next to "Output folder".
- Supports any local path — e.g. `C:\Outputs`, `D:\VideoWork\Shorts`, `E:\MyServer\Out`.
- If the selected path does not exist, the bot will create it automatically when you click Start.

### Subfolder Filter (Include/Exclude)

Click **Select Folders…** to choose which immediate subfolders of the input folder are processed:

- All subfolders are checked by default (= process everything).
- Uncheck any subfolder to skip it.
- Click **OK** to confirm. The label next to "Loop mode" shows how many folders are selected.
- Click **Select All / Deselect All** for quick selection.

---

## 8. Using the Workflow Modes

Select the mode from the **Workflow Mode** panel at the top.

### Normal (long videos)

Use this for source videos that are **longer than ~60 seconds** and need to be condensed.

- Automatic speed selection: ≤ 10 min → base speed (default 4×); > 10 min → 5×.
- No-skip guarantee: if a clip is too short after speed-up, the bot tries lower speeds (→ 3× → 2×) and renders anyway.
- Optional: enable **Random output duration** to produce clips of varying length (20–30 s each).
- Optional: enable **Use minimum duration rule** to trigger the speed fallback ladder.
- The **Output duration** spinbox sets the fixed target duration (used when random is off).

### Short Clips (10–60 s sources)

Use this for source videos that are **already short** (10 to ~60 seconds).

- Random duration and minimum duration rule are **automatically disabled** in this mode.
- The bot applies the speed multiplier from the "Default speed" spinbox directly.
- **No forced trim** is applied — the output duration reflects the natural result of the speed-up.
- Ideal for social media clips that just need a light speed boost without artificial cutting.

---

## 9. New Toggles and Controls (v8)

### Random Output Duration

- **Location:** Processing Options panel, left checkbox.
- **Effect:** Each file gets a randomly chosen target duration between **20 s and 30 s**.
- **When to use:** Normal mode with varied-length outputs for a more organic look.
- **Note:** Grayed out in Short Clips mode.

### Use Minimum Duration Rule

- **Location:** Processing Options panel, right checkbox.
- **Effect:** Enables the speed fallback ladder — if the output would be shorter than 50 s, the bot tries progressively lower speeds.
- **When off:** The initial speed is used directly with no ladder fallback.
- **Note:** Grayed out in Short Clips mode.

### Loop Mode

- **Location:** Processing Options panel, bottom-left checkbox.
- **Effect:** After processing all eligible files, the bot rescans and processes new/unprocessed files again, continuously.
- **Stopping:** Click **■ Stop** to halt the loop after the current file finishes.
- **With Overwrite OFF:** Only newly added files (no output yet) are processed each iteration. The bot sleeps 10 s between scans when nothing is found.
- **With Overwrite ON:** All files are reprocessed every iteration.

### Pause / Resume

- **Button:** ⏸ Pause (next to Stop).
- **Effect:** After the current file finishes encoding, processing pauses. Click **▶ Resume** to continue.
- **Safe:** The current FFmpeg encode is never interrupted by pause — only the next file is held.

### Select Folders…

- **Button:** Next to the Input folder Browse button.
- **Effect:** Opens the folder selection dialog (see §7 above).

---

## 10. Building a Standalone .exe (Optional)

If you want to distribute the bot as a single `.exe`:

1. Install PyInstaller:
   ```cmd
   pip install pyinstaller
   ```
2. Run from the bot's folder:
   ```cmd
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
3. The resulting `ShortBot.exe` will be in the `dist\` folder.
4. Place `ffmpeg.exe` and `ffprobe.exe` alongside `ShortBot.exe`.

---

## 11. RDP / Server-Specific Notes

### NVIDIA GPU on RDP

- NVENC is supported on all NVIDIA GPUs over RDP as long as the driver is up to date (471.11+).
- If you see NVENC errors, update the NVIDIA driver on the **host machine** (not the RDP client).
- On Windows Server, you may need to install the **NVIDIA Data Center GPU Manager** or a standard GRID driver depending on your VM/VPS provider.

### Display / GUI on RDP

- The Tkinter GUI works fully over RDP.
- If you see a blank window, try switching the RDP color depth to **32-bit**.
- On headless servers without a GPU-attached display, use the **-hwaccel cuda** option with caution — CUDA decode may require an attached display or CUDA context. If it fails, uncheck **Use CUDA hwaccel decode** in the GUI.

### Running Unattended

- Enable **Loop Mode** and set **Overwrite** to OFF.
- Start the bot, then minimize the RDP window. The bot continues running.
- The GUI does not need to be visible for processing to continue.
- Check the output folder's `_shortbot_run_*.jsonl` log files for progress.

### Firewall / Antivirus

- The bot makes no network connections. If your AV flags `ffmpeg.exe`, add it to the exceptions list.
- PyInstaller-built `.exe` files are sometimes flagged as false positives. Running from source (`python ShortVideoProcessingBot-v7.py`) avoids this.

---

## 12. Common Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `ffmpeg not found` | FFmpeg binary missing or not in PATH | Place `ffmpeg.exe` next to the script or in `tools\` |
| `FAIL (rc=1)` with NVENC error | No NVIDIA GPU or outdated driver | Update NVIDIA driver; ensure NVENC support |
| `FAIL (rc=1)` on HEVC files | CUDA decode incompatibility | Uncheck "Use CUDA hwaccel decode" — the bot retries in software automatically |
| All HEVC files show "using software decode" | Expected — HEVC codec detected | This is normal; HEVC sources always use software decode for reliability |
| GUI window opens then closes immediately | tkinter not installed | Reinstall Python and tick "Tcl/Tk" option |
| `ModuleNotFoundError: No module named 'tkinter'` | tkinter missing | On Windows: reinstall Python with tkinter. On Linux: `sudo apt install python3-tk` |
| Output files have no audio | Source has no audio stream | Expected; audio mapping is optional (`0:a:0?`) |
| Output is very short | Source clip is very short | Bot renders anyway (no-skip behavior preserved) |
| "Loop mode" processes same files repeatedly | Overwrite is ON | Switch Overwrite to OFF if you don't want reprocessing |
| Progress bar doesn't advance in loop mode | Total resets each loop iteration | Each loop iteration restarts the counter — this is expected |
| Config not saved between sessions | No write permission to script folder | Move the script to a folder where your user has write access |
| `Select Folders…` shows no subfolders | Input folder has no subdirectories | The bot will process files directly in the root folder |

---

## Encoder Reference

| Setting | Default | Meaning |
|---|---|---|
| Encoder | `h264_nvenc` | Use `hevc_nvenc` for HEVC/H.265 output |
| NVENC preset | `p5` | p1=fastest/lowest quality, p7=slowest/best quality |
| CQ | `23` | Quality: lower=better (larger file); range 15–35 |
| Force FPS | *(blank)* | Leave blank to keep source frame rate |

---

*For issues or questions, open an issue at [github.com/proditserbia/short-bot-ffmpeg](https://github.com/proditserbia/short-bot-ffmpeg).*
