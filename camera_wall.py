#!/usr/bin/env python3
"""
Dahua Camera Wall — Multi-camera live viewer for Dahua IP cameras.

Shows cameras in a 2×3 grid (configurable) with auto-reconnect on stream failure.
Plays direct RTSP streams (no NVR token needed).
Uses OpenCV (FFmpeg) for RTSP — works even when system VLC lacks live555.

Configuration is read from ~/.camera_wall/config.json at startup.
"""

import sys
import os
import json
import time
import logging
from pathlib import Path
from typing import Any, Dict
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QRect
from PyQt6.QtGui import QFont, QColor, QPixmap, QImage
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGridLayout,
    QLabel, QVBoxLayout, QFrame, QStatusBar,
)
import cv2
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("camera_wall")

# ── Default configuration ──────────────────────────────────────────────
DEFAULT_CONFIG = {
    "grid_layout": "2x3",
    "cameras": [
        {"name": "Camera 1", "ip": "172.16.1.110", "username": "admin", "password": "***REMOVED***", "stream": "main"},
        {"name": "Camera 2", "ip": "172.16.1.111", "username": "admin", "password": "***REMOVED***", "stream": "main"},
        {"name": "Camera 3", "ip": "172.16.1.112", "username": "admin", "password": "***REMOVED***", "stream": "main"},
        {"name": "Camera 4", "ip": "172.16.1.113", "username": "admin", "password": "***REMOVED***", "stream": "main"},
        {"name": "Camera 5", "ip": "172.16.1.114", "username": "admin", "password": "***REMOVED***", "stream": "main"},
        {"name": "Camera 6", "ip": "172.16.1.115", "username": "admin", "password": "***REMOVED***", "stream": "main"},
    ],
}

# ── Grid layout definitions ────────────────────────────────────────────
GRID_LAYOUTS = {
    "1x1":  {"rows": 1, "cols": 1},
    "2x2":  {"rows": 2, "cols": 2},
    "3x3":  {"rows": 3, "cols": 3},
    "4x4":  {"rows": 4, "cols": 4},
    "2x3":  {"rows": 2, "cols": 3},
    "3x2":  {"rows": 3, "cols": 2},
    "1+5":  {"rows": 2, "cols": 3, "layout": "1big_5small"},
    "1+3":  {"rows": 2, "cols": 4, "layout": "1big_3small"},
    "1+2":  {"rows": 2, "cols": 3, "layout": "1big_2small"},
    "1+6":  {"rows": 3, "cols": 4, "layout": "1big_6small"},
}

# Normalized lookup: maps ASCII 'x' versions to Unicode keys
_GRID_LAYOUT_NORMALIZATION = {
    k.replace("×", "x"): k for k in GRID_LAYOUTS.keys()
}


def _normalize_layout_name(name: str) -> str:
    """Normalize a layout name for dictionary lookup."""
    # First try direct lookup
    if name in GRID_LAYOUTS:
        return name
    # Try normalizing (convert × to x for lookup)
    normalized = name.replace("×", "x")
    if normalized in _GRID_LAYOUT_NORMALIZATION:
        return _GRID_LAYOUT_NORMALIZATION[normalized]
    return name


# ── Configuration file path ────────────────────────────────────────────
def get_config_path() -> Path:
    """Get the path to the configuration file."""
    config_dir = Path.home() / ".camera_wall"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "config.json"


def load_config() -> Dict[str, Any]:
    """Load configuration from file, or return defaults."""
    config_path = get_config_path()
    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                config = json.load(f)
            # Validate required keys
            if "grid_layout" not in config:
                config["grid_layout"] = "2x3"
            else:
                config["grid_layout"] = _normalize_layout_name(config["grid_layout"])
            if "cameras" not in config or not config["cameras"]:
                config["cameras"] = DEFAULT_CONFIG["cameras"]
            log.info("Loaded config from %s", config_path)
            return config
        except (json.JSONDecodeError, KeyError) as e:
            log.error("Failed to load config: %s", e)
    log.info("Using default configuration")
    return DEFAULT_CONFIG.copy()


# ── RTSP URL helpers ──────────────────────────────────────────────────
def build_rtsp_url(camera_ip: str, username: str, password: str, channel: int = 1, subtype: int = 0) -> str:
    """
    Build Dahua RTSP URL for the main stream.

    Parameters:
        camera_ip: IP address of the camera
        username: Camera username
        password: Camera password
        channel: Camera channel number (1-based)
        subtype: 0 = main stream (high quality), 1 = sub stream

    Returns:
        Full RTSP URL string
    """
    return (
        f"rtsp://{username}:{password}@{camera_ip}:554"
        f"/cam/realmonitor?channel={channel}&subtype={subtype}"
        f"&unicast=true&proto=Onvif"
    )


def get_stream_subtype(stream_type: str) -> int:
    """Convert stream type name to RTSP subtype value."""
    return 0 if stream_type == "main" else 1


# ── Frame reader thread ───────────────────────────────────────────────
class FrameReader(QThread):
    """Reads frames from an RTSP stream in a background thread."""

    frame_ready = pyqtSignal(QImage)   # new frame
    status_changed = pyqtSignal(str, str)  # camera_name, status_text
    color_changed = pyqtSignal(QColor)     # panel border color

    def __init__(self, camera_name: str, rtsp_url: str):
        super().__init__()
        self.camera_name = camera_name
        self.rtsp_url = rtsp_url
        self._running = True
        self._cap = None
        self._connect_start = None
        self._error_start = None

    def stop(self):
        self._running = False
        self.wait(3000)

    def _set_status(self, status: str):
        self.status_changed.emit(self.camera_name, status)

    def _set_color(self, color: QColor):
        self.color_changed.emit(color)

    def _open_camera(self):
        """Open or reopen the RTSP camera."""
        if self._cap and self._cap.isOpened():
            self._cap.release()
        self._cap = cv2.VideoCapture(self.rtsp_url)
        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        # Wait for connection
        for _ in range(30):  # 3s max wait
            if not self._running:
                return False
            if self._cap.isOpened():
                return True
            time.sleep(0.1)
        return False

    def run(self):
        """Main loop: keep reading frames, reconnect on failure."""
        log.info("%s: starting → %s", self.camera_name, self.rtsp_url)
        self._set_status("Connecting…")
        self._set_color(QColor(128, 128, 128))  # grey
        self._connect_start = time.time()

        while self._running:
            # Open camera
            if not self._open_camera():
                if not self._running:
                    return
                self._set_status("Reconnecting…")
                self._set_color(QColor(255, 165, 0))
                time.sleep(3)
                continue

            self._set_status("Connected")
            self._set_color(QColor(50, 205, 50))  # green
            self._error_start = None

            # Read frames
            while self._running:
                ret, frame = self._cap.read()
                if not ret or frame is None:
                    break

                # Convert BGR → RGB → QImage
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb.shape
                bytes_per_line = ch * w
                qimg = QImage(rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
                # Copy the image data — Qt only holds a reference during the signal
                self.frame_ready.emit(qimg.copy())

            # Stream ended or error — reconnect
            elapsed = time.time() - self._connect_start if self._connect_start else 0
            if elapsed < 15:
                log.info("%s: stream ended, reconnecting", self.camera_name)
                self._set_status("Reconnecting…")
                self._set_color(QColor(255, 165, 0))
            else:
                log.warning("%s: stream error after timeout", self.camera_name)
                self._set_status("Error — check camera")
                self._set_color(QColor(220, 20, 60))  # crimson
                self._error_start = time.time()

            if self._cap:
                self._cap.release()
                self._cap = None
            time.sleep(3)

        log.info("%s: stopped", self.camera_name)


# ── Camera panel widget ───────────────────────────────────────────────
class VLCPanel(QFrame):
    """A single camera panel with video display and status label."""

    def __init__(self, camera_name: str, rtsp_url: str, parent=None):
        super().__init__(parent)
        self.camera_name = camera_name
        self.rtsp_url = rtsp_url
        self.reader = FrameReader(camera_name, rtsp_url)

        # Video display
        self.video_label = QLabel()
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet(
            "QLabel { background-color: #000; color: #fff; }"
        )
        self.video_label.setMinimumSize(200, 150)

        # Status label
        self.status_label = QLabel("Connecting…")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setFont(QFont("Monospace", 9))

        # Layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(self.video_label)
        layout.addWidget(self.status_label)
        self.setStyleSheet(
            "QFrame { border: 2px solid gray; border-radius: 6px; }"
        )

        # Connect signals
        self.reader.frame_ready.connect(self._on_frame)
        self.reader.status_changed.connect(self._on_status)
        self.reader.color_changed.connect(self._on_color)

    def _on_frame(self, qimg: QImage):
        """Update the video display with a new frame."""
        pixmap = QPixmap.fromImage(qimg)
        # Scale to fit the label while preserving aspect ratio
        self.video_label.setPixmap(
            pixmap.scaled(
                self.video_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _on_status(self, name: str, status: str):
        if name == self.camera_name:
            self.status_label.setText(status)

    def _on_color(self, color: QColor):
        self.setStyleSheet(
            f"QFrame {{ border: 3px solid {color.name()}; border-radius: 6px; }}"
        )

    def start(self):
        self.reader.start()

    def stop(self):
        if self.reader.isRunning():
            self.reader.stop()


# ── Main window ───────────────────────────────────────────────────────
class CameraWall(QMainWindow):
    """Main application window with a configurable camera grid."""

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.panels: list[VLCPanel] = []
        self.setWindowTitle("Dahua Camera Wall")
        self.setMinimumSize(1280, 720)
        self.resize(1600, 900)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._overall_label = QLabel("Initializing…")
        self._overall_label.setFont(QFont("Monospace", 10))
        self.status_bar.addPermanentWidget(self._overall_label)

        # Central widget with grid
        central = QWidget()
        self.setCentralWidget(central)
        self.grid_layout = QGridLayout(central)
        self.grid_layout.setContentsMargins(8, 8, 8, 8)
        self.grid_layout.setSpacing(8)

        # Build grid from config
        self._build_grid()

        # Start all streams
        QTimer.singleShot(1000, self._start_all)

    def _build_grid(self):
        """Build the camera grid based on current configuration."""
        layout_name = _normalize_layout_name(self.config.get("grid_layout", "2x3"))
        layout_def = GRID_LAYOUTS.get(layout_name, GRID_LAYOUTS["2x3"])
        rows: int = int(layout_def["rows"])
        cols: int = int(layout_def["cols"])
        special_layout = layout_def.get("layout")

        num_cameras = len(self.config["cameras"])

        if special_layout:
            self._build_special_grid(rows, cols, special_layout, num_cameras)
        else:
            for idx in range(min(num_cameras, rows * cols)):
                row = idx // cols
                col = idx % cols
                cam = self.config["cameras"][idx]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = VLCPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, row, col)
                self.panels.append(panel)

        # Adjust column/row stretches so cells fill available space
        for i in range(cols):
            self.grid_layout.setColumnStretch(i, 1)
        for i in range(rows):
            self.grid_layout.setRowStretch(i, 1)

        self._update_overall_status()

    def _build_special_grid(self, rows: int, cols: int, layout: str, num_cameras: int):
        """Build special grid layouts (1 big + N small)."""
        if layout == "1big_5small":
            if num_cameras > 0:
                cam = self.config["cameras"][0]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = VLCPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, 0, 0, 1, cols)
                self.panels.append(panel)
            for idx in range(1, min(num_cameras, 6)):
                row = (idx - 1) // 3 + 1
                col = (idx - 1) % 3
                cam = self.config["cameras"][idx]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = VLCPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, row, col)
                self.panels.append(panel)

        elif layout == "1big_3small":
            if num_cameras > 0:
                cam = self.config["cameras"][0]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = VLCPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, 0, 0, 1, cols)
                self.panels.append(panel)
            for idx in range(1, min(num_cameras, 4)):
                row = (idx - 1) // 4 + 1
                col = (idx - 1) % 4
                cam = self.config["cameras"][idx]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = VLCPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, row, col)
                self.panels.append(panel)

        elif layout == "1big_2small":
            if num_cameras > 0:
                cam = self.config["cameras"][0]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = VLCPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, 0, 0, 1, cols)
                self.panels.append(panel)
            for idx in range(1, min(num_cameras, 3)):
                row = (idx - 1) // 3 + 1
                col = (idx - 1) % 3
                cam = self.config["cameras"][idx]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = VLCPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, row, col)
                self.panels.append(panel)

        elif layout == "1big_6small":
            if num_cameras > 0:
                cam = self.config["cameras"][0]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = VLCPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, 0, 0, 1, cols)
                self.panels.append(panel)
            for idx in range(1, min(num_cameras, 7)):
                row = (idx - 1) // 4 + 1
                col = (idx - 1) % 4
                cam = self.config["cameras"][idx]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = VLCPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, row, col)
                self.panels.append(panel)

    def _start_all(self):
        for panel in self.panels:
            panel.start()
        self._update_overall_status()

    def _update_overall_status(self):
        statuses = [p.status_label.text() for p in self.panels]
        connected = sum(1 for s in statuses if s == "Connected")
        total = len(statuses)
        self._overall_label.setText(f"Connected: {connected}/{total}")
        if connected == total:
            self._overall_label.setStyleSheet("color: green;")
        elif connected > 0:
            self._overall_label.setStyleSheet("color: orange;")
        else:
            self._overall_label.setStyleSheet("color: red;")

    def closeEvent(self, event):
        log.info("Shutting down…")
        for panel in self.panels:
            panel.stop()
        event.accept()


# ── Entry point ───────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))

    window = CameraWall()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
