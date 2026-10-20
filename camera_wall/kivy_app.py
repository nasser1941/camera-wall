"""The Kivy app: the camera grid, plus the screen that explains what to do without a config."""

from __future__ import annotations

import logging
import os
import time
from functools import partial
from pathlib import Path

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.widget import Widget

from camera_wall import __version__
from camera_wall.camera_reader import build_rtsp_url
from camera_wall.config import ConfigError, config_candidates, get_config_path, is_android, load_config
from camera_wall.hardware import hardware_decoder_limit, plan_decoders, software_threads
from camera_wall.kivy_panel import COLORS, TEXT_COLOR, CameraPanel

log = logging.getLogger("camera_wall.app")

APP_NAME = "Camera Wall"
ANDROID_PACKAGE = "org.camerawall.camerawall"  # package.domain + package.name in buildozer.spec
BACKGROUND = (0.063, 0.078, 0.094, 1)  # #101418, as the presplash
REFRESH_RATE = 30  # screen updates per second; cameras send 20-25 frames per second
CONFIG_CHECK_INTERVAL = 2  # seconds between checks for a new or changed config file
STOP_WAIT = 2.0  # seconds to wait in total for the readers when the wall closes


class CameraWall(BoxLayout):
    """A rows x columns grid of camera panels, and a line counting the connected cameras."""

    def __init__(self, wall_config: dict, **kwargs):
        super().__init__(orientation="vertical", padding=dp(6), spacing=dp(4), **kwargs)
        rows, cols = wall_config["rows"], wall_config["cols"]
        cameras = wall_config["cameras"]
        if len(cameras) > rows * cols:
            log.warning("%d cameras but a %dx%d grid: showing the first %d", len(cameras), rows, cols, rows * cols)

        # Hardware decoders for the heaviest cameras, as many as the device runs at once
        shown = cameras[: rows * cols]
        slots = wall_config.get("hardware_decoders")
        if slots is None:
            slots = hardware_decoder_limit()
        hardware = plan_decoders(shown, slots)
        threads = software_threads(hardware.count(False), os.cpu_count() or 1)
        log.info(
            "Decoding: %d camera(s) in hardware, %d in software (%s thread(s) each)",
            hardware.count(True), hardware.count(False), threads or "auto",
        )

        grid = GridLayout(rows=rows, cols=cols, spacing=dp(6))
        self.panels: list[CameraPanel] = []
        for index in range(rows * cols):
            if index < len(shown):
                cam = shown[index]
                panel = CameraPanel(
                    cam["name"], build_rtsp_url(cam), hardware=hardware[index], threads=threads,
                    aspect=cam["aspect_ratio"],
                )
            else:
                panel = CameraPanel(f"Cell {index + 1}", None)
            grid.add_widget(panel)
            self.panels.append(panel)

        self.overall = Label(size_hint_y=None, height=sp(20), font_size=sp(13), color=TEXT_COLOR)
        self.add_widget(grid)
        self.add_widget(self.overall)
        self._refresh_event = None
        self._shown_overall: tuple[int, int] | None = None

    def start(self) -> None:
        for panel in self.panels:
            panel.start()
        self._refresh_event = Clock.schedule_interval(self._refresh, 1 / REFRESH_RATE)
        self._refresh()

    def stop(self) -> None:
        """Stop every camera: ask them all first, then wait (briefly) for them together."""
        if self._refresh_event is not None:
            self._refresh_event.cancel()
            self._refresh_event = None
        readers = [panel.reader for panel in self.panels if panel.reader]
        for reader in readers:
            reader.stop()
        deadline = time.monotonic() + STOP_WAIT
        for reader in readers:
            # Readers are daemon threads: one stuck on a silent camera won't block the app
            reader.join(max(0.0, deadline - time.monotonic()))

    def _refresh(self, _dt: float = 0) -> None:
        for panel in self.panels:
            panel.refresh()
        cameras = [panel for panel in self.panels if panel.reader]
        counts = (sum(1 for panel in cameras if panel.connected), len(cameras))
        if counts != self._shown_overall:
            self._shown_overall = counts
            connected, total = counts
            self.overall.text = f"Connected: {connected}/{total}"
            if connected == total:
                self.overall.color = COLORS["green"]
            else:
                self.overall.color = COLORS["orange"] if connected else COLORS["red"]


class MessageView(Label):
    """Full-screen text: what to do when there is no usable configuration."""

    def __init__(self, text: str, **kwargs):
        super().__init__(
            text=text,
            markup=True,
            font_size=sp(16),
            color=TEXT_COLOR,
            halign="left",
            valign="middle",
            padding=(dp(48), dp(27)),  # inside a TV's overscan-safe area
            **kwargs,
        )
        self.bind(size=lambda label, size: setattr(label, "text_size", size))


def missing_config_message(path: Path) -> str:
    if is_android():
        how = (
            "From the computer with the camera wall project, over adb:\n\n"
            "  1. On the TV, turn on Developer options > USB debugging (or Network debugging).\n"
            "  2. adb connect <TV address>        (for network debugging)\n"
            "  3. ./push_config.sh config.json\n\n"
            "[color=888888]Without the script:\n"
            f"  adb shell \"run-as {ANDROID_PACKAGE} sh -c 'umask 077; cat > files/config.json'\" < config.json\n"
            f"The file goes to the app's private storage: {path}[/color]\n"
        )
    else:
        how = f"Put your config.json at:\n\n  {path}\n\nor start the app with --config <file>.\n"
    return (
        f"[size={int(sp(26))}][b]{APP_NAME}[/b][/size]  [color=888888]v{__version__}[/color]\n\n"
        "[b]No configuration yet.[/b] The app needs a config.json with your cameras "
        "(the same file as the desktop camera wall; see config.example.json).\n\n"
        f"{how}\n"
        "The camera wall starts by itself as soon as the file is there."
    )


def config_error_message(path: Path, error: Exception) -> str:
    fix = "Push a corrected file: ./push_config.sh config.json" if is_android() else f"Fix {path}"
    return (
        f"[size={int(sp(26))}][b]{APP_NAME}[/b][/size]\n\n"
        f"[b][color=ff6060]The configuration has a problem:[/color][/b]\n\n  {error}\n\n"
        f"{fix}\n\n"
        "The camera wall reloads by itself when the file changes."
    )


class CameraWallApp(App):
    """Shows the camera wall for the config file, and keeps it in step with the file.

    Without a usable file it shows what to do instead, and checks again every few seconds.
    Which file is used is decided again at each check (see config.config_candidates): a
    config pushed to the device takes over from one built into the APK, without a restart.
    The cameras stop while the app is in the background (Home button on a TV) and start
    again when it comes back.
    """

    title = APP_NAME

    def __init__(self, config_option: str | None = None, grid_override: str | None = None, **kwargs):
        super().__init__(**kwargs)
        # Not "config": App.config is Kivy's own settings object.
        self.config_option = config_option  # --config, if given
        self.grid_override = grid_override
        self.wall: CameraWall | None = None
        self._config_stamp: tuple[str, int, int] | None = None
        self._watch_event = None

    @property
    def config_path(self) -> Path:
        """The config file in use (or, if there is none, where it should go)."""
        return get_config_path(self.config_option)

    def build(self) -> Widget:
        Window.clearcolor = BACKGROUND
        self.root = BoxLayout()
        self._show_current_config()
        self._watch_event = Clock.schedule_interval(self._check_config, CONFIG_CHECK_INTERVAL)
        return self.root

    def on_pause(self) -> bool:
        log.info("Paused: stopping the cameras")
        self._stop_wall()
        return True  # keep the app alive in the background

    def on_resume(self) -> None:
        log.info("Resumed: starting the cameras")
        self._show_current_config()

    def on_stop(self) -> None:
        if self._watch_event is not None:
            self._watch_event.cancel()
        self._stop_wall()

    # ── Internals ────────────────────────────────────────────────────────

    def _config_signature(self) -> tuple[str, int, int] | None:
        """Changes whenever another file takes over or the file is written; None if there's none."""
        path = self.config_path
        try:
            stat = path.stat()
        except OSError:
            return None
        return str(path), stat.st_mtime_ns, stat.st_size

    def _check_config(self, _dt: float) -> None:
        if self._config_signature() != self._config_stamp:
            log.info("Configuration file changed: reloading %s", self.config_path)
            self._show_current_config()

    def _show_current_config(self) -> None:
        """Load the config file and show the wall for it, or what's wrong with it."""
        self._stop_wall()
        self._config_stamp = self._config_signature()
        path = self.config_path
        try:
            wall_config = load_config(path, self.grid_override)
        except FileNotFoundError:
            log.warning("No configuration at %s", path)
            self._set_content(MessageView(missing_config_message(config_candidates(self.config_option)[0])))
            return
        except ConfigError as exc:
            log.error("Configuration error: %s", exc)
            self._set_content(MessageView(config_error_message(path, exc)))
            return

        log.info(
            "Showing %d camera(s) in a %dx%d grid from %s",
            len(wall_config["cameras"]), wall_config["rows"], wall_config["cols"], path,
        )
        self.wall = CameraWall(wall_config)
        self._set_content(self.wall)
        # Start on the next frame, once the grid has been laid out
        Clock.schedule_once(partial(self._start_wall, self.wall))

    def _start_wall(self, wall: CameraWall, _dt: float) -> None:
        if wall is self.wall:  # not replaced or stopped in the meantime
            wall.start()

    def _set_content(self, widget: Widget) -> None:
        self.root.clear_widgets()
        self.root.add_widget(widget)

    def _stop_wall(self) -> None:
        if self.wall is not None:
            self.wall.stop()
            self.wall = None
