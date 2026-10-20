#!/usr/bin/env bash
# Copy a camera wall config.json into the Android app, over adb.
#
# The file goes to the app's private storage (no other app can read the passwords in it), through
# `adb shell run-as`. That works on every Android version, but only with the debug APK, which is
# what ./build_android.sh builds by default. The app picks the new file up within a few seconds;
# no restart needed.
#
# Usage:
#   ./push_config.sh path/to/config.json
# With several devices connected, choose one: ANDROID_SERIAL=<serial or ip:port> ./push_config.sh ...
set -euo pipefail

PACKAGE=org.camerawall.camerawall
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ $# -ne 1 ]; then
    echo "usage: $0 path/to/config.json   (the file format is in config.example.json)" >&2
    exit 2
fi
CONFIG="$1"
if [ ! -f "$CONFIG" ]; then
    echo "ERROR: $CONFIG not found" >&2
    exit 1
fi

# Check the file the way the app will, so a typo shows up here and not on the TV.
PYTHONPATH="$PROJECT_DIR" python3 -m camera_wall --check "$CONFIG"

ADB="$(command -v adb || true)"
if [ -z "$ADB" ]; then
    ADB="$HOME/.buildozer/android/platform/android-sdk/platform-tools/adb"
fi
if [ ! -x "$ADB" ]; then
    echo "ERROR: adb not found. Install it (sudo apt install adb) or build the APK once." >&2
    exit 1
fi

# run-as fails (e.g. "package not debuggable" or "unknown package") when the debug APK isn't installed.
if ! "$ADB" shell "run-as $PACKAGE true"; then
    echo "ERROR: can't reach the app with run-as. Is the debug APK installed on this device," >&2
    echo "       and is the device connected (adb devices)?" >&2
    exit 1
fi

# Stream the file in and swap it into place in one step, so the app never reads half a file.
"$ADB" shell "run-as $PACKAGE sh -c 'umask 077 && mkdir -p files && cat > files/config.json.tmp && mv files/config.json.tmp files/config.json'" < "$CONFIG"
echo "Copied to the app. The camera wall (re)starts with it within a few seconds."
