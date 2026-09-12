# Samsung .sbu Extractor

A free Windows desktop app that recovers photos and videos from old Samsung phone backup files (`.sbu`).

No installation required — just download and run.

## Download

**[⬇ Download SamsungSBUExtractor.exe](https://github.com/msaiproducts/samsung-sbu-extractor/releases/latest/download/SamsungSBUExtractor.exe)**

Windows 10/11 64-bit. No Python or other software needed.

> **Note:** Windows may show a SmartScreen warning ("Windows protected your PC"). Click **More info → Run anyway**. This is expected for unsigned apps.

## What it extracts

| Type | Format |
|------|--------|
| Photos | `.jpg` |
| Videos | `.mp4` / `.3gp` |

## How it works

Samsung `.sbu` files are proprietary binary containers. The app scans the file for JPEG and MP4/3GP signatures and carves the media out directly — no Samsung software needed.

Large backups (1–2 GB) are handled efficiently using memory-mapped I/O.

## How to use

1. Click **Browse…** and select your `.sbu` backup file
2. Choose an output folder (or use the auto-suggested one)
3. Click **Extract**
4. Find your photos in `output/photos/` and videos in `output/videos/`

## Build from source

Requires Python 3.10+ on Windows.

```
pip install pyinstaller
pyinstaller --onefile --windowed --name "Samsung SBU Extractor" sbu_extractor_gui.py
```

The `.exe` will be in the `dist/` folder.

## Problems or questions?

[Open an issue](https://github.com/msaiproducts/samsung-sbu-extractor/issues) — I respond quickly.

## License

MIT
