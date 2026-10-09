#!/usr/bin/env python3
"""
Dahua Camera Wall — Parametric multi-camera live viewer for Dahua IP cameras.

Shows cameras in configurable grid layouts (2×3, 3×3, 4×4, 1+5, 1+3, etc.)
with per-camera credentials and stream selection.
Configuration is stored in a JSON file and editable via a settings panel.
"""

import sys
import os
import json
import time
import logging
from pathlib import Path
from typing import Any, Dict
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QRect, QSize
from PyQt6.QtGui import QFont, QColor, QPixmap, QImage
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGridLayout,
    QLabel, QVBoxLayout, QFrame, QStatusBar,
    QDialog, QFormLayout, QLineEdit, QPushButton,
    QComboBox, QMessageBox, QGroupBox, QScrollArea,
    QSplitter, QHBoxLayout, QHeaderView, QTableWidget,
    QTableWidgetItem, QAbstractItemView, QFileDialog,
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
    "1×1": {"rows": 1, "cols": 1},
    "2×2": {"rows": 2, "cols": 2},
    "3×3": {"rows": 3, "cols": 3},
    "4×4": {"rows": 4, "cols": 4},
    "2×3": {"rows": 2, "cols": 3},
    "3×2": {"rows": 3, "cols": 2},
    "1+5": {"rows": 2, "cols": 3, "layout": "1big_5small"},
    "1+3": {"rows": 2, "cols": 4, "layout": "1big_3small"},
    "1+2": {"rows": 2, "cols": 3, "layout": "1big_2small"},
    "1+6": {"rows": 3, "cols": 4, "layout": "1big_6small"},
}

# Normalized lookup: maps ASCII 'x' versions to Unicode keys
_GRID_LAYOUT_NORMALIZATION = {
    k.replace("×", "x"): k for k in GRID_LAYOUTS.keys()
}


def _normalize_layout_name(name: str) -> str:
    """Normalize a layout name for dictionary lookup."""
    return _GRID_LAYOUT_NORMALIZATION.get(name, name)

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
                # Normalize to Unicode key for consistency
                config["grid_layout"] = _normalize_layout_name(config["grid_layout"])
            if "cameras" not in config or not config["cameras"]:
                config["cameras"] = DEFAULT_CONFIG["cameras"]
            log.info("Loaded config from %s", config_path)
            return config
        except (json.JSONDecodeError, KeyError) as e:
            log.error("Failed to load config: %s", e)
    log.info("Using default configuration")
    return DEFAULT_CONFIG.copy()


def save_config(config: Dict[str, Any]) -> bool:
    """Save configuration to file."""
    config_path = get_config_path()
    try:
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
        log.info("Saved config to %s", config_path)
        return True
    except IOError as e:
        log.error("Failed to save config: %s", e)
        return False


# ── RTSP URL builder ───────────────────────────────────────────────────
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
        # Don't block the calling thread — let the thread exit naturally
        # when it checks _running in its loop

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
class CameraPanel(QFrame):
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


# ── Configuration dialog ───────────────────────────────────────────────
class ConfigDialog(QDialog):
    """Dialog for configuring camera settings and grid layout."""

    def __init__(self, config: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Camera Wall Configuration")
        self.setMinimumSize(800, 600)

        # Layout
        layout = QVBoxLayout(self)

        # Tabs for different sections
        self._add_grid_section(layout)
        self._add_camera_table(layout)
        self._add_button_box(layout)

    def _add_grid_section(self, parent_layout):
        """Add grid layout selection section."""
        group = QGroupBox("Grid Layout")
        form = QFormLayout(group)

        self.grid_combo = QComboBox()
        self.grid_combo.addItems(list(GRID_LAYOUTS.keys()))
        current = self.config.get("grid_layout", "2x3")
        idx = self.grid_combo.findText(current)
        if idx >= 0:
            self.grid_combo.setCurrentIndex(idx)

        form.addRow("Layout:", self.grid_combo)
        parent_layout.addWidget(group)

    def _add_camera_table(self, parent_layout):
        """Add camera configuration table."""
        group = QGroupBox("Cameras")
        layout = QVBoxLayout(group)

        # Table
        self.table = QTableWidget(len(self.config["cameras"]), 5)
        self.table.setHorizontalHeaderLabels(["Name", "IP Address", "Username", "Password", "Stream"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)

        # Populate table
        for i, cam in enumerate(self.config["cameras"]):
            self.table.setItem(i, 0, QTableWidgetItem(cam.get("name", "")))
            self.table.setItem(i, 1, QTableWidgetItem(cam.get("ip", "")))
            self.table.setItem(i, 2, QTableWidgetItem(cam.get("username", "")))
            self.table.setItem(i, 3, QTableWidgetItem(cam.get("password", "")))
            combo = QComboBox()
            combo.addItems(["main", "sub"])
            combo.setCurrentText(cam.get("stream", "main"))
            self.table.setCellWidget(i, 4, combo)

        layout.addWidget(self.table)

        # Buttons
        btn_layout = QHBoxLayout()
        add_btn = QPushButton("Add Camera")
        add_btn.clicked.connect(self._add_camera)
        remove_btn = QPushButton("Remove Camera")
        remove_btn.clicked.connect(self._remove_camera)
        btn_layout.addWidget(add_btn)
        btn_layout.addWidget(remove_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        parent_layout.addWidget(group)

    def _add_camera(self):
        """Add a new camera row to the table."""
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(f"Camera {row + 1}"))
        self.table.setItem(row, 1, QTableWidgetItem(""))
        self.table.setItem(row, 2, QTableWidgetItem("admin"))
        self.table.setItem(row, 3, QTableWidgetItem(""))
        combo = QComboBox()
        combo.addItems(["main", "sub"])
        combo.setCurrentText("main")
        self.table.setCellWidget(row, 4, combo)

    def _remove_camera(self):
        """Remove selected camera row from the table."""
        current_row = self.table.currentRow()
        if current_row >= 0:
            self.table.removeRow(current_row)

    def _add_button_box(self, parent_layout):
        """Add OK/Cancel buttons."""
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save Configuration")
        save_btn.clicked.connect(self._save)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(cancel_btn)
        parent_layout.addLayout(btn_layout)

    def _save(self):
        """Save configuration from the table."""
        cameras = []
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            ip_item = self.table.item(row, 1)
            user_item = self.table.item(row, 2)
            pass_item = self.table.item(row, 3)
            stream_widget = self.table.cellWidget(row, 4)
            stream_type = stream_widget.currentText() if isinstance(stream_widget, QComboBox) else "main"

            if not name_item or not ip_item or not user_item or not pass_item:
                continue

            cameras.append({
                "name": name_item.text(),
                "ip": ip_item.text(),
                "username": user_item.text(),
                "password": pass_item.text(),
                "stream": stream_type,
            })

        if not cameras:
            QMessageBox.warning(self, "Warning", "At least one camera must be configured.")
            return

        self.config["grid_layout"] = self.grid_combo.currentText()
        self.config["cameras"] = cameras

        if save_config(self.config):
            self.accept()
        else:
            QMessageBox.critical(self, "Error", "Failed to save configuration.")

    def get_config(self) -> Dict[str, Any]:
        """Return the current configuration."""
        return self.config


# ── Main window ───────────────────────────────────────────────────────
class CameraWall(QMainWindow):
    """Main application window with configurable camera grid."""

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.panels = []
        self._was_special = False
        self.setWindowTitle("Dahua Camera Wall")
        self.setMinimumSize(1280, 720)
        self.resize(1600, 900)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._overall_label = QLabel("Initializing…")
        self._overall_label.setFont(QFont("Monospace", 10))
        self.status_bar.addPermanentWidget(self._overall_label)

        # Toolbar
        self._create_toolbar()

        # Central widget with grid
        central = QWidget()
        self.setCentralWidget(central)
        self.grid_layout = QGridLayout(central)
        self.grid_layout.setContentsMargins(8, 8, 8, 8)
        self.grid_layout.setSpacing(8)

        # Create panels from config
        self._build_grid()

        # Start all streams after a short delay
        QTimer.singleShot(1000, self._start_all)

    def _create_toolbar(self):
        """Create toolbar with configuration button."""
        toolbar = self.addToolBar("Main")
        toolbar.setMovable(False)

        config_btn = QPushButton("⚙ Configuration")
        config_btn.clicked.connect(self._open_config)
        toolbar.addWidget(config_btn)

        toolbar.addSeparator()

        # Grid layout selector
        self.grid_selector = QComboBox()
        self.grid_selector.addItems(list(GRID_LAYOUTS.keys()))
        self.grid_selector.currentTextChanged.connect(self._on_grid_changed)
        toolbar.addWidget(self.grid_selector)

    def _open_config(self):
        """Open the configuration dialog."""
        dialog = ConfigDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.config = dialog.get_config()
            # Defer so deleteLater() events are processed
            QTimer.singleShot(0, self._rebuild_grid)
            self._update_grid_selector()

    def _update_grid_selector(self):
        """Update the toolbar grid selector to match current config."""
        raw = self.config.get("grid_layout", "2x3")
        layout_name = _normalize_layout_name(raw)
        idx = self.grid_selector.findText(layout_name)
        if idx >= 0:
            self.grid_selector.setCurrentIndex(idx)

    def _on_grid_changed(self, layout_name: str):
        """Handle grid layout change from toolbar."""
        self.config["grid_layout"] = layout_name
        # Defer to next event loop iteration so deleteLater() events
        # from the old panels are processed before we add new ones
        QTimer.singleShot(0, self._rebuild_grid)

    def _clear_grid(self):
        """Stop and remove all panels from the grid without relying on deleteLater()."""
        for panel in self.panels:
            panel.stop()
            # Wait for the FrameReader thread to actually stop
            if panel.reader.isRunning():
                panel.reader.wait(2000)
            self.grid_layout.removeWidget(panel)
            panel.setParent(None)
            panel.deleteLater()
        self.panels.clear()

    def _build_grid(self):
        """Build the camera grid based on current configuration."""
        # Get grid dimensions
        layout_name = _normalize_layout_name(self.config.get("grid_layout", "2x3"))
        layout_def = GRID_LAYOUTS.get(layout_name, GRID_LAYOUTS["2×3"])
        rows: int = int(layout_def["rows"])
        cols: int = int(layout_def["cols"])
        special_layout = layout_def.get("layout")

        # Check if we're switching between standard and special layouts
        is_special = special_layout is not None
        was_special = getattr(self, "_was_special", False)

        if is_special != was_special:
            # Layout type changed — must clear and rebuild
            log.info("Switching from %s to %s layout — clearing grid",
                     "special" if was_special else "standard",
                     "special" if is_special else "standard")
            self._clear_grid()
            self._was_special = is_special
        elif is_special:
            # Special → special: clear and rebuild (grid structure changes)
            log.info("Switching special layout: %s", layout_name)
            self._clear_grid()
        else:
            # Standard → standard: update panels in place
            self._update_standard_grid(rows, cols)
            self._was_special = False

        # Build panels if we cleared above
        if not self.panels:
            num_cameras = len(self.config["cameras"])
            if is_special:
                self._build_special_grid(rows, cols, special_layout, num_cameras)
            else:
                self._build_standard_grid(rows, cols)

        # Adjust column/row stretches
        for i in range(cols):
            self.grid_layout.setColumnStretch(i, 1)
        for i in range(rows):
            self.grid_layout.setRowStretch(i, 1)

        self._update_overall_status()

    def _update_standard_grid(self, rows: int, cols: int):
        """Update existing panels for a standard grid (no layout type change)."""
        num_cameras = len(self.config["cameras"])
        total_cells = rows * cols
        needed = min(num_cameras, total_cells)

        # Update existing panels in place
        for idx in range(min(len(self.panels), needed)):
            row = idx // cols
            col = idx % cols
            cam = self.config["cameras"][idx]
            stream_subtype = get_stream_subtype(cam.get("stream", "main"))
            rtsp_url = build_rtsp_url(
                cam["ip"], cam.get("username", "admin"),
                cam.get("password", ""), channel=1, subtype=stream_subtype
            )
            panel = self.panels[idx]
            # Stop old thread before restarting with new URL
            panel.stop()
            if panel.reader.isRunning():
                panel.reader.wait(2000)
            panel.camera_name = cam["name"]
            panel.rtsp_url = rtsp_url
            panel.status_label.setText("Connecting…")
            panel.video_label.clear()
            # Reposition in grid
            self.grid_layout.removeWidget(panel)
            self.grid_layout.addWidget(panel, row, col)
            panel.start()

        # Remove excess panels
        for idx in range(needed, len(self.panels)):
            panel = self.panels[idx]
            panel.stop()
            if panel.reader.isRunning():
                panel.reader.wait(2000)
            self.grid_layout.removeWidget(panel)
            panel.setParent(None)
            panel.deleteLater()
        del self.panels[needed:]

        # Add new panels for extra cameras
        for idx in range(len(self.panels), needed):
            row = idx // cols
            col = idx % cols
            cam = self.config["cameras"][idx]
            stream_subtype = get_stream_subtype(cam.get("stream", "main"))
            rtsp_url = build_rtsp_url(
                cam["ip"], cam.get("username", "admin"),
                cam.get("password", ""), channel=1, subtype=stream_subtype
            )
            panel = CameraPanel(cam["name"], rtsp_url, self)
            self.grid_layout.addWidget(panel, row, col)
            self.panels.append(panel)
            panel.start()

    def _build_standard_grid(self, rows: int, cols: int):
        """Build a standard grid from scratch."""
        num_cameras = len(self.config["cameras"])
        total_cells = rows * cols
        for idx in range(min(num_cameras, total_cells)):
            row = idx // cols
            col = idx % cols
            cam = self.config["cameras"][idx]
            stream_subtype = get_stream_subtype(cam.get("stream", "main"))
            rtsp_url = build_rtsp_url(
                cam["ip"], cam.get("username", "admin"),
                cam.get("password", ""), channel=1, subtype=stream_subtype
            )
            panel = CameraPanel(cam["name"], rtsp_url, self)
            self.grid_layout.addWidget(panel, row, col)
            self.panels.append(panel)

    def _build_special_grid(self, rows: int, cols: int, layout: str, num_cameras: int):
        """Build special grid layouts (1 big + N small)."""
        if layout == "1big_5small":
            # 1 big on top, 5 small below (3 cols)
            if num_cameras > 0:
                cam = self.config["cameras"][0]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = CameraPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, 0, 0, 1, cols)
                self.panels.append(panel)

            for idx in range(1, min(num_cameras, 6)):
                row = (idx - 1) // 3 + 1
                col = (idx - 1) % 3
                cam = self.config["cameras"][idx]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = CameraPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, row, col)
                self.panels.append(panel)

        elif layout == "1big_3small":
            # 1 big on top, 3 small below (4 cols)
            if num_cameras > 0:
                cam = self.config["cameras"][0]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = CameraPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, 0, 0, 1, cols)
                self.panels.append(panel)

            for idx in range(1, min(num_cameras, 4)):
                row = (idx - 1) // 4 + 1
                col = (idx - 1) % 4
                cam = self.config["cameras"][idx]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = CameraPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, row, col)
                self.panels.append(panel)

        elif layout == "1big_2small":
            # 1 big on top, 2 small below (3 cols)
            if num_cameras > 0:
                cam = self.config["cameras"][0]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = CameraPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, 0, 0, 1, cols)
                self.panels.append(panel)

            for idx in range(1, min(num_cameras, 3)):
                row = (idx - 1) // 3 + 1
                col = (idx - 1) % 3
                cam = self.config["cameras"][idx]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = CameraPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, row, col)
                self.panels.append(panel)

        elif layout == "1big_6small":
            # 1 big on top, 6 small below (4 cols)
            if num_cameras > 0:
                cam = self.config["cameras"][0]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = CameraPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, 0, 0, 1, cols)
                self.panels.append(panel)

            for idx in range(1, min(num_cameras, 7)):
                row = (idx - 1) // 4 + 1
                col = (idx - 1) % 4
                cam = self.config["cameras"][idx]
                stream_subtype = get_stream_subtype(cam.get("stream", "main"))
                rtsp_url = build_rtsp_url(cam["ip"], cam.get("username", "admin"), cam.get("password", ""), channel=1, subtype=stream_subtype)
                panel = CameraPanel(cam["name"], rtsp_url, self)
                self.grid_layout.addWidget(panel, row, col)
                self.panels.append(panel)

    def _rebuild_grid(self):
        """Rebuild the grid with current configuration."""
        self._build_grid()
        self._update_grid_selector()

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
