#!/usr/bin/env python3
"""
Dahua Camera Wall — Multi-camera live viewer for Dahua IP cameras.

Shows 6 cameras in a 2×3 grid with auto-reconnect on stream failure.
Plays direct RTSP streams (no NVR token needed).
Uses OpenCV (FFmpeg) for RTSP — works even when system VLC lacks live555.
"""

import sys
import time
import logging
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

# ── Camera configuration ──────────────────────────────────────────────
CAMERAS = [
    {"name": "Camera 1", "ip": "172.16.1.110"},
    {"name": "Camera 2", "ip": "172.16.1.111"},
    {"name": "Camera 3", "ip": "172.16.1.112"},
    {"name": "Camera 4", "ip": "172.16.1.113"},
    {"name": "Camera 5", "ip": "172.16.1.114"},
    {"name": "Camera 6", "ip": "172.16.1.115"},
]
USERNAME = "admin"
PASSWORD = "***REMOVED***"
RTSP_PORT = 554
RECONNECT_INTERVAL = 3  # seconds between reconnect attempts
RECONNECT_TIMEOUT = 15  # seconds before declaring a camera dead

# ── RTSP URL helpers ──────────────────────────────────────────────────
def build_rtsp_url(camera_ip: str, channel: int = 1, subtype: int = 0) -> str:
    """
    Build Dahua RTSP URL for the main stream.

    Parameters:
        camera_ip: IP address of the camera
        channel: Camera channel number (1-based)
        subtype: 0 = main stream (high quality), 1 = sub stream

    Returns:
        Full RTSP URL string
    """
    return (
        f"rtsp://{USERNAME}:{PASSWORD}@{camera_ip}:{RTSP_PORT}"
        f"/cam/realmonitor?channel={channel}&subtype={subtype}"
        f"&unicast=true&proto=Onvif"
    )


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
                time.sleep(RECONNECT_INTERVAL)
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
            if elapsed < RECONNECT_TIMEOUT:
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
            time.sleep(RECONNECT_INTERVAL)

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
    """Main application window with a 2×3 camera grid."""

    def __init__(self):
        super().__init__()
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
        grid_layout = QGridLayout(central)
        grid_layout.setContentsMargins(8, 8, 8, 8)
        grid_layout.setSpacing(8)
        grid_layout.setColumnStretch(0, 1)
        grid_layout.setColumnStretch(1, 1)
        grid_layout.setColumnStretch(2, 1)
        grid_layout.setRowStretch(0, 1)
        grid_layout.setRowStretch(1, 1)

        # Create panels
        self.panels: list[VLCPanel] = []
        for idx, cam in enumerate(CAMERAS):
            col = idx % 3
            row = idx // 3
            panel = VLCPanel(cam["name"], build_rtsp_url(cam["ip"]), self)
            grid_layout.addWidget(panel, row, col)
            self.panels.append(panel)

        # Start all streams
        QTimer.singleShot(1000, self._start_all)

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
