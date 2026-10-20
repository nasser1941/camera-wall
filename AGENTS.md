# Camera Wall — Agent Guide

## Overview
Live RTSP camera grid viewer for Dahua IP cameras. Two apps, one config format, one video engine:
- **Desktop (Linux):** PyQt6 window — `camera_wall.py`, using the package's config + reader
- **Android (TV / Google TV / ChromeOS / phones):** Kivy UI in the package `camera_wall/`, built with buildozer
- **Shared (`camera_wall/` package):** config, PyAV reader (hardware decoding, skip-to-live, scaling)

Config holds camera IPs, credentials and grid layout. Streams auto-reconnect.

## Setup & Run

### Desktop app
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python camera_wall.py
```

### Android app on the desktop (development) and tests
```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m camera_wall [--config FILE] [--grid 3x3]
.venv/bin/python -m pytest
```

### Android APK
```bash
./build_android.sh            # debug APK -> bin/
./build_android.sh release    # release APK (signed if P4A_RELEASE_* env vars are set)
./build_android.sh clean      # drop built recipes/dists
./build_android.sh --config ~/.camera_wall/config.json   # APK with that config built in (-with-config.apk)
adb install -r bin/*.apk
./push_config.sh config.json  # validates, then writes it into the app's private storage (run-as)
adb shell run-as org.camerawall.camerawall cat files/camera_wall.log   # app log (TV logcat buffers are tiny)
```
Build host: Linux x86_64, full JDK 17+ (javac — the script picks one from /usr/lib/jvm), ~20 GB free
disk for a first build. SDK, NDK r30 and python-for-android live in `~/.buildozer`.

**Config lookup** (`config.config_candidates`, first existing wins, re-resolved every 2 s by the app):
`--config` alone → `CAMERA_WALL_CONFIG` alone → `app_dir()/config.json` (desktop `~/.camera_wall`, Android
`getFilesDir()` = `/data/data/org.camerawall.camerawall/files/`, private) → `camera_wall/bundled_config.json`
(only in APKs built with `build_android.sh --config FILE`; the script copies it in for the build, removes it
via trap, buildozer.spec lets exactly that one .json in). `python3 -m camera_wall --check FILE` validates
without Kivy (used by build_android.sh and push_config.sh).
Android config delivery = `push_config.sh` → `adb shell "run-as <pkg> sh -c '… cat > files/config.json.tmp && mv …'" < file`.
Needs the **debug** APK (run-as). Don't switch to `getExternalFilesDir`/`Android/data`: adb can't write
there from Android 11 on (verified on an API 30 emulator).

## Architecture

```
camera-wall/
├── camera_wall.py              # Desktop app (PyQt6 window; imports the camera_wall package)
├── camera_wall/                # Android app (Kivy + PyAV)
│   ├── __init__.py             # __version__ (buildozer reads the APK version from here)
│   ├── __main__.py             # entry: env/logging/Kivy config, then CameraWallApp
│   ├── config.py               # config path/loading/validation, is_android() — no Kivy, unit-tested
│   ├── camera_reader.py        # RTSP URL, CameraReader thread (PyAV decode + scale, MediaCodec, skip-to-live)
│   ├── hardware.py             # Android MediaCodec instance limit (jnius), which cameras get HW decoders
│   ├── kivy_panel.py           # CameraPanel: Image + status label + coloured border
│   └── kivy_app.py             # CameraWall grid, MessageView, CameraWallApp (config watch, pause/resume)
├── main.py                     # buildozer entry point (calls camera_wall.__main__.main)
├── tests/                      # pytest: config, camera_reader helpers, hardware decoder plan
├── buildozer.spec              # APK config (min API 24, target 35, arm64-v8a + armeabi-v7a, p4a pinned)
├── p4a-recipes/av/             # local PyAV 16.1.0 recipe (p4a's bundled one can't build with FFmpeg 8)
├── android-res/                # icon, presplash, TV banner, Leanback manifest snippets
├── build_android.sh            # build script (JDK pick, venv + buildozer, p4a workarounds, buildozer android ...)
├── push_config.sh              # validate a config.json and copy it into the app over adb (run-as)
├── requirements.txt            # desktop app
├── requirements-android.txt    # Kivy app on desktop (same versions as the APK)
├── requirements-dev.txt        # + pytest
├── requirements-build.txt      # buildozer, Cython
└── config.example.json         # 6-camera example
```

### Key design points (Android app)
- **Decoding is PyAV, not OpenCV:** python-for-android builds OpenCV without FFmpeg, so
  `cv2.VideoCapture("rtsp://…")` can't work on Android. PyAV opens RTSP over TCP, decodes H.264/H.265,
  and scales + converts to RGB in one `frame.reformat()` (GIL released).
- **Threading:** one `CameraReader` thread per camera. The UI thread *polls* `latest_frame()` / `status`
  at 30 Hz from `CameraWall._refresh` — no Kivy calls from background threads, frames dropped naturally.
- **Frames are pre-scaled** in the reader to the cell's size (`set_output_size`, never enlarged) to keep
  texture uploads small on TV hardware.
- **Hardware decoder order** (`HARDWARE_DECODERS`): `*_mediacodec` (Android) → `*_cuvid` (NVIDIA NVDEC) →
  `*_qsv` (Intel); each FFmpeg build has only its platform's (PyAV desktop wheels: cuvid, qsv). cuvid also
  scales on the GPU (`resize` option = the cell size, `GPU_SCALING`); the decoder is re-made at the next
  keyframe when the cell size changes >10%. A decoder failing before its first picture (FFmpeg error, or
  none after `HARDWARE_PATIENCE` packets) hands over to the next candidate at the next keyframe, same
  connection. `_decoder_results`: a decoder that failed twice and never worked is skipped process-wide.
  Desktop (this PC, 6× 2560x1440 HEVC): OpenCV app 482% CPU → cuvid + GPU scaling 42%.
  Intel QSV here fails ("Function not implemented"): Raptor Lake needs `libmfx-gen1.2`.
- **Hardware decoding (Android):** FFmpeg's `h264_mediacodec`/`hevc_mediacodec`, created with
  `av.CodecContext.create(name, "r")` + the stream's extradata/size. No JavaVM is registered, so FFmpeg uses
  the NDK AMediaCodec API (libmediandk) — no JNI needed. `hardware.hardware_decoder_limit()` reads
  `getMaxSupportedInstances()`; `plan_decoders` gives slots to main streams first; config
  `"hardware_decoders"` caps it. No picture after `HARDWARE_PATIENCE` packets or an FFmpeg error before the
  first picture → that reader switches to software for good. Drop the decoder reference promptly (devices
  have few). Tested on a TCL Google TV (Realtek RTD6748, 32-bit): reports 6, 5 work.
- **Skip-to-live:** `LagTracker` measures lag vs the freshest packet (arrival time − media time); over
  `MAX_LAG` the reader drops packets undecoded until a keyframe with lag ≤ `CATCH_UP_LAG`.
- **Aspect ratio:** Dahua D1 sub streams (704x576) are anamorphic 16:9 with no SAR. Config `aspect_ratio`
  (top level and/or per camera; `config.parse_aspect_ratio`) → reader's `fit_size(..., aspect)` scales to the
  true shape; else `stream_aspect()` uses the stream's SAR if it has one.
- **Android log file:** `files/camera_wall.log` (RotatingFileHandler, set up in `__main__.log_to_file`).
- **No config → instructions screen**; the app polls the file's mtime/size every 2 s and rebuilds the
  wall on change. `on_pause` stops readers, `on_resume` rebuilds.
- Don't name the app's config attribute `config`: `App.config` is Kivy's ConfigParser.
- Set `KIVY_NO_ARGS=1` before importing Kivy (Kivy's own `-c/--config` options clash with ours).

## Conventions & Gotchas
- **Config passwords are sensitive.** `config.json` is gitignored and never reaches the APK unless the user
  asks (`--config`): `buildozer.spec` ships `*.py` plus only `camera_wall/bundled_config.json`, and excludes
  `camera_wall.py`, `*.json`, `tests/`, `p4a-recipes/`, `android-res/`.
  Logs go through `redact_password()`. `android.allow_backup = False`.
- **Don't test wrong passwords against real Dahua cameras** — they lock the account.
- **Grid parsing** accepts `2x3`, `2×3`, `2 X 3`. Config key can be `grid` or `grid_layout`.
- **RTSP URL:** `rtsp://{user}:{pass}@{ip}:{port}/cam/realmonitor?channel={ch}&subtype={st}&unicast=true&proto=Onvif`
  (subtype 0 = main, 1 = sub). Credentials are URL-quoted.
- **Desktop app = same reader:** the Qt panel polls `latest_frame()` every 33 ms and shows the frame as
  is (already scaled to the label's physical pixels, `devicePixelRatioF`); no OpenCV any more.
  `python camera_wall.py` imports the *package* `camera_wall` (a package beats a same-named module).
- **Android performance:** use `"stream": "sub"` on TV boxes (main streams are often 4 MP H.265), set to
  H.264, D1, 15 fps, CBR ~1024 kb/s, GOP 2×fps; plus `"aspect_ratio": "16:9"` for D1. TV CPUs (e.g. TCL
  Google TV, 4× ARM, 32-bit userland) can't decode even 5 D1 H.265 streams in software in real time.
- **Android TV launcher:** Leanback intent filter + `android:banner` come from `android-res/*.xml` via
  `android.manifest.intent_filters`, `android.extra_manifest_*` and `android.add_resources`.
- **p4a v2026.05.09 workarounds:** `charset-normalizer<3.5` in requirements (p4a mistakes its Android
  wheels for pure Python); `build_android.sh` sets `PIP_CONSTRAINT` (pip==25.3) and deletes
  `build/venv` (p4a re-creates that venv per arch over the old one and mixes two pips).
- **Testing on an emulator:** ARM translation in the API 30 x86_64 image SIGILLs in this app's native
  code, so build `APP_ANDROID_ARCHS=x86_64 ./build_android.sh` for an x86_64 emulator instead.
- **p4a is pinned** (`p4a.commit = v2026.05.09`). Its ffmpeg recipe enables all native decoders,
  demuxers and protocols (needed: hevc, h264, rtsp); the app logs an error at startup if any is missing.
  Re-check this, and `p4a-recipes/av`, when bumping p4a.
- Built `.so` files are named `*.cpython-314-x86_64-linux-gnu.so` in p4a's build dir; p4a renames them
  to `*.so` when bundling — that's expected.
- **`.gitignore`** excludes `config.json`, `*.config.json`, `camera_wall/bundled_config.json`,
  `client_secret*.json`, keystores, `.buildozer/`, `bin/`, `dist/`, `*.apk`, `.venv/`, NDK downloads;
  `*.spec` is ignored except `buildozer.spec`.
- No pyproject.toml / setup.py: requirements files + `buildozer.spec`.
