"""
Video Cropper  —  interactive crop + trim, modern UI
=====================================================
• Drag a crop box, then grab its handles to move / resize it.
• Lock an aspect ratio (1:1, 16:9, 9:16 …) for clean exports.
• Set In / Out points to trim the clip in time, not just space.
• Real percentage progress (with ETA) and a working Cancel button.

Requires:  pip install opencv-python Pillow customtkinter
(missing packages are installed automatically on first run)
"""

import os
import sys
import math
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

# ── dependency bootstrap ────────────────────────────────────────────────────
def _ensure(*pkgs):
    subprocess.run([sys.executable, "-m", "pip", "install", *pkgs,
                    "--break-system-packages", "-q"])

try:
    import cv2
    from PIL import Image, ImageTk
    import customtkinter as ctk
    from tkinterdnd2 import TkinterDnD, DND_FILES
except ImportError:
    _ensure("opencv-python", "Pillow", "customtkinter", "tkinterdnd2")
    import cv2
    from PIL import Image, ImageTk
    import customtkinter as ctk
    from tkinterdnd2 import TkinterDnD, DND_FILES


class TkDnD(ctk.CTk, TkinterDnD.DnDWrapper):
    """A CustomTkinter root window with drag-and-drop support."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.TkdndVersion = TkinterDnD._require(self)


__version__ = "1.0.0"


# ── ffmpeg discovery ─────────────────────────────────────────────────────────
BUNDLED_FFMPEG = r"C:\Users\Saksham\Desktop\SakshamFiles\Application\iTFlow2\iTFlow.UI\bin\ReleaseBeta\bmptoavi\ffmpeg.exe"

def _app_dir():
    """Folder of the .exe (frozen build) or this script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def _resource(name):
    """Path to a bundled asset (PyInstaller _MEIPASS, or the source dir)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)

def find_ffmpeg():
    """Return a usable ffmpeg path, or None."""
    candidates = [os.path.join(_app_dir(), "ffmpeg.exe"),
                  BUNDLED_FFMPEG, shutil.which("ffmpeg")]
    for c in candidates:
        if c and os.path.exists(c):
            return c
        if c and shutil.which(c):
            return c
    return None


# Hide a console window for child processes on Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


# ── palette (Catppuccin Mocha, refined) ──────────────────────────────────────
C = {
    "base":     "#000000",
    "surface":  "#0d0d0d",
    "surface2": "#1a1a1a",
    "overlay":  "#2e2e2e",
    "text":     "#ededed",
    "subtext":  "#a8a8a8",
    "muted":    "#6b6b6b",
    "blue":     "#89b4fa",
    "blue_h":   "#74a0f0",
    "green":    "#a6e3a1",
    "green_h":  "#94d68f",
    "red":      "#f38ba8",
    "red_h":    "#eb6f92",
    "yellow":   "#f9e2af",
    "mauve":    "#c8c8c8",   # section headings — neutral grey (no purple)
}

ASPECTS = {           # label -> ratio (w/h), or None
    "Free":     None,
    "Original": "orig",
    "1:1":      1 / 1,
    "16:9":     16 / 9,
    "9:16":     9 / 16,
    "4:3":      4 / 3,
    "3:4":      3 / 4,
}

HANDLE = 9            # half-size of a resize handle, in px
HIT = 11              # hit radius for grabbing handles


def fmt_time(seconds):
    if seconds is None or seconds < 0:
        seconds = 0
    m, s = divmod(seconds, 60)
    h, m = divmod(int(m), 60)
    if h:
        return f"{h:d}:{m:02d}:{s:05.2f}"
    return f"{m:d}:{s:05.2f}"


class VideoCropper:
    def __init__(self, root, ffmpeg):
        self.root = root
        self.ffmpeg = ffmpeg

        root.title(f"Video Cropper {__version__}")
        root.geometry("1180x760")
        root.minsize(940, 620)
        root.configure(fg_color=C["base"])

        # ── video state ──────────────────────────────────────────────
        self.video_path = None
        self.cap = None
        self.frame_count = 0
        self.fps = 30.0
        self.orig_w = self.orig_h = 0
        self.cur_frame_idx = 0
        self.frame_rgb = None          # full-res RGB ndarray of preview frame
        self.photo = None

        # display transform (image -> canvas)
        self.scale = 1.0
        self.off_x = self.off_y = 0
        self.disp_w = self.disp_h = 0

        # ── crop state (authoritative, in *video* pixel coords) ──────
        self.crop = None               # (x, y, w, h) or None
        self.ratio = None              # locked aspect ratio (w/h) or None

        # interaction
        self._mode = None              # 'new' | 'move' | handle key
        self._anchor = None            # mode-specific drag anchor data

        # ── trim state (frame indices) ──────────────────────────────
        self.in_frame = 0
        self.out_frame = 0

        # export
        self._proc = None
        self._cancelled = False

        # playback
        self.playing = False
        self._play_job = None
        self._suppress_scrub = False

        self._build_ui()
        self._update_controls_enabled()

    # ════════════════════════════════════════════════════════════ UI ══
    def _build_ui(self):
        root = self.root
        root.grid_columnconfigure(0, weight=1)
        root.grid_columnconfigure(1, weight=0)
        root.grid_rowconfigure(1, weight=1)

        self._build_toolbar()
        self._build_editor()
        self._build_panel()
        self._build_statusbar()

        # keyboard shortcuts
        root.bind("<Left>",        lambda e: self._step(-1))
        root.bind("<Right>",       lambda e: self._step(1))
        root.bind("<Shift-Left>",  lambda e: self._step(-10))
        root.bind("<Shift-Right>", lambda e: self._step(10))
        root.bind("<Home>",        lambda e: self._seek_to(self.in_frame))
        root.bind("<End>",         lambda e: self._seek_to(self.out_frame))
        root.bind("i",             lambda e: self._set_in())
        root.bind("o",             lambda e: self._set_out())
        root.bind("<Control-o>",   lambda e: self._open_video())
        root.bind("<space>",       self._on_space)

    def _build_toolbar(self):
        bar = ctk.CTkFrame(self.root, fg_color=C["surface"], corner_radius=0,
                           height=58)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.grid_propagate(False)

        self.btn_open = ctk.CTkButton(
            bar, text="  Open Video", width=140, height=36,
            command=self._open_video,
            fg_color=C["blue"], hover_color=C["blue_h"], text_color=C["base"],
            font=ctk.CTkFont(size=14, weight="bold"))
        self.btn_open.pack(side="left", padx=(16, 12), pady=11)

        self.lbl_file = ctk.CTkLabel(
            bar, text="No file loaded", text_color=C["subtext"],
            font=ctk.CTkFont(size=13), anchor="w")
        self.lbl_file.pack(side="left", padx=4)

        # ffmpeg status pill
        self.lbl_ff = ctk.CTkLabel(
            bar, text="● ffmpeg ready", text_color=C["green"],
            font=ctk.CTkFont(size=12))
        self.lbl_ff.pack(side="right", padx=16)

    def _build_editor(self):
        wrap = ctk.CTkFrame(self.root, fg_color=C["base"], corner_radius=0)
        wrap.grid(row=1, column=0, sticky="nsew", padx=(14, 8), pady=10)
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)

        # preview canvas (raw tk for custom drawing)
        self.canvas = tk.Canvas(
            wrap, bg=C["base"], highlightthickness=1,
            highlightbackground=C["overlay"], bd=0, cursor="crosshair")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda e: self._render())
        self.canvas.bind("<ButtonPress-1>",   self._on_press)
        self.canvas.bind("<B1-Motion>",        self._on_drag)
        self.canvas.bind("<ButtonRelease-1>",  self._on_release)
        self.canvas.bind("<Motion>",           self._on_hover)

        # drag-and-drop a video file onto the preview
        for w in (self.canvas, self.root):
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>",      self._on_drop)
            w.dnd_bind("<<DropEnter>>", self._on_drop_enter)
            w.dnd_bind("<<DropLeave>>", self._on_drop_leave)

        # ── timeline + transport ─────────────────────────────────────
        tl = ctk.CTkFrame(wrap, fg_color=C["surface"], corner_radius=10)
        tl.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        tl.grid_columnconfigure(0, weight=1)

        # visual range bar (shows in/out fill + playhead)
        self.timeline = tk.Canvas(tl, height=26, bg=C["surface"],
                                  highlightthickness=0, bd=0)
        self.timeline.grid(row=0, column=0, columnspan=6, sticky="ew",
                           padx=14, pady=(12, 2))
        self.timeline.bind("<Configure>", lambda e: self._draw_timeline())
        self.timeline.bind("<Button-1>",  self._timeline_click)
        self.timeline.bind("<B1-Motion>", self._timeline_click)

        # scrubber
        self.scrub_var = tk.DoubleVar(value=0)
        self.scrub = ctk.CTkSlider(
            tl, from_=0, to=1, variable=self.scrub_var,
            command=self._on_scrub, progress_color=C["blue"],
            button_color=C["blue"], button_hover_color=C["blue_h"],
            fg_color=C["surface2"])
        self.scrub.grid(row=1, column=0, columnspan=6, sticky="ew",
                       padx=14, pady=(0, 6))

        # transport row
        tr = ctk.CTkFrame(tl, fg_color="transparent")
        tr.grid(row=2, column=0, columnspan=6, sticky="ew", padx=10,
               pady=(0, 10))

        def tbtn(txt, cmd, w=42):
            return ctk.CTkButton(tr, text=txt, width=w, height=30, command=cmd,
                                 fg_color=C["surface2"], hover_color=C["overlay"],
                                 text_color=C["text"],
                                 font=ctk.CTkFont(size=13))

        self.btn_play = ctk.CTkButton(
            tr, text="▶  Play", width=86, height=30, command=self._toggle_play,
            fg_color=C["blue"], hover_color=C["blue_h"], text_color=C["base"],
            font=ctk.CTkFont(size=13, weight="bold"))
        self.btn_play.pack(side="left", padx=(2, 10))

        tbtn("⏮", lambda: self._seek_to(self.in_frame)).pack(side="left", padx=2)
        tbtn("◀◀", lambda: self._step(-10)).pack(side="left", padx=2)
        tbtn("◀",  lambda: self._step(-1)).pack(side="left", padx=2)
        tbtn("▶",  lambda: self._step(1)).pack(side="left", padx=2)
        tbtn("▶▶", lambda: self._step(10)).pack(side="left", padx=2)
        tbtn("⏭", lambda: self._seek_to(self.out_frame)).pack(side="left", padx=2)

        self.lbl_frame = ctk.CTkLabel(
            tr, text="Frame 0 / 0   ·   0:00.00",
            text_color=C["subtext"], font=ctk.CTkFont(size=12))
        self.lbl_frame.pack(side="right", padx=8)

        self._transport = tr

    def _build_panel(self):
        panel = ctk.CTkScrollableFrame(
            self.root, width=300, fg_color=C["surface"], corner_radius=12,
            scrollbar_button_color=C["overlay"],
            scrollbar_button_hover_color=C["muted"])
        panel.grid(row=1, column=1, sticky="nsew", padx=(8, 14), pady=10)

        def heading(text):
            ctk.CTkLabel(panel, text=text.upper(), text_color=C["mauve"],
                         font=ctk.CTkFont(size=12, weight="bold"),
                         anchor="w").pack(fill="x", padx=4, pady=(16, 6))

        # ── CROP ─────────────────────────────────────────────────────
        heading("Crop region")

        grid = ctk.CTkFrame(panel, fg_color="transparent")
        grid.pack(fill="x", padx=4)
        grid.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self.var_x = tk.StringVar(value="0")
        self.var_y = tk.StringVar(value="0")
        self.var_w = tk.StringVar(value="0")
        self.var_h = tk.StringVar(value="0")

        def field(col, lbl, var):
            ctk.CTkLabel(grid, text=lbl, text_color=C["muted"],
                         font=ctk.CTkFont(size=11)).grid(
                row=0, column=col, sticky="w", padx=(4, 0))
            e = ctk.CTkEntry(grid, textvariable=var, width=58, height=30,
                             fg_color=C["surface2"], border_width=0,
                             text_color=C["text"],
                             font=ctk.CTkFont(size=13))
            e.grid(row=1, column=col, padx=3, pady=2, sticky="ew")
            e.bind("<Return>", lambda ev: self._apply_manual())
            e.bind("<FocusOut>", lambda ev: self._apply_manual())
            return e

        field(0, "X", self.var_x)
        field(1, "Y", self.var_y)
        field(2, "W", self.var_w)
        field(3, "H", self.var_h)

        # aspect ratio
        ctk.CTkLabel(panel, text="Aspect ratio", text_color=C["muted"],
                     font=ctk.CTkFont(size=11), anchor="w").pack(
            fill="x", padx=8, pady=(10, 2))
        self.ratio_var = tk.StringVar(value="Free")
        self.seg_ratio = ctk.CTkSegmentedButton(
            panel, values=list(ASPECTS.keys()), variable=self.ratio_var,
            command=self._on_ratio, fg_color=C["surface2"],
            selected_color=C["blue"], selected_hover_color=C["blue_h"],
            unselected_color=C["surface2"], unselected_hover_color=C["overlay"],
            text_color=C["text"], font=ctk.CTkFont(size=11))
        self.seg_ratio.pack(fill="x", padx=4, pady=2)

        rowf = ctk.CTkFrame(panel, fg_color="transparent")
        rowf.pack(fill="x", padx=4, pady=(6, 0))
        ctk.CTkButton(rowf, text="Full frame", height=30, command=self._full_frame,
                      fg_color=C["surface2"], hover_color=C["overlay"],
                      text_color=C["text"], font=ctk.CTkFont(size=12)
                      ).pack(side="left", expand=True, fill="x", padx=2)
        ctk.CTkButton(rowf, text="Center", height=30, command=self._center_crop,
                      fg_color=C["surface2"], hover_color=C["overlay"],
                      text_color=C["text"], font=ctk.CTkFont(size=12)
                      ).pack(side="left", expand=True, fill="x", padx=2)

        # ── TRIM ─────────────────────────────────────────────────────
        heading("Trim")
        trimf = ctk.CTkFrame(panel, fg_color="transparent")
        trimf.pack(fill="x", padx=4)
        trimf.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkButton(trimf, text="Set In  [i]", height=32, command=self._set_in,
                      fg_color=C["surface2"], hover_color=C["green"],
                      text_color=C["text"], font=ctk.CTkFont(size=12)
                      ).grid(row=0, column=0, padx=3, pady=2, sticky="ew")
        ctk.CTkButton(trimf, text="Set Out  [o]", height=32, command=self._set_out,
                      fg_color=C["surface2"], hover_color=C["red"],
                      text_color=C["text"], font=ctk.CTkFont(size=12)
                      ).grid(row=0, column=1, padx=3, pady=2, sticky="ew")

        self.lbl_trim = ctk.CTkLabel(
            panel, text="In 0:00.00   ·   Out 0:00.00\nDuration 0:00.00",
            text_color=C["subtext"], font=ctk.CTkFont(size=12), justify="left")
        self.lbl_trim.pack(fill="x", padx=8, pady=(6, 0))

        # ── LOOP ─────────────────────────────────────────────────────
        heading("Loop / repeat")
        loopf = ctk.CTkFrame(panel, fg_color="transparent")
        loopf.pack(fill="x", padx=4)
        self.loop_var = tk.IntVar(value=1)

        ctk.CTkButton(loopf, text="−", width=36, height=32,
                      command=lambda: self._bump_loop(-1),
                      fg_color=C["surface2"], hover_color=C["overlay"],
                      text_color=C["text"], font=ctk.CTkFont(size=16)
                      ).pack(side="left", padx=(0, 4))
        self.entry_loop = ctk.CTkEntry(
            loopf, textvariable=self.loop_var, width=64, height=32,
            justify="center", fg_color=C["surface2"], border_width=0,
            text_color=C["text"], font=ctk.CTkFont(size=14, weight="bold"))
        self.entry_loop.pack(side="left")
        self.entry_loop.bind("<Return>",   lambda e: self._update_loop_label())
        self.entry_loop.bind("<FocusOut>", lambda e: self._update_loop_label())
        ctk.CTkButton(loopf, text="+", width=36, height=32,
                      command=lambda: self._bump_loop(1),
                      fg_color=C["surface2"], hover_color=C["overlay"],
                      text_color=C["text"], font=ctk.CTkFont(size=16)
                      ).pack(side="left", padx=(4, 8))
        ctk.CTkLabel(loopf, text="× plays", text_color=C["muted"],
                     font=ctk.CTkFont(size=12)).pack(side="left")

        self.lbl_loop = ctk.CTkLabel(
            panel, text="Output ≈ 0:00.00  (1× — no loop)",
            text_color=C["subtext"], font=ctk.CTkFont(size=12), anchor="w")
        self.lbl_loop.pack(fill="x", padx=8, pady=(6, 0))

        # ── OUTPUT ───────────────────────────────────────────────────
        heading("Output quality")
        self.crf_var = tk.IntVar(value=18)
        self.lbl_crf = ctk.CTkLabel(
            panel, text="CRF 18  ·  visually lossless",
            text_color=C["subtext"], font=ctk.CTkFont(size=12), anchor="w")
        self.lbl_crf.pack(fill="x", padx=8, pady=(2, 2))
        ctk.CTkSlider(panel, from_=14, to=30, number_of_steps=16,
                      variable=self.crf_var, command=self._on_crf,
                      progress_color=C["green"], button_color=C["green"],
                      button_hover_color=C["green_h"], fg_color=C["surface2"]
                      ).pack(fill="x", padx=6, pady=2)

        self.audio_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(panel, text="Keep audio", variable=self.audio_var,
                        text_color=C["text"], fg_color=C["blue"],
                        hover_color=C["blue_h"], font=ctk.CTkFont(size=12)
                        ).pack(anchor="w", padx=8, pady=(8, 4))

        # ── VIDEO INFO ───────────────────────────────────────────────
        heading("Video info")
        self.lbl_info = ctk.CTkLabel(
            panel, text="—", text_color=C["muted"],
            font=ctk.CTkFont(size=12), justify="left", anchor="w")
        self.lbl_info.pack(fill="x", padx=8)

        # ── EXPORT ───────────────────────────────────────────────────
        ctk.CTkFrame(panel, fg_color="transparent", height=10).pack()
        self.btn_export = ctk.CTkButton(
            panel, text="✂  Crop & Save MP4", height=44, command=self._export,
            fg_color=C["green"], hover_color=C["green_h"], text_color=C["base"],
            font=ctk.CTkFont(size=15, weight="bold"))
        self.btn_export.pack(fill="x", padx=4, pady=(8, 4))

        self.btn_cancel = ctk.CTkButton(
            panel, text="Cancel export", height=32, command=self._cancel,
            fg_color=C["red"], hover_color=C["red_h"], text_color=C["base"],
            font=ctk.CTkFont(size=13, weight="bold"))
        # shown only during export

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self.root, fg_color=C["surface"], corner_radius=0,
                           height=44)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_columnconfigure(0, weight=1)

        self.lbl_status = ctk.CTkLabel(
            bar, text="Ready  ·  drag a video in or click Open", text_color=C["subtext"],
            font=ctk.CTkFont(size=12), anchor="w")
        self.lbl_status.grid(row=0, column=0, sticky="w", padx=16, pady=10)

        self.progress = ctk.CTkProgressBar(
            bar, width=260, progress_color=C["green"], fg_color=C["surface2"])
        self.progress.set(0)
        # gridded only during export

    # ═══════════════════════════════════════════════ file / frames ══
    def _open_video(self):
        path = filedialog.askopenfilename(
            title="Select video file",
            filetypes=[("Video files",
                        "*.mp4 *.mov *.avi *.mkv *.webm *.m4v *.wmv"),
                       ("All files", "*.*")])
        if path:
            self._load_video(path)

    def _load_video(self, path):
        if not path or not os.path.exists(path):
            return
        self._stop_play()
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            messagebox.showerror("Error", "Could not open that video file.")
            return
        if self.cap:
            self.cap.release()

        self.cap = cap
        self.video_path = path
        self.orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.frame_count = max(1, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

        self.lbl_file.configure(text=os.path.basename(path), text_color=C["text"])
        dur = self.frame_count / self.fps
        self.lbl_info.configure(
            text=f"{self.orig_w} × {self.orig_h}\n{self.fps:.2f} fps\n"
                 f"{self.frame_count} frames\n{fmt_time(dur)} long")

        # reset crop -> full frame, trim -> whole clip
        self.crop = (0, 0, self.orig_w, self.orig_h)
        self.in_frame = 0
        self.out_frame = self.frame_count - 1
        self.scrub.configure(to=max(1, self.frame_count - 1))
        self.cur_frame_idx = 0
        self.scrub_var.set(0)

        self._show_frame(0)
        self._sync_crop_fields()
        self._update_trim_label()
        self._update_loop_label()
        self._update_controls_enabled()
        self._set_status("Drag on the preview to draw a crop · grab handles to adjust",
                         C["subtext"])

    def _show_frame(self, n):
        if not self.cap:
            return
        n = max(0, min(self.frame_count - 1, int(n)))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, n)
        ok, frame = self.cap.read()
        if not ok:
            return
        self.cur_frame_idx = n
        self.frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        self.lbl_frame.configure(
            text=f"Frame {n} / {self.frame_count - 1}   ·   {fmt_time(n / self.fps)}")
        self._render()
        self._draw_timeline()

    def _seek_to(self, n):
        if not self.cap:
            return
        n = max(0, min(self.frame_count - 1, int(n)))
        self.scrub_var.set(n)
        self._show_frame(n)

    def _step(self, delta):
        self._stop_play()
        self._seek_to(self.cur_frame_idx + delta)

    def _on_scrub(self, val):
        if self._suppress_scrub:
            return
        if self.cap:
            self._stop_play()
            self._show_frame(int(float(val)))

    # ═══════════════════════════════════════════════════ playback ══
    def _toggle_play(self, event=None):
        if not self.cap:
            return
        if self.playing:
            self._stop_play()
        else:
            self.playing = True
            self.btn_play.configure(text="⏸  Pause")
            if self.cur_frame_idx >= self.out_frame:   # restart from In
                self._seek_to(self.in_frame)
            self._play_tick()

    def _play_tick(self):
        if not self.playing:
            return
        nxt = self.cur_frame_idx + 1
        if nxt > self.out_frame:                        # loop within trim range
            nxt = self.in_frame
        self._suppress_scrub = True
        self.scrub_var.set(nxt)
        self._suppress_scrub = False
        self._show_frame(nxt)
        self._play_job = self.root.after(
            max(1, int(1000 / self.fps)), self._play_tick)

    def _stop_play(self):
        self.playing = False
        if self._play_job:
            self.root.after_cancel(self._play_job)
            self._play_job = None
        if hasattr(self, "btn_play"):
            self.btn_play.configure(text="▶  Play")

    def _on_space(self, event):
        if isinstance(self.root.focus_get(), tk.Entry):   # don't hijack typing
            return
        self._toggle_play()

    # ═══════════════════════════════════════════════ drag & drop ══
    VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv")

    @staticmethod
    def _parse_drop(data):
        """Pull the first path out of a tkdnd drop payload (handles braces)."""
        data = data.strip()
        if data.startswith("{"):                      # {C:\path with spaces}
            return data[1:].split("}", 1)[0]
        return data.split()[0] if data else ""

    def _on_drop(self, event):
        self._on_drop_leave(event)
        path = self._parse_drop(event.data)
        if not path:
            return
        if not path.lower().endswith(self.VIDEO_EXTS):
            self._set_status(f"Not a supported video: {os.path.basename(path)}",
                             C["red"])
            return
        self._load_video(path)

    def _on_drop_enter(self, event):
        self.canvas.configure(highlightbackground=C["blue"], highlightthickness=2)
        self._set_status("Drop to open video…", C["blue"])
        return event.action

    def _on_drop_leave(self, event):
        self.canvas.configure(highlightbackground=C["overlay"],
                              highlightthickness=1)
        return event.action

    # ═══════════════════════════════════════════════════ rendering ══
    def _compute_transform(self):
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw < 2 or ch < 2 or not self.orig_w:
            return False
        self.scale = min(cw / self.orig_w, ch / self.orig_h)
        self.disp_w = max(1, int(self.orig_w * self.scale))
        self.disp_h = max(1, int(self.orig_h * self.scale))
        self.off_x = (cw - self.disp_w) // 2
        self.off_y = (ch - self.disp_h) // 2
        return True

    def _v2c(self, vx, vy):
        return int(vx * self.scale) + self.off_x, int(vy * self.scale) + self.off_y

    def _c2v(self, cx, cy):
        vx = (cx - self.off_x) / self.scale
        vy = (cy - self.off_y) / self.scale
        return (max(0, min(self.orig_w, vx)), max(0, min(self.orig_h, vy)))

    def _render(self):
        self.canvas.delete("all")
        if self.frame_rgb is None:
            cw = self.canvas.winfo_width() or 1
            ch = self.canvas.winfo_height() or 1
            self.canvas.create_text(cw // 2, ch // 2,
                                    text="Drag a video here  ·  or click “Open Video”",
                                    fill=C["muted"],
                                    font=("Helvetica", 16))
            return
        if not self._compute_transform():
            return

        img = Image.fromarray(self.frame_rgb).resize(
            (self.disp_w, self.disp_h), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(img)
        self.canvas.create_image(self.off_x, self.off_y, anchor="nw",
                                 image=self.photo)
        self._draw_crop()

    def _draw_crop(self):
        if not self.crop:
            return
        x, y, w, h = self.crop
        cx0, cy0 = self._v2c(x, y)
        cx1, cy1 = self._v2c(x + w, y + h)

        # dim the area outside the crop (4 stippled rects over the image)
        ix0, iy0 = self.off_x, self.off_y
        ix1, iy1 = self.off_x + self.disp_w, self.off_y + self.disp_h
        dim = dict(fill=C["base"], stipple="gray50", outline="")
        self.canvas.create_rectangle(ix0, iy0, ix1, cy0, **dim)      # top
        self.canvas.create_rectangle(ix0, cy1, ix1, iy1, **dim)      # bottom
        self.canvas.create_rectangle(ix0, cy0, cx0, cy1, **dim)      # left
        self.canvas.create_rectangle(cx1, cy0, ix1, cy1, **dim)      # right

        # rule-of-thirds grid
        for i in (1, 2):
            gx = cx0 + (cx1 - cx0) * i / 3
            gy = cy0 + (cy1 - cy0) * i / 3
            self.canvas.create_line(gx, cy0, gx, cy1, fill=C["overlay"])
            self.canvas.create_line(cx0, gy, cx1, gy, fill=C["overlay"])

        # border
        self.canvas.create_rectangle(cx0, cy0, cx1, cy1,
                                     outline=C["blue"], width=2)

        # handles
        for hx, hy in self._handle_points(cx0, cy0, cx1, cy1).values():
            self.canvas.create_rectangle(hx - HANDLE, hy - HANDLE,
                                         hx + HANDLE, hy + HANDLE,
                                         fill=C["blue"], outline=C["base"])

        # size readout
        self.canvas.create_text(
            (cx0 + cx1) // 2, cy0 - 12 if cy0 > 20 else cy0 + 12,
            text=f"{w} × {h}", fill=C["yellow"],
            font=("Helvetica", 11, "bold"))

    @staticmethod
    def _handle_points(x0, y0, x1, y1):
        mx, my = (x0 + x1) // 2, (y0 + y1) // 2
        return {
            "nw": (x0, y0), "n": (mx, y0), "ne": (x1, y0),
            "w":  (x0, my),                "e":  (x1, my),
            "sw": (x0, y1), "s": (mx, y1), "se": (x1, y1),
        }

    # ═══════════════════════════════════════════════ crop editing ══
    def _hit_test(self, cx, cy):
        if not self.crop:
            return None
        x, y, w, h = self.crop
        a0 = self._v2c(x, y)
        a1 = self._v2c(x + w, y + h)
        for key, (hx, hy) in self._handle_points(*a0, *a1).items():
            if abs(cx - hx) <= HIT and abs(cy - hy) <= HIT:
                return key
        if a0[0] <= cx <= a1[0] and a0[1] <= cy <= a1[1]:
            return "move"
        return None

    _CURSORS = {
        "nw": "size_nw_se", "se": "size_nw_se",
        "ne": "size_ne_sw", "sw": "size_ne_sw",
        "n": "sb_v_double_arrow", "s": "sb_v_double_arrow",
        "e": "sb_h_double_arrow", "w": "sb_h_double_arrow",
        "move": "fleur",
    }

    def _on_hover(self, event):
        if self._mode:
            return
        hit = self._hit_test(event.x, event.y)
        self.canvas.configure(cursor=self._CURSORS.get(hit, "crosshair"))

    def _on_press(self, event):
        if not self.cap:
            return
        hit = self._hit_test(event.x, event.y)
        if hit == "move":
            self._mode = "move"
            vx, vy = self._c2v(event.x, event.y)
            self._anchor = (vx - self.crop[0], vy - self.crop[1])
        elif hit:                              # a resize handle
            self._mode = hit
        else:                                  # draw a new box
            self._mode = "new"
            self._anchor = self._c2v(event.x, event.y)
            self.crop = (self._anchor[0], self._anchor[1], 0, 0)

    def _on_drag(self, event):
        if not self._mode:
            return
        vx, vy = self._c2v(event.x, event.y)
        if self._mode == "move":
            self._do_move(vx, vy)
        elif self._mode == "new":
            self._do_new(vx, vy)
        else:
            self._do_resize(self._mode, vx, vy)
        self._render()
        self._sync_crop_fields()

    def _on_release(self, event):
        if not self._mode:
            return
        self._mode = None
        self._normalize_crop()
        self._render()
        self._sync_crop_fields()

    def _do_move(self, vx, vy):
        x, y, w, h = self.crop
        ax, ay = self._anchor
        nx = max(0, min(self.orig_w - w, vx - ax))
        ny = max(0, min(self.orig_h - h, vy - ay))
        self.crop = (nx, ny, w, h)

    def _do_new(self, vx, vy):
        x0, y0 = self._anchor
        w = vx - x0
        h = vy - y0
        if self.ratio:
            h = math.copysign(abs(w) / self.ratio, h or 1)
        x = min(x0, x0 + w)
        y = min(y0, y0 + h)
        self.crop = (x, y, abs(w), abs(h))

    def _do_resize(self, key, vx, vy):
        x, y, w, h = self.crop
        x1, y1 = x + w, y + h            # right / bottom edges
        if "w" in key: x = vx
        if "e" in key: x1 = vx
        if "n" in key: y = vy
        if "s" in key: y1 = vy
        nw, nh = x1 - x, y1 - y

        if self.ratio:
            # drive the perpendicular dimension from the dragged one
            if key in ("e", "w"):
                nh = nw / self.ratio
                cy = (y + y1) / 2
                y, y1 = cy - nh / 2, cy + nh / 2
            elif key in ("n", "s"):
                nw = nh * self.ratio
                cx = (x + x1) / 2
                x, x1 = cx - nw / 2, cx + nw / 2
            else:                        # corners: keep width, derive height
                nh = nw / self.ratio
                if "n" in key:
                    y = y1 - nh
                else:
                    y1 = y + nh
        self.crop = (min(x, x1), min(y, y1), abs(x1 - x), abs(y1 - y))

    def _normalize_crop(self):
        if not self.crop:
            return
        x, y, w, h = self.crop
        x = max(0, min(self.orig_w - 2, int(round(x))))
        y = max(0, min(self.orig_h - 2, int(round(y))))
        w = int(round(w)); h = int(round(h))
        w = min(w, self.orig_w - x)
        h = min(h, self.orig_h - y)
        w -= w % 2; h -= h % 2          # ffmpeg likes even dimensions
        if w < 2 or h < 2:
            self.crop = (0, 0, self.orig_w - self.orig_w % 2,
                         self.orig_h - self.orig_h % 2)
        else:
            self.crop = (x, y, w, h)

    def _sync_crop_fields(self):
        if not self.crop:
            return
        x, y, w, h = (int(v) for v in self.crop)
        self.var_x.set(str(x)); self.var_y.set(str(y))
        self.var_w.set(str(w)); self.var_h.set(str(h))

    def _apply_manual(self):
        if not self.cap:
            return
        try:
            x = int(float(self.var_x.get())); y = int(float(self.var_y.get()))
            w = int(float(self.var_w.get())); h = int(float(self.var_h.get()))
        except ValueError:
            return
        self.crop = (x, y, w, h)
        self._normalize_crop()
        self._render()
        self._sync_crop_fields()

    def _on_ratio(self, label):
        r = ASPECTS[label]
        if r == "orig" and self.orig_h:
            r = self.orig_w / self.orig_h
        self.ratio = r if isinstance(r, (int, float)) else None
        if self.ratio and self.crop:                 # reflow current box
            x, y, w, h = self.crop
            nh = w / self.ratio
            if y + nh > self.orig_h:
                nh = self.orig_h - y
                w = nh * self.ratio
            self.crop = (x, y, w, nh)
            self._normalize_crop()
            self._render()
            self._sync_crop_fields()

    def _full_frame(self):
        if self.cap:
            self.crop = (0, 0, self.orig_w - self.orig_w % 2,
                         self.orig_h - self.orig_h % 2)
            self._render(); self._sync_crop_fields()

    def _center_crop(self):
        if not (self.cap and self.crop):
            return
        _, _, w, h = self.crop
        x = (self.orig_w - w) // 2
        y = (self.orig_h - h) // 2
        self.crop = (x, y, w, h)
        self._normalize_crop()
        self._render(); self._sync_crop_fields()

    # ═══════════════════════════════════════════════════════ trim ══
    def _set_in(self):
        if not self.cap:
            return
        self.in_frame = self.cur_frame_idx
        if self.out_frame <= self.in_frame:
            self.out_frame = min(self.frame_count - 1, self.in_frame + 1)
        self._update_trim_label(); self._update_loop_label(); self._draw_timeline()

    def _set_out(self):
        if not self.cap:
            return
        self.out_frame = self.cur_frame_idx
        if self.in_frame >= self.out_frame:
            self.in_frame = max(0, self.out_frame - 1)
        self._update_trim_label(); self._update_loop_label(); self._draw_timeline()

    # ═══════════════════════════════════════════════════════ loop ══
    def _loop_count(self):
        try:
            n = int(float(self.loop_var.get()))
        except (ValueError, tk.TclError):
            n = 1
        return max(1, min(999, n))

    def _bump_loop(self, delta):
        self.loop_var.set(max(1, min(999, self._loop_count() + delta)))
        self._update_loop_label()

    def _update_loop_label(self):
        n = self._loop_count()
        self.loop_var.set(n)                       # normalise the displayed value
        base = ((self.out_frame - self.in_frame + 1) / self.fps) if self.cap else 0
        total = max(0, base) * n
        if n <= 1:
            self.lbl_loop.configure(text=f"Output ≈ {fmt_time(total)}  (1× — no loop)")
        else:
            self.lbl_loop.configure(
                text=f"Output ≈ {fmt_time(total)}  ({n}× of {fmt_time(base)})")

    def _update_trim_label(self):
        ti = self.in_frame / self.fps
        to = (self.out_frame + 1) / self.fps
        self.lbl_trim.configure(
            text=f"In {fmt_time(ti)}   ·   Out {fmt_time(to)}\n"
                 f"Duration {fmt_time(max(0, to - ti))}"
                 f"  ({self.out_frame - self.in_frame + 1} frames)")

    def _draw_timeline(self):
        c = self.timeline
        c.delete("all")
        w = c.winfo_width(); h = c.winfo_height()
        if w < 4 or not self.frame_count:
            return
        c.create_rectangle(0, h // 2 - 3, w, h // 2 + 3,
                           fill=C["surface2"], outline="")
        if self.frame_count > 1:
            xi = w * self.in_frame / (self.frame_count - 1)
            xo = w * self.out_frame / (self.frame_count - 1)
            xp = w * self.cur_frame_idx / (self.frame_count - 1)
        else:
            xi = xo = xp = 0
        # in/out range
        c.create_rectangle(xi, h // 2 - 4, xo, h // 2 + 4,
                           fill=C["blue"], outline="")
        # in / out markers
        c.create_polygon(xi, 2, xi + 7, 2, xi, 11, fill=C["green"], outline="")
        c.create_polygon(xo, 2, xo - 7, 2, xo, 11, fill=C["red"], outline="")
        # playhead
        c.create_line(xp, 0, xp, h, fill=C["text"], width=2)

    def _timeline_click(self, event):
        w = self.timeline.winfo_width()
        if w < 4 or self.frame_count < 2:
            return
        self._stop_play()
        frac = max(0, min(1, event.x / w))
        self._seek_to(round(frac * (self.frame_count - 1)))

    def _on_crf(self, val):
        v = int(float(val))
        quality = ("visually lossless" if v <= 18 else
                   "high" if v <= 22 else
                   "medium" if v <= 26 else "low")
        self.lbl_crf.configure(text=f"CRF {v}  ·  {quality}")

    # ═══════════════════════════════════════════════════════ export ══
    def _export(self):
        if not self.video_path or not self.crop:
            return
        self._stop_play()
        self._normalize_crop()
        x, y, w, h = self.crop

        default = os.path.splitext(os.path.basename(self.video_path))[0] + "_cropped.mp4"
        out_path = filedialog.asksaveasfilename(
            title="Save cropped video", initialfile=default,
            defaultextension=".mp4", filetypes=[("MP4 video", "*.mp4")])
        if not out_path:
            return

        ss = self.in_frame / self.fps
        dur = (self.out_frame - self.in_frame + 1) / self.fps
        full = (x == 0 and y == 0 and w >= self.orig_w - 1 and h >= self.orig_h - 1)
        count = self._loop_count()

        self._cancelled = False
        self.btn_export.configure(state="disabled")
        self.btn_open.configure(state="disabled")
        self.btn_cancel.pack(fill="x", padx=4, pady=(0, 6))
        self.progress.grid(row=0, column=1, sticky="e", padx=16, pady=10)
        self.progress.set(0)

        threading.Thread(
            target=self._do_export,
            args=(out_path, ss, dur, x, y, w, h, full, count),
            daemon=True).start()

    def _encode_cmd(self, src, dst, ss, dur, full, x, y, w, h):
        cmd = [self.ffmpeg, "-y", "-i", src, "-ss", f"{ss:.3f}", "-t", f"{dur:.3f}"]
        if not full:
            cmd += ["-vf", f"crop={w}:{h}:{x}:{y}"]
        cmd += ["-c:v", "libx264", "-preset", "fast",
                "-crf", str(self.crf_var.get())]
        cmd += ["-c:a", "aac"] if self.audio_var.get() else ["-an"]
        cmd += ["-progress", "pipe:1", "-nostats", dst]
        return cmd

    def _do_export(self, out_path, ss, dur, x, y, w, h, full, count):
        # Single pass: just crop/trim straight to the destination.
        if count <= 1:
            cmd = self._encode_cmd(self.video_path, out_path, ss, dur,
                                   full, x, y, w, h)
            rc, err = self._run_proc(cmd, dur, 0.0, 1.0, f"Cropping → {w}×{h}")
            self.root.after(0, lambda: self._done(rc, err, out_path))
            return

        # Looping: encode the clip once, then repeat it losslessly (stream copy).
        tmp = out_path + ".loopbase.mp4"
        try:
            cmd1 = self._encode_cmd(self.video_path, tmp, ss, dur,
                                    full, x, y, w, h)
            rc, err = self._run_proc(cmd1, dur, 0.0, 0.8,
                                     f"Cropping (1/2) → {w}×{h}")
            if rc != 0 or self._cancelled:
                self.root.after(0, lambda: self._done(rc, err, out_path))
                return
            total2 = dur * count
            cmd2 = [self.ffmpeg, "-y", "-stream_loop", str(count - 1), "-i", tmp,
                    "-c", "copy", "-progress", "pipe:1", "-nostats", out_path]
            rc2, err2 = self._run_proc(cmd2, total2, 0.8, 1.0,
                                       f"Looping ×{count} (2/2)")
            self.root.after(0, lambda: self._done(rc2, err2 or err, out_path))
        finally:
            if os.path.exists(tmp):
                try: os.remove(tmp)
                except OSError: pass

    def _run_proc(self, cmd, total, lo, hi, label):
        """Run one ffmpeg pass, mapping its progress into [lo, hi]."""
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, creationflags=_NO_WINDOW)
        except Exception as exc:
            return 1, str(exc)
        self._proc = proc

        err = []
        threading.Thread(
            target=lambda: err.extend(proc.stderr.readlines()),
            daemon=True).start()

        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time=") and total > 0:
                t = self._parse_time(line.split("=", 1)[1])
                if t is not None:
                    frac = lo + (hi - lo) * max(0, min(1, t / total))
                    self.root.after(0, lambda f=frac, lbl=label:
                                    self._on_progress(f, lbl))
        proc.wait()
        self._proc = None
        return proc.returncode, "".join(err)

    @staticmethod
    def _parse_time(text):
        try:
            if ":" in text:
                h, m, s = text.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
            return float(text)
        except (ValueError, AttributeError):
            return None

    def _on_progress(self, frac, label):
        self.progress.set(frac)
        self._set_status(f"{label}…  {frac * 100:3.0f}%", C["yellow"])

    def _cancel(self):
        if self._proc:
            self._cancelled = True
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._set_status("Cancelling…", C["red"])

    def _done(self, rc, stderr, out_path):
        self.progress.grid_forget()
        self.btn_cancel.pack_forget()
        self.btn_export.configure(state="normal")
        self.btn_open.configure(state="normal")

        if self._cancelled:
            self._set_status("Export cancelled.", C["subtext"])
            if os.path.exists(out_path):
                try: os.remove(out_path)
                except OSError: pass
            return
        if rc == 0:
            self._set_status(f"✓ Saved {os.path.basename(out_path)}", C["green"])
            messagebox.showinfo("Done", f"Saved to:\n{out_path}")
        else:
            self._set_status("Export failed — see error dialog.", C["red"])
            messagebox.showerror("ffmpeg error",
                                 stderr[-1500:] if stderr else "Unknown error")

    # ═══════════════════════════════════════════════════════ misc ══
    def _set_status(self, text, color=None):
        self.lbl_status.configure(text=text, text_color=color or C["subtext"])

    def _update_controls_enabled(self):
        state = "normal" if self.cap else "disabled"
        self.btn_export.configure(state=state)

    def cleanup(self):
        self._stop_play()
        if self.cap:
            self.cap.release()


# ─────────────────────────────────────────────────────────────────────────────
def main():
    ffmpeg = find_ffmpeg()

    ctk.set_appearance_mode("dark")
    root = TkDnD()
    try:
        root.iconbitmap(_resource("icon.ico"))
    except Exception:
        pass

    if not ffmpeg:
        root.withdraw()
        if messagebox.askyesno(
                "ffmpeg not found",
                "Could not locate ffmpeg automatically.\n\n"
                "Would you like to browse for ffmpeg.exe?"):
            chosen = filedialog.askopenfilename(
                title="Locate ffmpeg",
                filetypes=[("ffmpeg", "ffmpeg.exe ffmpeg"), ("All files", "*.*")])
            if chosen and os.path.exists(chosen):
                ffmpeg = chosen
        if not ffmpeg:
            messagebox.showerror("ffmpeg required",
                                 "ffmpeg is required to export. Exiting.")
            root.destroy()
            return
        root.deiconify()

    app = VideoCropper(root, ffmpeg)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.cleanup(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    main()
