#!/usr/bin/env python3
"""
Dahua Camera Wall: live view of Dahua IP cameras in a grid.

Cameras and the grid layout come from a JSON file (see README.md and config.example.json),
looked up in this order:
  1. the path given with --config
  2. the CAMERA_WALL_CONFIG environment variable
  3. ~/.camera_wall/config.json

The window opens at 90% of the screen it starts on. Each video is scaled into its cell, so the
window never grows past the screen; maximize or resize it as you like.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import cv2
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("camera_wall")

APP_NAME = "Dahua Camera Wall"
DEFAULT_GRID = "2x3"
DEFAULT_PORT = 554
MAX_GRID = 8  # rows or columns
WINDOW_FRACTION = 0.9  # of the screen the window opens on
RECONNECT_DELAY = 3  # seconds between connection attempts
ERROR_AFTER = 15  # seconds of failing before the status says "Error"

GREY, ORANGE, GREEN, RED = QColor(128, 128, 128), QColor(255, 165, 0), QColor(50, 205, 50), QColor(220, 20, 60)

EXAMPLE_CONFIG = {
    "grid": DEFAULT_GRID,
    "cameras": [
        {"name": f"Camera {i}", "ip": f"192.168.1.{100 + i}", "username": "admin", "password": "CHANGE_ME", "stream": "main"}
        for i in range(1, 7)
    ],
}


# ── Configuration ─────────────────────────────────────────────────────
class ConfigError(Exception):
    pass


def config_path(given: str | None) -> Path:
    if given:
        return Path(given).expanduser()
    if os.environ.get("CAMERA_WALL_CONFIG"):
        return Path(os.environ["CAMERA_WALL_CONFIG"]).expanduser()
    return Path.home() / ".camera_wall" / "config.json"


def parse_grid(text: str) -> tuple[int, int]:
    """'2x3' (or '2×3', '2 X 3') → (rows, cols)."""
    match = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", str(text))
    if not match:
        raise ConfigError(f'grid must look like "2x3" (rows x columns), not {text!r}')
    rows, cols = int(match.group(1)), int(match.group(2))
    if not (1 <= rows <= MAX_GRID and 1 <= cols <= MAX_GRID):
        raise ConfigError(f"grid {text!r}: rows and columns must be between 1 and {MAX_GRID}")
    return rows, cols


def load_config(path: Path) -> dict:
    """The validated configuration: {"rows", "cols", "cameras": [...]}."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as e:
        raise ConfigError(f"can't read {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    rows, cols = parse_grid(raw.get("grid") or raw.get("grid_layout") or DEFAULT_GRID)
    cameras = raw.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        raise ConfigError(f'{path} needs a "cameras" list with at least one camera')
    checked = []
    for i, cam in enumerate(cameras, 1):
        if not isinstance(cam, dict) or not cam.get("ip"):
            raise ConfigError(f'camera {i} in {path} needs at least an "ip"')
        stream = str(cam.get("stream", "main")).lower()
        if stream not in ("main", "sub"):
            raise ConfigError(f'camera {i}: "stream" must be "main" or "sub", not {stream!r}')
        checked.append(
            {
                "name": str(cam.get("name") or f"Camera {i}"),
                "ip": str(cam["ip"]),
                "port": int(cam.get("port", DEFAULT_PORT)),
                "username": str(cam.get("username", "admin")),
                "password": str(cam.get("password", "")),
                "channel": int(cam.get("channel", 1)),
                "stream": stream,
            }
        )
    return {"rows": rows, "cols": cols, "cameras": checked}


def write_example(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(EXAMPLE_CONFIG, indent=2) + "\n")
    path.chmod(0o600)  # it will hold camera passwords


# ── RTSP ──────────────────────────────────────────────────────────────
def build_rtsp_url(cam: dict) -> str:
    """Dahua's RTSP address for a camera; subtype 0 is the main stream, 1 the sub stream."""
    subtype = 0 if cam["stream"] == "main" else 1
    credentials = f"{quote(cam['username'], safe='')}:{quote(cam['password'], safe='')}@"
    return (
        f"rtsp://{credentials}{cam['ip']}:{cam['port']}"
        f"/cam/realmonitor?channel={cam['channel']}&subtype={subtype}&unicast=true&proto=Onvif"
    )


def redact(url: str) -> str:
    """The address without its password, for logs."""
    return re.sub(r"(rtsp://[^:/@]*):[^@]*@", r"\1:***@", url)


class FrameReader(QThread):
    """Reads frames from one RTSP stream in the background and reconnects when it drops."""

    frame_ready = pyqtSignal(QImage)
    status_changed = pyqtSignal(str, QColor)

    def __init__(self, camera_name: str, rtsp_url: str):
        super().__init__()
        self.camera_name = camera_name
        self.rtsp_url = rtsp_url
        self._running = True

    def stop(self) -> None:
        """Ask the thread to finish (call wait() to wait for it)."""
        self._running = False

    def _status(self, text: str, color: QColor) -> None:
        self.status_changed.emit(text, color)

    def _open(self):
        cap = cv2.VideoCapture(self.rtsp_url)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if cap.isOpened():
            return cap
        cap.release()
        return None

    def run(self) -> None:
        log.info("%s: connecting to %s", self.camera_name, redact(self.rtsp_url))
        self._status("Connecting…", GREY)
        failing_since = time.monotonic()
        while self._running:
            cap = self._open()
            if cap is None:
                failed = time.monotonic() - failing_since >= ERROR_AFTER
                self._status("Error — check the camera" if failed else "Reconnecting…", RED if failed else ORANGE)
                self._sleep(RECONNECT_DELAY)
                continue
            self._status("Connected", GREEN)
            while self._running:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb.shape
                self.frame_ready.emit(QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888).copy())
            cap.release()
            if self._running:
                log.info("%s: stream ended, reconnecting", self.camera_name)
                failing_since = time.monotonic()
                self._status("Reconnecting…", ORANGE)
                self._sleep(RECONNECT_DELAY)
        log.info("%s: stopped", self.camera_name)

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            time.sleep(0.1)


# ── Widgets ───────────────────────────────────────────────────────────
class CameraPanel(QFrame):
    """One camera: its video, scaled into the cell, and a status line."""

    def __init__(self, name: str, rtsp_url: str | None, parent=None):
        super().__init__(parent)
        self.name = name
        self.status = "No camera" if rtsp_url is None else "Connecting…"
        self._frame: QImage | None = None

        self.video = QLabel("—" if rtsp_url is None else "")
        self.video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video.setStyleSheet("QLabel { background-color: #000; color: #888; }")
        # A label's natural minimum is the size of its picture, so every frame scaled to the label
        # would hold the window at that size or push it bigger. Ignoring it lets the grid decide.
        self.video.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.video.setMinimumSize(1, 1)

        self.status_label = QLabel(f"{name} — {self.status}")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setFont(QFont("Monospace", 9))
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(self.video, 1)
        layout.addWidget(self.status_label)
        self._set_border(GREY)

        self.reader = FrameReader(name, rtsp_url) if rtsp_url else None
        if self.reader:
            self.reader.frame_ready.connect(self._on_frame)
            self.reader.status_changed.connect(self._on_status)

    def _set_border(self, color: QColor) -> None:
        self.setStyleSheet(f"CameraPanel {{ border: 2px solid {color.name()}; border-radius: 6px; }}")

    def _show(self) -> None:
        if self._frame is None:
            return
        size = self.video.size()
        if size.width() < 2 or size.height() < 2:
            return
        pixmap = QPixmap.fromImage(self._frame)
        self.video.setPixmap(
            pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        )

    def _on_frame(self, image: QImage) -> None:
        self._frame = image
        self._show()

    def _on_status(self, text: str, color: QColor) -> None:
        self.status = text
        self.status_label.setText(f"{self.name} — {text}")
        self._set_border(color)
        window = self.window()
        if isinstance(window, CameraWall):
            window.update_overall_status()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._show()  # rescale the last frame right away, not at the next one

    def start(self) -> None:
        if self.reader:
            self.reader.start()


class CameraWall(QMainWindow):
    """The main window: a rows × columns grid of camera panels."""

    def __init__(self, config: dict):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(320, 240)
        self.overall = QLabel("Starting…")
        self.overall.setFont(QFont("Monospace", 10))
        status_bar = QStatusBar()
        status_bar.addPermanentWidget(self.overall)
        self.setStatusBar(status_bar)

        central = QWidget()
        self.setCentralWidget(central)
        grid = QGridLayout(central)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setSpacing(8)
        rows, cols = config["rows"], config["cols"]
        cameras = config["cameras"]
        if len(cameras) > rows * cols:
            log.warning("%d cameras but a %dx%d grid: showing the first %d", len(cameras), rows, cols, rows * cols)
        self.panels: list[CameraPanel] = []
        for index in range(rows * cols):
            if index < len(cameras):
                cam = cameras[index]
                panel = CameraPanel(cam["name"], build_rtsp_url(cam), self)
            else:
                panel = CameraPanel(f"Cell {index + 1}", None, self)
            grid.addWidget(panel, index // cols, index % cols)
            self.panels.append(panel)
        for r in range(rows):
            grid.setRowStretch(r, 1)
        for c in range(cols):
            grid.setColumnStretch(c, 1)
        self.update_overall_status()

    def fit_to_screen(self) -> None:
        """Open at WINDOW_FRACTION of the available screen, centered."""
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(1600, 900)
            return
        area = screen.availableGeometry()
        width, height = int(area.width() * WINDOW_FRACTION), int(area.height() * WINDOW_FRACTION)
        self.resize(width, height)
        self.move(area.x() + (area.width() - width) // 2, area.y() + (area.height() - height) // 2)

    def start(self) -> None:
        for panel in self.panels:
            panel.start()

    def update_overall_status(self) -> None:
        cameras = [p for p in self.panels if p.reader]
        connected = sum(1 for p in cameras if p.status == "Connected")
        self.overall.setText(f"Connected: {connected}/{len(cameras)}")
        color = "green" if cameras and connected == len(cameras) else ("orange" if connected else "red")
        self.overall.setStyleSheet(f"color: {color};")

    def closeEvent(self, event) -> None:
        log.info("Shutting down…")
        readers = [p.reader for p in self.panels if p.reader]
        for reader in readers:
            reader.stop()  # ask them all first, then wait: closing takes seconds, not 3 s per camera
        for reader in readers:
            reader.wait(5000)
        event.accept()


# ── Entry point ───────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live view of Dahua IP cameras in a grid.")
    parser.add_argument("--config", help="the JSON config file (default: ~/.camera_wall/config.json)")
    parser.add_argument("--grid", help='override the config\'s grid, e.g. "3x3"')
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    path = config_path(args.config)
    try:
        config = load_config(path)
        if args.grid:
            config["rows"], config["cols"] = parse_grid(args.grid)
    except FileNotFoundError:
        write_example(path)
        message = (
            f"No configuration found, so an example was written to:\n{path}\n\n"
            "Put your cameras' addresses and passwords in it, then start the camera wall again. "
            "README.md explains every setting."
        )
        log.error(message.replace("\n", " "))
        QMessageBox.information(None, APP_NAME, message)
        return 1
    except ConfigError as e:
        log.error("%s", e)
        QMessageBox.critical(None, APP_NAME, f"The configuration has a problem:\n\n{e}")
        return 1

    window = CameraWall(config)
    window.fit_to_screen()
    window.show()
    QTimer.singleShot(500, window.start)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
