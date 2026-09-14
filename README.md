# mvdl — HLS video download manager

A small **local** desktop app that downloads video from HLS-based sites (free
broadcasters and streaming sites) as **.mp4** for offline viewing.
You paste a **page URL**, the app finds the stream itself. Works on
**Windows, macOS and Linux**. UI in **English (default) or Turkish**.

> ⚠️ For DRM-free, freely available content only (personal / offline use). It
> detects and refuses DRM-protected services (Netflix, Disney+, …). Not a
> YouTube downloader — use yt-dlp for that.

---

## 1. Install (one time)

Needs **Python 3.8+** ([python.org](https://www.python.org/downloads/); on
Windows keep "Add python.exe to PATH" checked). Then run the setup once — it
installs Playwright, a bundled Chromium (for auto-detection) and ffmpeg:

| OS | Setup |
|----|-------|
| **Windows** | double-click **`setup.bat`** |
| **macOS** | double-click **`setup.command`** |
| **Linux** | `./setup.sh` |

(Equivalent everywhere: `python setup.py` / `py setup.py`.)

## 2. Run

| OS | Start | Notes |
|----|-------|-------|
| **Windows** | double-click **`mvdl.vbs`** | opens as its own window, **no terminal** |
| **macOS** | double-click **`mvdl.command`** | |
| **Linux** | `./mvdl.sh` | |

The app opens in its **own window** (like a normal app). **Closing the window
quits the app** — nothing keeps running in the background. On Windows, if
`mvdl.vbs` does nothing, run `baslat.bat` to see the error message.

## 3. Use

1. **＋ New download** → paste the episode/video **page URL** (a direct `.m3u8`
   also works).
2. **Detect** → the app opens the page in a hidden browser and captures the
   stream (~20–40 s). If several streams exist, it asks which one.
3. Pick **quality / folder / range** → **Start Download**.
   - *Range* lets you skip the part you already watched (start–end mm:ss).
4. Watch live progress in the queue; when done, **Show in folder** / **Open**.
   Click a card to expand full details.

---

## Features

- **Auto-detect** the m3u8 from a page URL — no DevTools/Network digging
- Always **.mp4** (bundled ffmpeg)
- **Parallel** downloads with **adaptive throttle protection** (auto-slows on
  429/403, speeds back up)
- **Resume** interrupted downloads; **pause / resume / cancel**
- **Multiple** simultaneous downloads (a global queue)
- **Range** download (skip the watched part) — never overwrites: same name gets
  ` (2)`, ranges get a distinct name
- **Auto-reconnect**: if a stream link expires mid-download, it re-detects a
  fresh one and resumes from where it left off
- **Subtitles**: when the stream has them, pick a language — it's saved as a
  `.srt` next to the video and embedded into the `.mp4`
- **English / Turkish** UI (top-right toggle, remembered)
- Choose the download folder; per-download connection count

## Settings (⚙)

- Default download folder
- **Max simultaneous downloads** — *global* total across all sites, not per site
- Default **connections** per download — 8–16 is ideal; higher rarely helps and
  raises the throttle (429) risk (the app auto-reduces when throttled)

---

## How it works

| File | Role |
|------|------|
| `app.py` | Local web server + job manager (backend). Also serves the UI. |
| `web/index.html` | The UI (served by `app.py`) |
| `engine.py` | Download engine: parallel, adaptive, resume, range, ffmpeg→mp4 |
| `detector.py` | Auto-detects the m3u8 (headless Chromium via Playwright) |
| `hlsdl.py` | Standalone command-line downloader |
| `setup.py` | One-time dependency installer |

The UI is a local page served by `app.py` at `http://127.0.0.1:8756`; the
launchers open it in an app window. Everything runs locally on your machine.

## Dependencies (installed by setup)

- [Playwright](https://playwright.dev/python/) — drives a headless Chromium to
  find the stream (uses system Edge/Chrome if present, else its own Chromium)
- [imageio-ffmpeg](https://pypi.org/project/imageio-ffmpeg/) — bundles ffmpeg
  cross-platform (or put an `ffmpeg` binary in `bin/`, or on PATH)

## Troubleshooting

- **Detection finds nothing** → the video didn't play, or the site uses a
  different method. Grab the `.m3u8` yourself (F12 → Network → filter `m3u8`)
  and paste it directly.
- **“Token expired”** → the stream link is time-limited; just re-add the page.
- **Output is `.ts` not `.mp4`** → ffmpeg wasn't found; run setup again.
- **Windows: `mvdl.vbs` does nothing** → run `baslat.bat` to see the error
  (usually Python not installed / not on PATH). Logs go to `mvdl.log`.

## Standalone app (no Python needed)

A **prebuilt Windows app** is attached to the
[latest Release](https://github.com/fignupafya/mvdl/releases): download the zip,
extract it, and run `mvdl.exe`. No Python, no setup.

**Rather build it yourself?** (e.g. you don't want to trust a prebuilt binary) —
you can produce the exact same app on **any platform**. Needs
`pip install pyinstaller` once, then:

| OS | Build | Result |
|----|-------|--------|
| Windows | `build.bat` (or `py build.py`) | `dist/mvdl/mvdl.exe` |
| macOS | `build.command` (or `python3 build.py`) | `dist/mvdl.app` |
| Linux | `./build.sh` (or `python3 build.py`) | `dist/mvdl/mvdl` |

The bundle includes Python, ffmpeg and Playwright and uses the system browser
(Edge/Chrome) for detection. To distribute, zip the whole `dist/mvdl` folder
(or the `.app`) — that's exactly what the Release contains.

## Command-line (optional)

```
python hlsdl.py "<m3u8-or-page-url>" -o out.mp4 --referer https://site/ -n 16
```

---

## Türkçe özet

HLS tabanlı sitelerden bölümleri **.mp4** olarak çevrimdışı izlemek
için yerel bir uygulama. **Kurulum:** `setup.bat` (Win) / `setup.command` (Mac) /
`./setup.sh` (Linux). **Başlatma:** `mvdl.vbs` (Win, terminalsiz) / `mvdl.command`
(Mac) / `./mvdl.sh` (Linux). Uygulama kendi penceresinde açılır, **pencereyi
kapatınca kapanır.** Sayfa adresini yapıştır → **Tespit Et** → kalite/klasör/aralık
seç → indir. Arayüz sağ üstten **EN/TR**. Yalnızca DRM'siz içerik içindir.

## License / Legal

For personal, offline use of DRM-free content only. Respect the terms of the
sites you use and applicable copyright law.
