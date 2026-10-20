# Camera Wall

Live view of Dahua IP cameras in a grid. Plays each camera's RTSP stream directly (no NVR needed),
reconnects automatically, and fits the screen.

Two apps, one configuration file, one video engine (PyAV/FFmpeg, in the `camera_wall/` package):

- **Desktop (Linux):** `camera_wall.py`, a PyQt6 window.
- **Android:** a Kivy app, built into an APK for Android TV, Google TV / Chromecast, ChromeOS,
  phones and tablets.

Both decode on the video hardware when there is some (NVIDIA or Intel graphics on a PC,
MediaCodec on Android) and fall back to the processor by themselves.

---

## Desktop (Linux)

### Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python camera_wall.py
```

The first time, there's no configuration yet: the app writes an example to
`~/.camera_wall/config.json` and tells you so. Put your cameras in it and start it again.

The window opens at 90% of the screen it starts on. Each video is scaled into its cell, so you can
maximize the window, move it to another screen or resize it freely.

### Graphics card decoding

With an NVIDIA graphics card (and its driver), the GPU decodes the streams *and* scales each one
down to its cell, so the processor hardly works: six 2560×1440 H.265 cameras take about 0.4 of a
core instead of almost 5. Without one, Intel Quick Sync is tried (it needs Intel's oneVPL GPU
runtime, `sudo apt install libmfx-gen1.2`), then the processor. The terminal log says which one
each camera uses:

```
Camera 1: connected, hevc 2560x1440, hardware decoding (hevc_cuvid, scaled on the GPU to 1098x618)
```

To decode on the processor only, set `"hardware_decoders": 0` in the config.

### Single-file build (PyInstaller)

```bash
.venv/bin/pip install pyinstaller
.venv/bin/pyinstaller --name camera-wall --windowed --onefile camera_wall.py
./dist/camera-wall
```

The build reads the same configuration file; nothing about your cameras is built into it.

---

## Android (TV, Google TV, ChromeOS, phones)

### Build the APK

On a Linux (x86_64) computer with about 20 GB of free disk:

```bash
sudo apt install openjdk-17-jdk git zip unzip autoconf automake libtool pkg-config cmake \
    build-essential libffi-dev libssl-dev zlib1g-dev
./build_android.sh
```

The script installs everything else it needs (buildozer into `.venv`; the Android SDK, NDK r30
and python-for-android into `~/.buildozer`). The first build compiles Python, Kivy, NumPy, FFmpeg
and PyAV for two processor types and takes a while; later builds take a minute or two. The APK
lands in `bin/`, e.g. `bin/camerawall-0.2.0-arm64-v8a_armeabi-v7a-debug.apk`.

One APK runs on 64-bit and 32-bit ARM devices (many TV boxes and the Chromecast with Google TV are
32-bit), from Android 7.0 up. Intel/AMD Chromebooks run it through ARM translation; for native
speed there, add `x86_64` to `android.archs` in `buildozer.spec`.

### Build your cameras into the APK (optional)

To have the app start straight into your wall, with no config to push, build your config file
into it:

```bash
./build_android.sh --config ~/.camera_wall/config.json
```

This makes `bin/camerawall-…-debug-with-config.apk`: the same grid and cameras as the desktop app,
filling the TV screen. The file is checked first, and only lives in the source tree during the
build. A config pushed later with `push_config.sh` still takes over, without a rebuild; remove it
(`adb shell run-as org.camerawall.camerawall rm files/config.json`) to go back to the built-in one.

That APK contains your camera passwords in plain text (anyone with the APK file can read them,
and on the device so can other installed apps, since APK files are readable by all apps). Keep it
to yourself; `bin/` is not committed. Without `--config`, the APK holds no camera details.

Without `--config`, use the debug APK: it's what `push_config.sh` (below) needs to hand the app
its configuration. `./build_android.sh release` builds a release APK (signed when the
`P4A_RELEASE_KEYSTORE`, `P4A_RELEASE_KEYSTORE_PASSWD`, `P4A_RELEASE_KEYALIAS` and
`P4A_RELEASE_KEYALIAS_PASSWD` variables are set); on Android 11 and later adb can't give a release
build a config file, so build one in with `--config`.

### Install it on the TV

1. On the TV, turn on developer options (Settings > Device Preferences or System > About >
   click the *build* entry seven times), then *USB debugging* or *Network debugging* in
   Developer options.
2. From the computer (adb is in `~/.buildozer/android/platform/android-sdk/platform-tools`, or
   install `adb` from your distribution):

   ```bash
   adb connect <TV address>        # network debugging; skip for USB
   adb install -r bin/camerawall-*-debug.apk
   ```

3. Give it your cameras (skip this with an APK built with `--config`). The app has no
   remote-friendly settings screen; it reads the same
   `config.json` as the desktop app (use the sub streams, see below):

   ```bash
   ./push_config.sh path/to/config.json
   ```

   The script checks the file, then copies it over adb into the app's private storage, where no
   other app can read the passwords (`adb shell run-as`, which works on every Android version with
   the debug APK; adb can't write to the app's shared-storage folder on Android 11 and later).
   Until the app has a config it shows these steps. It starts the wall as soon as the file
   arrives, and reloads whenever you push a new one; no restart needed.

The app appears on the TV home screen (*Camera Wall*). The screen stays on while it runs, Back
closes it, and the cameras pause while it's in the background.

### Use the sub stream on Android (H.264)

TV processors are slow: one that plays 4K Netflix may manage only a couple of camera streams in
software. The app does three things about it:

- **Hardware decoding.** It asks Android how many videos the device's video hardware decodes at
  once and gives those to the cameras (main streams first). A camera whose hardware decoder
  doesn't work switches to software by itself. Set `"hardware_decoders": N` at the top of the
  config to use at most N (0 turns it off), e.g. when the device claims more than really work.
- **Stays live.** A camera that falls more than 1.5 s behind skips ahead to its next keyframe
  instead of showing an ever older picture.
- **Lean software decoding** when there are more cameras than processor cores.

It still pays to send the TV light streams. Set `"stream": "sub"` for every camera in the TV's
config, and on each camera (web interface: Setup > Camera > Video > Sub Stream) use:

| Setting | Value |
|---|---|
| Compression | H.264 (H.265 is much heavier to decode; no H.264+/smart codec) |
| Resolution | D1 (704×576), the largest a Dahua sub stream offers; more than a TV cell shows |
| Frame rate | 15 (20 if the TV keeps up) |
| Bit rate | CBR, about 1024 kb/s |
| Audio (under "More") | off: the camera wall has no sound |
| I-frame interval | twice the frame rate, e.g. 30 |
| Profile | High (or Main) |

Some web interfaces don't show the I-frame interval and profile of the sub stream; the camera's
HTTP API sets them (curl asks for the password):

```bash
curl --digest -u admin "http://CAMERA-IP/cgi-bin/configManager.cgi?action=setConfig&Encode[0].ExtraFormat[0].Video.GOP=30&Encode[0].ExtraFormat[0].Video.Profile=High"
```

The desktop app uses the main streams, so these settings don't affect it. A camera without a
sub stream shows *"Error — no stream (is this stream enabled on the camera?)"*.

D1 (704×576) is nearly square, but a widescreen camera squeezes its whole 16:9 picture into it
without saying so, so the picture looks too narrow. Tell the app the real shape with
`"aspect_ratio": "16:9"` at the top of the config (all cameras) or in a camera's entry (just that
one; `"auto"` = as the stream says).

### Run the Android app on the desktop

The Kivy app also runs on a desktop, which is the quickest way to try changes before building an
APK. It uses the same `config.json` lookup as the desktop app.

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m camera_wall                  # or: --config FILE --grid 3x3
.venv/bin/python -m pytest                       # tests for the config and stream helpers
```

### See what the app does (logs)

The app keeps its log in its private storage (TV log buffers are too small and busy for
`adb logcat` to hold it for long):

```bash
adb shell run-as org.camerawall.camerawall cat files/camera_wall.log
```

It shows how each camera is decoded (hardware or software), connection problems and catch-up
skips. Passwords are masked.

---

## Configuration

```json
{
  "grid": "2x3",
  "cameras": [
    {"name": "Main door", "ip": "192.168.1.101", "username": "admin", "password": "CHANGE_ME", "stream": "main"},
    {"name": "Parking",   "ip": "192.168.1.102", "username": "admin", "password": "CHANGE_ME", "stream": "main"}
  ]
}
```

[`config.example.json`](config.example.json) has a complete example with six cameras.

The apps look for the file in this order:

1. the path you give with `--config`, e.g. `camera_wall.py --config ~/office-cameras.json`;
2. the `CAMERA_WALL_CONFIG` environment variable;
3. `~/.camera_wall/config.json` on a desktop, or the app's private storage on Android
   (`/data/data/org.camerawall.camerawall/files/config.json`, written by `push_config.sh`);
4. on Android, then the configuration built into the APK with `build_android.sh --config`.

It holds your cameras' passwords, so keep it out of git (`config.json` is in `.gitignore`) and
readable only by you: `chmod 600 ~/.camera_wall/config.json`. The APK only contains it if you
build it in (`--config`), and the Android app opts out of cloud backup so the file isn't uploaded
anywhere. To check a file without starting anything: `python3 -m camera_wall --check FILE`.

### Settings for the whole wall

| Key | | Default |
|---|---|---|
| `grid` | Rows x columns, see below | `2x3` |
| `aspect_ratio` | The pictures' real shape, e.g. `"16:9"`, for sub streams that squeeze it | as each stream says |
| `hardware_decoders` | At most this many cameras on the video hardware; `0` turns hardware decoding off | as many as the device allows (all on a PC) |

### Adding a camera

Add an entry to `cameras`. Cameras fill the grid left to right, top to bottom, in the order of the
list.

| Field | | Default |
|---|---|---|
| `ip` | The camera's address (required) | |
| `name` | Shown under the video | `Camera N` |
| `username`, `password` | The camera's login | `admin`, empty |
| `stream` | `main` (full quality) or `sub` (lighter: use it for many cameras, a slow network or Android) | `main` |
| `channel` | The channel, for a camera or encoder with several | `1` |
| `port` | The RTSP port | `554` |
| `aspect_ratio` | This camera's real picture shape, overriding the wall's | the wall's |

### Changing the grid

Set `grid` to `rows x columns`, from `1x1` to `8x8`:

| `grid` | Cameras |
|---|---|
| `2x3` | 6 (the default) |
| `3x3` | 9 |
| `3x4` | 12 |
| `4x4` | 16 |

With more cameras than cells, the first ones are shown; empty cells say "No camera". To try a
grid without editing the file: `camera_wall.py --grid 3x3`.

More cameras at full quality take more network and CPU. If the video stutters, switch some
cameras to `"stream": "sub"`.

---

## Status

The border and the line under each video show its state: grey while connecting, green when
connected, orange while reconnecting, red when it keeps failing (check the camera's address,
login and RTSP settings). The status bar counts the connected cameras.

---

## Troubleshooting

- **No video:** check that the camera answers (`ping 192.168.1.101`) and that RTSP is on in its
  settings.
- **"Error — check the camera"** (desktop) or a more specific error (Android): the address,
  login or stream is wrong, or the camera allows no more RTSP connections (close other viewers).
  Avoid retrying a wrong password many times: Dahua cameras lock the account for a while.
- **Stuttering:** use `"stream": "sub"` for some cameras (on Android, for all of them).
- **High CPU on the PC:** check the log for `hardware decoding (hevc_cuvid …)`; if it says
  software, the NVIDIA driver (or Intel's runtime, see above) isn't available to FFmpeg.
- **Logs:** run the desktop app from a terminal; on Android, read
  `adb shell run-as org.camerawall.camerawall cat files/camera_wall.log` (passwords are masked).
- **Android video lags or stutters:** use H.264 sub streams at 15 fps (see above); the log shows
  whether cameras run on hardware decoding and how often they skip ahead to stay live.
- **Android pictures look squeezed (too narrow):** add `"aspect_ratio": "16:9"` to the config.
- **The app isn't on the TV home screen:** look in Settings > Apps, or start it with
  `adb shell am start -n org.camerawall.camerawall/org.kivy.android.PythonActivity`.
- **Android build fails:** the build needs a full JDK (with `javac`, not only `java`); the script
  picks one from `/usr/lib/jvm`, or set `JAVA_HOME`. Run with `buildozer.spec`'s `log_level = 2`
  (the default here) and look for the first error. `./build_android.sh clean` starts over.

---

## License

MIT
