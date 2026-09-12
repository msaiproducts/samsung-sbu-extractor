#!/usr/bin/env python3
"""
Samsung .sbu Extractor — Desktop GUI
Extracts photos and videos from Samsung phone backup files (.sbu).

The SBU format is a proprietary Samsung container.  Media files are stored raw
inside it and are located by their magic bytes (JPEG SOI, MP4 ftyp atom).

No external dependencies — only Python stdlib (tkinter, mmap, struct, pathlib).

To build as a standalone Windows .exe (run this on Windows):
    pip install pyinstaller
    pyinstaller --onefile --windowed --name "Samsung SBU Extractor" sbu_extractor_gui.py

The resulting dist/Samsung SBU Extractor.exe runs on any Windows machine
with no Python or software installation required.
"""

import io
import mmap
import struct
import threading
import queue
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk


# ═══════════════════════════════════════════════════════════════════════════════
#  EXTRACTION ENGINE  (no GUI dependencies — pure stdlib)
# ═══════════════════════════════════════════════════════════════════════════════

MIN_JPEG_BYTES  = 20_000        # ignore carved blobs smaller than this
MIN_JPEG_PIX    = 200           # ignore images narrower or shorter than this (px)
MAX_VIDEO_BYTES = 500 * 1024 * 1024   # safety cap per video (500 MB)


# ── JPEG helpers ───────────────────────────────────────────────────────────────

def _jpeg_dimensions(data: mmap.mmap, start: int, end: int):
    """Return (width, height) by scanning SOF markers, or None if not found."""
    SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB}
    pos = start + 2
    while pos < end - 3:
        if data[pos] != 0xFF:
            pos += 1
            continue
        while pos < end and data[pos] == 0xFF:
            pos += 1
        if pos >= end:
            break
        marker = data[pos]
        pos += 1
        if marker == 0xD9:
            break
        if 0xD0 <= marker <= 0xD8:
            continue
        if pos + 2 > end:
            break
        seg_len = (data[pos] << 8) | data[pos + 1]
        if seg_len < 2:
            break
        if marker in SOF_MARKERS:
            # SOF: length(2) + precision(1) + height(2) + width(2)
            if pos + 7 <= end:
                h = (data[pos + 3] << 8) | data[pos + 4]
                w = (data[pos + 5] << 8) | data[pos + 6]
                return w, h
        pos += seg_len
    return None


def _find_jpeg_end(mm: mmap.mmap, start: int, size: int) -> int:
    """Walk JPEG markers from *start* and return the byte offset just after EOI.
    Handles EXIF thumbnail nesting. Returns -1 if no valid EOI found."""
    pos = start + 2           # skip SOI FF D8
    end = min(start + size, len(mm))
    depth = 1                 # nesting level

    while pos < end - 1:
        b = mm[pos]
        if b != 0xFF:
            pos += 1
            continue
        while pos < end and mm[pos] == 0xFF:
            pos += 1
        if pos >= end:
            break
        marker = mm[pos]
        pos += 1

        if marker == 0xD8:            # nested SOI
            depth += 1
            continue
        if marker == 0xD9:            # EOI
            depth -= 1
            if depth == 0:
                return pos
            continue
        if 0xD0 <= marker <= 0xD7:   # RST markers
            continue
        if marker == 0x00:            # byte stuffing
            continue

        if pos + 2 > end:
            break
        seg_len = (mm[pos] << 8) | mm[pos + 1]
        if seg_len < 2:
            break

        if marker == 0xDA:            # SOS — scan data follows
            pos += seg_len
            while pos < end - 1:
                if mm[pos] == 0xFF:
                    nxt = mm[pos + 1]
                    if nxt != 0x00 and not (0xD0 <= nxt <= 0xD7):
                        break
                pos += 1
        else:
            pos += seg_len

    return -1


# ── MP4 / 3GP helpers ──────────────────────────────────────────────────────────

_VIDEO_STOP_ATOMS = {b'mdat', b'moov'}


def _find_mp4_end(mm: mmap.mmap, ftyp_pos: int) -> int:
    """Parse the MP4/3GP atom chain starting at ftyp_pos - 4.
    Returns the byte offset just after the last atom, or -1 on failure."""
    atom_start = ftyp_pos - 4
    if atom_start < 0:
        return -1

    total = len(mm)
    pos = atom_start
    seen = set()

    while pos < total - 7:
        atom_size = struct.unpack_from('>I', mm, pos)[0]
        atom_type = bytes(mm[pos + 4: pos + 8])

        if atom_size == 0:
            return total
        if atom_size == 1:
            if pos + 16 > total:
                return -1
            atom_size = struct.unpack_from('>Q', mm, pos + 8)[0]
        if atom_size < 8 or atom_size > MAX_VIDEO_BYTES:
            if b'ftyp' in seen and seen & _VIDEO_STOP_ATOMS:
                return pos
            return -1

        seen.add(atom_type)
        pos += atom_size

        if atom_type == b'mdat' and b'ftyp' in seen:
            return pos

    if b'ftyp' in seen and seen & _VIDEO_STOP_ATOMS:
        return pos
    return -1


# ── Main extraction function ───────────────────────────────────────────────────

def extract_sbu(
    sbu_path: Path,
    output_dir: Path,
    log,
    progress_cb=None,
) -> dict:
    """Carve photos and videos from an SBU backup file.

    Args:
        sbu_path:    Path to the .sbu file.
        output_dir:  Directory to write extracted files into.
        log:         Callable(str) — receives log lines.
        progress_cb: Optional callable(pct: float) — receives 0..100 progress.

    Returns:
        dict with keys 'photos', 'videos'.
    """
    (output_dir / 'photos').mkdir(parents=True, exist_ok=True)
    (output_dir / 'videos').mkdir(parents=True, exist_ok=True)

    filesize = sbu_path.stat().st_size
    log(f"\n── {sbu_path.name}  ({filesize / 1_048_576:.1f} MB) ──")
    log("  Scanning...")

    photos_saved = videos_saved = 0
    skipped_tiny = skipped_bad = 0
    last_progress_report = 0

    with open(sbu_path, 'rb') as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        total = len(mm)
        pos = 0

        while pos < total - 3:
            b0 = mm[pos]

            # ── JPEG ──
            if b0 == 0xFF and mm[pos + 1] == 0xD8 and mm[pos + 2] == 0xFF:
                end = _find_jpeg_end(mm, pos, 15 * 1024 * 1024)
                if end != -1:
                    blob_len = end - pos
                    if blob_len >= MIN_JPEG_BYTES:
                        dims = _jpeg_dimensions(mm, pos, end)
                        if dims and dims[0] >= MIN_JPEG_PIX and dims[1] >= MIN_JPEG_PIX:
                            out = output_dir / 'photos' / f'photo_{pos:012d}.jpg'
                            out.write_bytes(bytes(mm[pos:end]))
                            photos_saved += 1
                            log(f"  Photo: {out.name}  {dims[0]}×{dims[1]}  ({blob_len // 1024} KB)")
                            pos = end
                            continue
                        else:
                            skipped_tiny += 1
                    else:
                        skipped_tiny += 1
                pos += 1

            # ── MP4 / 3GP ──
            elif b0 == ord('f') and mm[pos:pos + 4] == b'ftyp':
                end = _find_mp4_end(mm, pos)
                if end != -1:
                    atom_start = pos - 4
                    blob = bytes(mm[atom_start:end])
                    brand = blob[4:8].decode('ascii', errors='replace').strip('\x00')
                    ext = '3gp' if brand.lower().startswith('3g') else 'mp4'
                    out = output_dir / 'videos' / f'video_{pos:012d}.{ext}'
                    out.write_bytes(blob)
                    videos_saved += 1
                    log(f"  Video: {out.name}  brand={brand}  ({len(blob) // 1024} KB)")
                    pos = end
                    continue
                else:
                    pos += 1

            else:
                pos += 1

            # Progress callback every 50 MB
            if progress_cb and pos - last_progress_report >= 50 * 1024 * 1024:
                last_progress_report = pos
                progress_cb(pos / total * 100)

        mm.close()

    if progress_cb:
        progress_cb(100.0)

    log(f"\n  Photos: {photos_saved}  |  Videos: {videos_saved}"
        f"  |  Skipped (tiny/invalid): {skipped_tiny}")

    return {'photos': photos_saved, 'videos': videos_saved}


# ═══════════════════════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════════════════════

BRAND   = "#1A1A2E"   # deep navy — window background
ACCENT  = "#E94560"   # vivid red — buttons
SURFACE = "#16213E"   # slightly lighter navy — log box
FG      = "#EAEAEA"   # near-white text
FG_DIM  = "#8892A4"   # muted text
SUCCESS = "#4CAF50"   # green for done
BTN_FG  = "#FFFFFF"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Samsung .sbu Extractor")
        self.resizable(False, False)
        self.configure(bg=BRAND)

        self._sbu_path: Path | None = None
        self._out_dir = tk.StringVar()
        self._running = False
        self._q: queue.Queue[str | None] = queue.Queue()
        self._pct_q: queue.Queue[float] = queue.Queue()

        self._build_ui()
        self._poll_queue()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        PAD = 18

        # ── header ──
        hdr = tk.Frame(self, bg=BRAND)
        hdr.pack(fill="x", padx=PAD, pady=(PAD, 0))

        tk.Label(
            hdr, text="Samsung .sbu Extractor",
            bg=BRAND, fg=FG,
            font=("Segoe UI", 16, "bold"),
        ).pack(side="left")

        tk.Label(
            hdr, text="  Recover photos & videos from old Samsung backups",
            bg=BRAND, fg=FG_DIM,
            font=("Segoe UI", 9),
        ).pack(side="left", pady=(4, 0))

        _sep(self)

        # ── file selection ──
        f1 = tk.Frame(self, bg=BRAND)
        f1.pack(fill="x", padx=PAD, pady=(10, 4))
        tk.Label(f1, text="Backup file (.sbu)", bg=BRAND, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(anchor="w")

        row1 = tk.Frame(f1, bg=BRAND)
        row1.pack(fill="x", pady=(2, 0))

        self._file_label = tk.Label(
            row1, text="No file selected",
            bg=SURFACE, fg=FG_DIM,
            font=("Segoe UI", 9), anchor="w", padx=8,
            width=55, relief="flat",
        )
        self._file_label.pack(side="left", fill="x", expand=True, ipady=6)

        _btn(row1, "Browse…", self._pick_file).pack(side="left", padx=(6, 0))

        # ── output folder ──
        f2 = tk.Frame(self, bg=BRAND)
        f2.pack(fill="x", padx=PAD, pady=(8, 4))
        tk.Label(f2, text="Output folder", bg=BRAND, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(anchor="w")

        row2 = tk.Frame(f2, bg=BRAND)
        row2.pack(fill="x", pady=(2, 0))

        tk.Entry(
            row2, textvariable=self._out_dir,
            bg=SURFACE, fg=FG, insertbackground=FG,
            font=("Segoe UI", 9), relief="flat", width=55,
        ).pack(side="left", fill="x", expand=True, ipady=6)

        _btn(row2, "Browse…", self._pick_output).pack(side="left", padx=(6, 0))

        _sep(self)

        # ── extract button + status ──
        ctrl = tk.Frame(self, bg=BRAND)
        ctrl.pack(fill="x", padx=PAD, pady=(10, 6))

        self._extract_btn = _btn(ctrl, "Extract", self._start_extract, big=True)
        self._extract_btn.pack(side="left")

        self._status = tk.Label(
            ctrl, text="", bg=BRAND, fg=FG_DIM, font=("Segoe UI", 9),
        )
        self._status.pack(side="left", padx=(14, 0))

        # ── progress bar (determinate — we know file size) ──
        self._progress = ttk.Progressbar(
            self, mode="determinate", length=560, maximum=100,
        )
        self._progress.pack(padx=PAD, pady=(0, 8))

        # ── log box ──
        log_frame = tk.Frame(self, bg=SURFACE, bd=0)
        log_frame.pack(fill="both", expand=True, padx=PAD, pady=(0, PAD))

        self._log = tk.Text(
            log_frame,
            bg=SURFACE, fg=FG, insertbackground=FG,
            font=("Consolas", 8), relief="flat",
            width=80, height=18,
            state="disabled", wrap="none",
        )
        sb = ttk.Scrollbar(log_frame, orient="vertical", command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log.pack(side="left", fill="both", expand=True, padx=6, pady=6)

        self.geometry("")

    # ── actions ──────────────────────────────────────────────────────────────

    def _pick_file(self):
        path = filedialog.askopenfilename(
            title="Select Samsung .sbu backup file",
            filetypes=[("Samsung backup", "*.sbu"), ("All files", "*.*")],
        )
        if not path:
            return
        self._sbu_path = Path(path)
        self._file_label.configure(text=self._sbu_path.name, fg=FG)
        if not self._out_dir.get():
            self._out_dir.set(str(self._sbu_path.parent / "sbu_extracted"))

    def _pick_output(self):
        d = filedialog.askdirectory(title="Select output folder")
        if d:
            self._out_dir.set(d)

    def _start_extract(self):
        if self._running:
            return
        if not self._sbu_path:
            self._flash_status("Select a .sbu file first.")
            return
        if not self._out_dir.get().strip():
            self._flash_status("Choose an output folder first.")
            return

        self._running = True
        self._extract_btn.configure(state="disabled")
        self._progress["value"] = 0
        self._status.configure(text="Extracting…", fg=FG_DIM)

        threading.Thread(target=self._run_extraction, daemon=True).start()

    def _run_extraction(self):
        out_dir = Path(self._out_dir.get().strip())

        def progress_cb(pct: float):
            self._pct_q.put(pct)

        try:
            counts = extract_sbu(
                self._sbu_path,
                out_dir,
                log=lambda msg: self._q.put(msg),
                progress_cb=progress_cb,
            )
            summary = (
                f"\n{'═' * 55}\n"
                f"  Done!\n"
                f"  Photos : {counts['photos']}\n"
                f"  Videos : {counts['videos']}\n"
                f"  Saved to : {out_dir}\n"
                f"{'═' * 55}"
            )
            self._q.put(summary)
        except Exception as exc:
            self._q.put(f"\n  ERROR: {exc}")

        self._q.put(None)   # sentinel

    # ── queue polling ─────────────────────────────────────────────────────────

    def _poll_queue(self):
        # Drain progress updates
        try:
            while True:
                pct = self._pct_q.get_nowait()
                self._progress["value"] = pct
        except queue.Empty:
            pass

        # Drain log messages
        try:
            while True:
                msg = self._q.get_nowait()
                if msg is None:
                    self._on_done()
                else:
                    self._append_log(msg)
        except queue.Empty:
            pass

        self.after(80, self._poll_queue)

    def _on_done(self):
        self._running = False
        self._progress["value"] = 100
        self._extract_btn.configure(state="normal")
        self._status.configure(text="Complete ✓", fg=SUCCESS)

    def _append_log(self, text: str):
        self._log.configure(state="normal")
        self._log.insert("end", text + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")

    def _flash_status(self, msg: str):
        self._status.configure(text=msg, fg=ACCENT)
        self.after(3000, lambda: self._status.configure(text="", fg=FG_DIM))


# ── helpers ───────────────────────────────────────────────────────────────────

def _sep(parent):
    tk.Frame(parent, bg="#2A2A4A", height=1).pack(fill="x", padx=18, pady=4)


def _btn(parent, text, cmd, big=False):
    size = 10 if big else 9
    px   = 20 if big else 12
    py   = 7  if big else 4
    return tk.Button(
        parent, text=text, command=cmd,
        bg=ACCENT, fg=BTN_FG, activebackground="#C73652",
        activeforeground=BTN_FG,
        font=("Segoe UI", size, "bold"),
        relief="flat", padx=px, pady=py, cursor="hand2", bd=0,
    )


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
