#!/usr/bin/env python3
"""
Dahua Camera Wall: live view of Dahua IP cameras in a grid (desktop app, PyQt6).

Cameras and the grid layout come from a JSON file (see README.md and config.example.json),
looked up in this order:
  1. the path given with --config
  2. the CAMERA_WALL_CONFIG environment variable
  3. ~/.camera_wall/config.json

The window opens at 90% of the screen it starts on. Each video is scaled into its cell, so the
window never grows past the screen; maximize or resize it as you like.

The streams are read and decoded by the camera_wall package (PyAV/FFmpeg), the same code as the
Android app. It decodes on the GPU when there is one (NVIDIA NVDEC, which also scales the frames
down to the cell's size, or Intel Quick Sync) and falls back to the CPU by itself.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import numpy as np
from PyQt6.QtCore import Qt, QTimer
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

from camera_wall.camera_reader import CameraReader, build_rtsp_url
from camera_wall.config import ConfigError, get_config_path, load_config, write_example
from camera_wall.hardware import hardware_decoder_limit, plan_decoders, software_threads

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("camera_wall")

APP_NAME = "Dahua Camera Wall"
WINDOW_FRACTION = 0.9  # of the screen the window opens on
REFRESH_MS = 33  # how often the window shows the newest frames (~30 per second)
STOP_WAIT = 2.0  # seconds to wait in total for the cameras when the window closes

# Status colours by name (the reader reports names)
COLORS = {
    "grey": QColor(128, 128, 128),
    "orange": QColor(255, 165, 0),
    "green": QColor(50, 205, 50),
    "red": QColor(220, 20, 60),
}


# ── Widgets ───────────────────────────────────────────────────────────
class CameraPanel(QFrame):
    """One camera: its video, scaled into the cell, and a status line."""

    def __init__(self, name: str, rtsp_url: str | None, parent=None, **reader_options):
        super().__init__(parent)
        self.name = name
        self.reader = CameraReader(name, rtsp_url, **reader_options) if rtsp_url else None
        self._frame_id = 0
        self._shown_status: tuple[str, str] | None = None

        self.video = QLabel("—" if rtsp_url is None else "")
        self.video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video.setStyleSheet("QLabel { background-color: #000; color: #888; }")
        # A label's natural minimum is the size of its picture, so every frame sized to the label
        # would hold the window at that size or push it bigger. Ignoring it lets the grid decide.
        self.video.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.video.setMinimumSize(1, 1)

        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setFont(QFont("Monospace", 9))
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(self.video, 1)
        layout.addWidget(self.status_label)
        self._show_status(("Connecting…", "grey") if self.reader else ("No camera", "grey"))

    @property
    def connected(self) -> bool:
        return self._shown_status is not None and self._shown_status[1] == "green"

    def start(self) -> None:
        if self.reader:
            self.reader.set_output_size(*self._video_pixels())
            self.reader.start()

    def refresh(self) -> bool:
        """Show the reader's newest status and frame, if they changed. True if the status did."""
        if not self.reader:
            return False
        status = self.reader.status
        status_changed = status != self._shown_status
        if status_changed:
            self._show_status(status)

        frame_id, frame = self.reader.latest_frame()
        if frame is not None and frame_id != self._frame_id:
            self._frame_id = frame_id
            # The reader already scaled the frame to fit the cell: show it as it is
            frame = np.ascontiguousarray(frame)
            height, width = frame.shape[:2]
            image = QImage(frame.data, width, height, 3 * width, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(image)  # copies the pixels
            pixmap.setDevicePixelRatio(self.video.devicePixelRatioF())
            self.video.setPixmap(pixmap)
        return status_changed

    def _show_status(self, status: tuple[str, str]) -> None:
        text, color = status
        self._shown_status = status
        self.status_label.setText(f"{self.name} — {text}")
        self.setStyleSheet(
            f"CameraPanel {{ border: 2px solid {COLORS.get(color, COLORS['grey']).name()}; border-radius: 6px; }}"
        )

    def _video_pixels(self) -> tuple[float, float]:
        """The video area's size in physical pixels (sharp pictures on HiDPI screens too)."""
        ratio = self.video.devicePixelRatioF()
        return self.video.width() * ratio, self.video.height() * ratio

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.reader:  # the next frames come at the new size
            self.reader.set_output_size(*self._video_pixels())


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
        shown = cameras[: rows * cols]

        # Hardware decoding for every camera unless the config limits it ("hardware_decoders")
        slots = config.get("hardware_decoders")
        hardware = plan_decoders(shown, hardware_decoder_limit() if slots is None else slots)
        threads = software_threads(hardware.count(False), os.cpu_count() or 1)

        self.panels: list[CameraPanel] = []
        for index in range(rows * cols):
            if index < len(shown):
                cam = shown[index]
                panel = CameraPanel(
                    cam["name"], build_rtsp_url(cam), self,
                    hardware=hardware[index], threads=threads, aspect=cam["aspect_ratio"],
                )
            else:
                panel = CameraPanel(f"Cell {index + 1}", None, self)
            grid.addWidget(panel, index // cols, index % cols)
            self.panels.append(panel)
        for r in range(rows):
            grid.setRowStretch(r, 1)
        for c in range(cols):
            grid.setColumnStretch(c, 1)
        self.update_overall_status()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh)

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
        self._refresh_timer.start(REFRESH_MS)

    def _refresh(self) -> None:
        if any([panel.refresh() for panel in self.panels]):  # every panel, not just up to the first change
            self.update_overall_status()

    def update_overall_status(self) -> None:
        cameras = [p for p in self.panels if p.reader]
        connected = sum(1 for p in cameras if p.connected)
        self.overall.setText(f"Connected: {connected}/{len(cameras)}")
        color = "green" if cameras and connected == len(cameras) else ("orange" if connected else "red")
        self.overall.setStyleSheet(f"color: {color};")

    def closeEvent(self, event) -> None:
        log.info("Shutting down…")
        self._refresh_timer.stop()
        readers = [p.reader for p in self.panels if p.reader]
        for reader in readers:
            reader.stop()  # ask them all first, then wait: closing takes seconds, not seconds per camera
        deadline = time.monotonic() + STOP_WAIT
        for reader in readers:
            reader.join(max(0.0, deadline - time.monotonic()))  # daemon threads: a stuck one won't block exit
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
    path = get_config_path(args.config)
    try:
        config = load_config(path, grid_override=args.grid)
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
