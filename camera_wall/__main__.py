"""Run the camera wall (Kivy). The Android app starts here too, through main.py.

Usage on a desktop, for development:
    python -m camera_wall                      # ~/.camera_wall/config.json
    python -m camera_wall --config FILE.json   # another config file
    python -m camera_wall --grid 3x3           # override the config's grid
    python -m camera_wall --check FILE.json    # check a config file and exit (no Kivy needed)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

log = logging.getLogger("camera_wall")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_FILE = "camera_wall.log"  # on Android, in the app's private storage (see log_to_file)


def main(argv: list[str] | None = None) -> None:
    # Before Kivy is imported: Kivy must not parse our command line (it has its own -c/--config
    # options), and must leave Python's logging to us, so our messages reach logcat as well.
    os.environ.setdefault("KIVY_NO_ARGS", "1")
    os.environ.setdefault("KIVY_LOG_MODE", "MIXED")
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

    parser = argparse.ArgumentParser(prog="camera_wall", description="Live view of Dahua IP cameras in a grid.")
    parser.add_argument("--config", help="the JSON config file (default: ~/.camera_wall/config.json)")
    parser.add_argument("--grid", help='override the config\'s grid, e.g. "3x3"')
    parser.add_argument("--check", metavar="FILE", help="check a config file, print a summary and exit")
    args, _unknown = parser.parse_known_args(argv)  # ignore whatever a launcher may add

    if args.check:
        sys.exit(check_config(args.check))

    from kivy.config import Config  # pylint: disable=import-outside-toplevel

    from camera_wall.config import is_android  # pylint: disable=import-outside-toplevel

    if is_android():
        log_to_file()

    Config.set("kivy", "exit_on_escape", "1")  # Back on the remote closes the app
    Config.set("input", "mouse", "mouse,disable_multitouch")  # no red dots on right-click
    if not is_android():
        Config.set("graphics", "width", "1280")
        Config.set("graphics", "height", "720")

    # Imported after Config is set: importing the app creates the window.
    import av  # pylint: disable=import-outside-toplevel

    from camera_wall.kivy_app import CameraWallApp  # pylint: disable=import-outside-toplevel

    missing = [name for name in ("h264", "hevc") if name not in av.codecs_available]
    missing += [name for name in ("rtsp",) if name not in av.formats_available]
    log.info("PyAV %s, FFmpeg libavcodec %s", av.__version__, ".".join(map(str, av.library_versions["libavcodec"])))
    if missing:
        log.error("This FFmpeg build lacks %s: some cameras may not play", ", ".join(missing))

    try:
        CameraWallApp(config_option=args.config, grid_override=args.grid).run()
    except Exception:
        log.exception("The camera wall crashed")
        raise


def log_to_file() -> None:
    """Also keep a log file in the app's private storage (Android). TV log buffers are small and
    busy, so logcat loses the app's lines within seconds. To read it:
    adb shell run-as org.camerawall.camerawall cat files/camera_wall.log
    """
    from logging.handlers import RotatingFileHandler  # pylint: disable=import-outside-toplevel

    from camera_wall.config import app_dir  # pylint: disable=import-outside-toplevel

    try:
        handler = RotatingFileHandler(app_dir() / LOG_FILE, maxBytes=512_000, backupCount=1, encoding="utf-8")
    except OSError as exc:
        log.warning("No log file: %s", exc)
        return
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(handler)


def check_config(path: str) -> int:
    """Validate a config file the way the app will; print a summary or the problem."""
    from pathlib import Path  # pylint: disable=import-outside-toplevel

    from camera_wall.config import ConfigError, load_config  # pylint: disable=import-outside-toplevel

    try:
        config = load_config(Path(path))
    except (FileNotFoundError, ConfigError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    cameras = config["cameras"]
    print(f"{path}: {len(cameras)} camera(s) in a {config['rows']}x{config['cols']} grid")
    for cam in cameras:
        print(f"  {cam['name']}: {cam['ip']}:{cam['port']} channel {cam['channel']}, {cam['stream']} stream")
    if any(cam["stream"] == "main" for cam in cameras):
        print('Note: main streams are heavy for TV boxes; if the video stutters, use "sub".')
    return 0


if __name__ == "__main__":
    main()
