# Video Cropper

A small desktop tool for **cropping** and **trimming** video clips. Drag a crop
box on the preview, set In/Out points to cut the duration, and export an MP4 —
with a live preview, real progress, and a modern dark UI.

![version](https://img.shields.io/badge/version-1.0.0-informational) ![status](https://img.shields.io/badge/platform-Windows-blue) ![python](https://img.shields.io/badge/python-3.10%2B-green)

---

## Features

- **Interactive crop box** — draw a region, then drag the box to move it or grab
  any of the 8 handles to resize. The area outside the crop is dimmed and a
  rule-of-thirds grid is overlaid for framing.
- **Aspect-ratio lock** — Free, Original, 1:1, 16:9, 9:16, 4:3, 3:4. The box
  keeps its ratio as you drag.
- **Trim in time** — set In/Out points to cut the clip's duration, not just the
  frame. A timeline bar shows the range, markers, and playhead.
- **Playback** — Play/Pause the preview (loops within the trimmed range) so you
  can check exactly what will be exported.
- **Drag & drop** — drop a video file anywhere on the window to open it.
- **Real progress** — a true percentage bar with elapsed/total time, plus a
  working **Cancel** button.
- **Output control** — quality slider (CRF) and a keep-audio toggle.
- **ffmpeg auto-detection** — finds a bundled ffmpeg or one on your `PATH`, and
  falls back to a browse dialog.

---

## Requirements

- **Python 3.10+**
- **ffmpeg** — auto-detected in this order: an `ffmpeg.exe` next to the app, a
  known bundled location, then your system `PATH`. If none is found you'll be
  prompted to locate `ffmpeg.exe`.
- Python packages (installed automatically on first run if missing):
  - `opencv-python` — frame decoding for the preview
  - `Pillow` — image display
  - `customtkinter` — the UI
  - `tkinterdnd2` — drag-and-drop

To install the packages manually:

```bash
pip install opencv-python Pillow customtkinter tkinterdnd2
```

---

## Usage

```bash
python video_cropper.py
```

1. **Open a video** — click *Open Video* (`Ctrl+O`) or drag a file onto the window.
2. **Set the crop** — drag a rectangle on the preview, then move/resize it with
   the handles. Optionally lock an aspect ratio, or type exact `X / Y / W / H`
   values. *Full frame* resets to the whole frame; *Center* centers the current box.
3. **Trim (optional)** — scrub to a frame and press *Set In* (`i`); scrub to the
   end and press *Set Out* (`o`). Use Play to preview the trimmed range.
4. **Choose output** — set the quality (CRF) and whether to keep audio.
5. **Export** — click *Crop & Save MP4*, pick a destination, and watch the
   progress. *Cancel* stops the export and removes the partial file.

---

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Space` | Play / Pause |
| `←` / `→` | Step 1 frame back / forward |
| `Shift + ←` / `→` | Step 10 frames |
| `Home` / `End` | Jump to In / Out point |
| `i` / `o` | Set In / Out point |
| `Ctrl + O` | Open video |

---

## Notes

- Crop dimensions are snapped to **even numbers** (required by the H.264 encoder).
- Export uses `libx264` (`-preset fast`) with audio re-encoded to AAC (or dropped
  if *Keep audio* is off). When the crop equals the full frame, the crop filter is
  skipped so a trim-only export re-encodes without scaling.
- **CRF guide:** lower = better quality / larger file. 14–18 ≈ visually lossless,
  19–22 high, 23–26 medium, 27–30 low. Default is 18.

---

## Building a standalone .exe

A Windows executable is produced with [PyInstaller](https://pyinstaller.org):

```bash
pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name VideoCropper \
  --icon icon.ico --add-data "icon.ico;." \
  --collect-all customtkinter --collect-all tkinterdnd2 video_cropper.py
```

The result is `dist\VideoCropper.exe` — a single self-contained file (no Python
needed on the target machine); all four Python packages are bundled inside.

**ffmpeg is *not* embedded.** To run the exe on a machine without ffmpeg, place
an `ffmpeg.exe` in the same folder as `VideoCropper.exe` (it's picked up
automatically), or let the app prompt you to locate one.

Build artifacts (`build/`, `dist/`, `VideoCropper.spec`) are regenerated each
build and don't need to be committed.

---

## Files

- `video_cropper.py` — the application.
- `icon.ico` — app / executable icon.
- `video_cropper_original.py.bak` — the original single-draw version, kept as a backup.
