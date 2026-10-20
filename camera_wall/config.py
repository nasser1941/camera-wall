"""Configuration: where the file is, loading and validation.

Platform-agnostic (no Kivy imports), so it runs, and is tested, on any desktop. The file format
is the same as the desktop app's (camera_wall.py), so one config.json works for both.
"""

from __future__ import annotations

import json
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

DEFAULT_GRID = "2x3"
DEFAULT_PORT = 554
MAX_GRID = 8  # max rows or columns
CONFIG_ENV = "CAMERA_WALL_CONFIG"
CONFIG_NAME = "config.json"


class ConfigError(Exception):
    """The configuration file exists but can't be used."""


def is_android() -> bool:
    """True when running inside the Android app.

    python-for-android sets ANDROID_ARGUMENT; Python 3.13+ also reports ``sys.platform`` as
    ``"android"``.
    """
    return sys.platform == "android" or "ANDROID_ARGUMENT" in os.environ


@lru_cache(maxsize=1)
def app_dir() -> Path:
    """The folder that holds config.json by default.

    Android: the app's private storage, ``/data/data/<package>/files``. No other app can read
    it (the file holds passwords), it survives app updates and goes away on uninstall.
    ``push_config.sh`` writes the file there with ``adb shell run-as``, which works on every
    Android version with the debug APK. (The app's folder on shared storage,
    ``Android/data/<package>``, is not an option: from Android 11 on, adb can't write there.)
    Desktop: ``~/.camera_wall``, like the desktop app.
    """
    if is_android():
        from jnius import autoclass  # pylint: disable=import-outside-toplevel,import-error

        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        return Path(activity.getFilesDir().getAbsolutePath())
    return Path.home() / ".camera_wall"


# A default configuration built into the APK (./build_android.sh --config FILE). It only exists
# in such builds; the build script copies it here for the build and removes it afterwards.
BUNDLED_CONFIG = Path(__file__).with_name("bundled_config.json")


def config_candidates(given: str | None = None) -> list[Path]:
    """The config files to look for, by priority; the first one that exists is used.

    1. ``given`` (the ``--config`` option), alone
    2. else the ``CAMERA_WALL_CONFIG`` environment variable, alone
    3. else ``config.json`` in :func:`app_dir` (on Android: pushed with push_config.sh),
       then the configuration built into the APK, if any
    """
    if given:
        return [Path(given).expanduser()]
    env = os.environ.get(CONFIG_ENV)
    if env:
        return [Path(env).expanduser()]
    return [app_dir() / CONFIG_NAME, BUNDLED_CONFIG]


def get_config_path(given: str | None = None) -> Path:
    """The config file to use: the first of :func:`config_candidates` that exists, else the
    first one (where the user should put it)."""
    candidates = config_candidates(given)
    return next((path for path in candidates if path.is_file()), candidates[0])


def parse_grid(text: str) -> tuple[int, int]:
    """Parse a grid like ``"2x3"`` (also ``"2×3"``, ``"2 X 3"``) into ``(rows, cols)``."""
    match = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", str(text))
    if not match:
        raise ConfigError(f'grid must look like "2x3" (rows x columns), not {text!r}')
    rows, cols = int(match.group(1)), int(match.group(2))
    if not (1 <= rows <= MAX_GRID and 1 <= cols <= MAX_GRID):
        raise ConfigError(f"grid {text!r}: rows and columns must be between 1 and {MAX_GRID}")
    return rows, cols


def parse_aspect_ratio(value) -> float | None:
    """A picture shape like ``"16:9"``, ``"4:3"`` or ``1.78`` as width / height; None (or
    ``"auto"``) means "as the stream says"."""
    if value is None or str(value).strip().lower() in ("", "auto"):
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:[:/xX×]\s*(\d+(?:\.\d+)?))?\s*", str(value))
    ratio = float(match.group(1)) / float(match.group(2) or 1) if match and float(match.group(2) or 1) else 0
    if not 0.25 <= ratio <= 4:
        raise ConfigError(f'aspect_ratio must look like "16:9" or "4:3", not {value!r}')
    return ratio


def load_config(path: Path, grid_override: str | None = None) -> dict:
    """Load and validate the config file.

    Returns ``{"rows": int, "cols": int, "cameras": [camera dicts], "hardware_decoders": int or
    None}``, every camera with all its keys filled in. ``hardware_decoders`` (optional) caps how
    many cameras the Android app decodes with the device's video hardware; None means as many
    as the device allows, 0 turns it off. Raises FileNotFoundError if the file doesn't exist
    and ConfigError if it can't be used.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise ConfigError(f"can't read {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a JSON object")

    rows, cols = parse_grid(grid_override or raw.get("grid") or raw.get("grid_layout") or DEFAULT_GRID)

    # The picture shape to show, for all cameras (each camera can override it). Needed for
    # sub streams like Dahua's D1 (704x576), which squeeze a 16:9 picture into nearly square
    # pixels without saying so.
    default_aspect = raw.get("aspect_ratio")
    parse_aspect_ratio(default_aspect)

    cameras_raw = raw.get("cameras")
    if not isinstance(cameras_raw, list) or not cameras_raw:
        raise ConfigError(f'{path} needs a "cameras" list with at least one camera')

    cameras = []
    for i, cam in enumerate(cameras_raw, 1):
        if not isinstance(cam, dict) or not cam.get("ip"):
            raise ConfigError(f'camera {i} in {path} needs at least an "ip"')

        stream = str(cam.get("stream", "main")).lower()
        if stream not in ("main", "sub"):
            raise ConfigError(f'camera {i}: "stream" must be "main" or "sub", not {stream!r}')

        try:
            port = int(cam.get("port", DEFAULT_PORT))
            channel = int(cam.get("channel", 1))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f'camera {i}: "port" and "channel" must be whole numbers') from exc

        try:
            aspect_ratio = parse_aspect_ratio(cam.get("aspect_ratio", default_aspect))
        except ConfigError as exc:
            raise ConfigError(f"camera {i}: {exc}") from exc

        cameras.append(
            {
                "name": str(cam.get("name") or f"Camera {i}"),
                "ip": str(cam["ip"]),
                "port": port,
                "username": str(cam.get("username", "admin")),
                "password": str(cam.get("password", "")),
                "channel": channel,
                "stream": stream,
                "aspect_ratio": aspect_ratio,
            }
        )

    hardware_decoders = raw.get("hardware_decoders")
    if hardware_decoders is not None and (
        isinstance(hardware_decoders, bool) or not isinstance(hardware_decoders, int) or hardware_decoders < 0
    ):
        raise ConfigError('"hardware_decoders" must be a whole number, 0 or more (0 turns it off)')

    return {"rows": rows, "cols": cols, "cameras": cameras, "hardware_decoders": hardware_decoders}
