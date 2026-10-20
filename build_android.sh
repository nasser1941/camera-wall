#!/usr/bin/env bash
# Build the Camera Wall APK for Android TV / Google TV, ChromeOS, phones and tablets.
#
# Build host: Linux x86_64 with
#   - a full JDK 17 or newer (java *and* javac):  sudo apt install openjdk-17-jdk
#   - the native build tools python-for-android needs:
#       sudo apt install git zip unzip autoconf automake libtool pkg-config cmake \
#           build-essential libffi-dev libssl-dev zlib1g-dev
#   - about 20 GB of free disk for the first build (later builds reuse it)
# Buildozer, the Android SDK, NDK r30 and python-for-android are installed on demand
# (into .venv and ~/.buildozer).
#
# Usage:
#   ./build_android.sh [debug|release|clean] [--config FILE]
#
#   debug (default)   debug APK, ready to install (adb install bin/<name>.apk);
#                     push_config.sh can hand it a config later (it needs a debug build)
#   release           release APK; signed if the P4A_RELEASE_* variables are set
#   clean             forget built dependencies; the next build starts over
#   --config FILE     build FILE into the APK as its default configuration, so the app starts
#                     with your cameras right away. A config pushed later still takes over.
#                     The APK then contains the camera passwords: keep it to yourself.
set -euo pipefail

usage() {
    echo "usage: $0 [debug|release|clean] [--config FILE]" >&2
    exit 2
}

MODE=debug
BUNDLE_CONFIG=""
while [ $# -gt 0 ]; do
    case "$1" in
        debug | release | clean) MODE="$1" ;;
        --config)
            [ $# -ge 2 ] || usage
            BUNDLE_CONFIG="$2"
            shift
            ;;
        --config=*) BUNDLE_CONFIG="${1#--config=}" ;;
        *) usage ;;
    esac
    shift
done
if [ -n "$BUNDLE_CONFIG" ]; then
    # Resolve before changing directory, so a path relative to where you are works.
    BUNDLE_CONFIG="$(realpath -e -- "$BUNDLE_CONFIG")" || { echo "ERROR: config file not found" >&2; exit 1; }
fi

cd "$(dirname "$0")"

# The Android Gradle plugin needs a full JDK (javac too). Pick one unless JAVA_HOME is set:
# the default `java` may be a runtime only, which is a common reason for Gradle failures.
if [ -z "${JAVA_HOME:-}" ]; then
    for jdk in /usr/lib/jvm/java-17-openjdk-* /usr/lib/jvm/java-21-openjdk-* /usr/lib/jvm/*; do
        if [ -x "$jdk/bin/javac" ]; then
            JAVA_HOME="$jdk"
            break
        fi
    done
fi
if [ -z "${JAVA_HOME:-}" ] || [ ! -x "$JAVA_HOME/bin/javac" ]; then
    echo "ERROR: no JDK with javac found. Install one: sudo apt install openjdk-17-jdk" >&2
    exit 1
fi
export JAVA_HOME
export PATH="$JAVA_HOME/bin:$PATH"
echo "Using JDK: $JAVA_HOME"

# Buildozer and Cython, in the project's virtualenv. Activate it (not just PATH): buildozer
# installs python-for-android's own dependencies with `pip --user` unless VIRTUAL_ENV is set.
if [ ! -x .venv/bin/python ]; then
    echo "Creating .venv..."
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q -r requirements-build.txt

if [ "$MODE" = clean ]; then
    buildozer android clean
    exit 0
fi

# python-for-android (v2026.05.09) installs pure-Python packages from a helper venv that it
# re-creates for each architecture *over* the previous one, after upgrading its pip. With pip 26.2+
# the bundled pip then lands on top of the upgraded one and the venv's pip breaks
# ("cannot import name 'BuildDependencyInstallError'"), failing the second architecture.
# Keep that pip at the version bundled with p4a's Python 3.14.2 so the "upgrade" is a no-op, and
# start each build from a fresh venv. Only pip itself is constrained.
mkdir -p .buildozer
echo "pip==25.3" > .buildozer/pip-constraints.txt
export PIP_CONSTRAINT="$PWD/.buildozer/pip-constraints.txt"
rm -rf .buildozer/android/platform/build-*/build/venv

# The default configuration to build in (see --config). buildozer.spec lets exactly this file
# into the APK. Remove any leftover first, so a plain build never ships one by accident, and
# remove ours when the script ends, however it ends.
BUNDLED=camera_wall/bundled_config.json
rm -f "$BUNDLED"
if [ -n "$BUNDLE_CONFIG" ]; then
    python3 -m camera_wall --check "$BUNDLE_CONFIG"
    trap 'rm -f "$BUNDLED"' EXIT
    install -m 600 "$BUNDLE_CONFIG" "$BUNDLED"
    echo "Building in the configuration from $BUNDLE_CONFIG"
fi

echo "=== Building Camera Wall APK (${MODE}) ==="
buildozer android "$MODE"

APK=$(ls -t bin/*.apk 2>/dev/null | head -n 1 || true)
if [ -z "$APK" ]; then
    echo "ERROR: no APK in bin/. See the buildozer output above." >&2
    exit 1
fi
if [ -n "$BUNDLE_CONFIG" ]; then
    mv -f "$APK" "${APK%.apk}-with-config.apk"
    APK="${APK%.apk}-with-config.apk"
fi

echo
echo "=== APK built: $APK ==="
echo
echo "Install it (TV with network debugging on: adb connect <tv-ip> first):"
echo "  adb install -r \"$APK\""
echo
if [ -n "$BUNDLE_CONFIG" ]; then
    echo "It starts with the cameras from $BUNDLE_CONFIG."
    echo "This APK contains their passwords: don't share it. To change the cameras later"
    echo "without rebuilding: ./push_config.sh path/to/config.json"
else
    echo "Then give it your cameras (the app shows these steps until it has a config):"
    echo "  ./push_config.sh path/to/config.json"
fi
