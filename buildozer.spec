[app]

# (str) Title of your application
title = Camera Wall

# (str) Package name and domain: the app id is org.camerawall.camerawall
package.name = camerawall
package.domain = org.camerawall

# (str) Source code where the main.py live
source.dir = .

# Ship only the Python app: main.py and the camera_wall/ package. Everything else in this
# folder stays out of the APK — above all config.json (camera passwords), camera_wall.py
# (the desktop app) and any SDK/NDK or virtualenv that happens to sit here.
# Hidden folders (.venv, .buildozer, .git, .idea) are always skipped by buildozer.
# The one JSON file let through is camera_wall/bundled_config.json, which exists only while
# `./build_android.sh --config FILE` builds an APK with a default configuration built in.
source.include_exts = py, json
source.exclude_dirs = tests, p4a-recipes, android-res, bin, build, dist, release
source.exclude_patterns = camera_wall.py, *.json
source.include_patterns = camera_wall/bundled_config.json

# (str) Application versioning: read from camera_wall/__init__.py
version.regex = __version__ = ['"](.*)['"]
version.filename = %(source.dir)s/camera_wall/__init__.py

# (list) Application requirements
# av is PyAV (FFmpeg): it plays the RTSP streams and decodes H.264/H.265. OpenCV is not used
# on Android because python-for-android builds it without FFmpeg, so it can't open RTSP.
# av comes from the local recipe in p4a-recipes/ (see p4a.local_recipes below).
# charset-normalizer (pulled in through Kivy's recipe -> requests) is held below 3.5, the first release
# with Android wheels: python-for-android v2026.05.09 mistakes those compiled wheels for pure
# Python and its host pip then refuses to install them ("not a supported wheel on this platform").
requirements = python3,kivy,av,numpy,charset-normalizer<3.5

# (str) Presplash and icon of the application
presplash.filename = %(source.dir)s/android-res/presplash.png
icon.filename = %(source.dir)s/android-res/icon.png

# (list) Supported orientations
orientation = landscape

# (bool) Indicate if the application should be fullscreen or not
fullscreen = 1

#
# Android specific
#

# (string) Presplash background color
android.presplash_color = #101418

# (list) Permissions: only network access. The config lives in the app's own external files
# folder, which needs no storage permission.
android.permissions = INTERNET, ACCESS_NETWORK_STATE

# (int) Target Android API
android.api = 35

# (int) Minimum API your APK / AAB will support. 24 = Android 7.0, which covers practically
# every Android TV box, Google TV / Chromecast and ChromeOS device in use.
android.minapi = 24

# (int) Android NDK API to use. Must not be higher than android.minapi.
android.ndk_api = 24

# (str) Android NDK version. Buildozer keeps it in ~/.buildozer/android/platform/android-ndk-r30
# and downloads it there if it is missing.
android.ndk = 30

# (bool) Accept the SDK license agreements automatically (needed for unattended builds)
android.accept_sdk_license = True

# (list) The Android archs to build for. Many TV devices (Chromecast with Google TV, most
# Android TV boxes) run a 32-bit userland, so armeabi-v7a matters as much as arm64-v8a.
# Add x86_64 for Intel/AMD Chromebooks if you want native speed there.
android.archs = arm64-v8a, armeabi-v7a

# (bool) Keep the screen on while the app runs (no screensaver over the cameras)
android.wakelock = True

# (bool) No cloud backup: the config file holds camera passwords.
android.allow_backup = False

# Android TV: list the app on the TV home screen (Leanback launcher) with a banner, and don't
# require a touchscreen, so the same APK runs on TVs, phones, tablets and Chromebooks.
android.manifest.intent_filters = android-res/intent_filters.xml
android.extra_manifest_xml = android-res/extra_manifest.xml
android.extra_manifest_application_arguments = android-res/application_args.xml
android.add_resources = android-res/tv_banner.png:drawable-xhdpi/tv_banner.png

# (str) Android logcat filters to use
android.logcat_filters = *:S python:D

# (str) The format used to package the app for release mode (aab or apk).
# APK, because the app is sideloaded (adb install), not published on Google Play.
android.release_artifact = apk

#
# Python for android (p4a) specific
#

# (str) python-for-android release to build with, pinned so that builds are reproducible.
# The local recipes below are written against this release.
p4a.branch = master
p4a.commit = v2026.05.09

# (str) The directory in which python-for-android should look for your own build recipes
p4a.local_recipes = ./p4a-recipes

# (str) Bootstrap to use for android builds
p4a.bootstrap = sdl2


[buildozer]

# (int) Log level (0 = error only, 1 = info, 2 = debug (with command output))
log_level = 2

# (int) Display warning if buildozer is run as root (0 = False, 1 = True)
warn_on_root = 1

# (str) Path to build output (i.e. .apk, .aab, .ipa) storage
bin_dir = ./bin
