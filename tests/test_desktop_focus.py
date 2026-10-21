"""Double-click a camera to fill the window with it; double-click again for the grid (desktop app, off screen)."""

import importlib.util
import os
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtCore import QEventLoop, Qt, QTimer  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class FakeReader:
    """Solid frames at whatever size the panel asks for: no camera, no network."""

    def __init__(self, name, url, **options):
        self.status, self.size, self.frames = ("Connected", "green"), (320, 240), 0

    def set_output_size(self, width, height):
        self.size = (max(int(width), 1), max(int(height), 1))

    def start(self):
        pass

    def stop(self):
        pass

    def join(self, timeout=None):
        pass

    def latest_frame(self):
        self.frames += 1
        width, height = self.size
        return self.frames, np.full((height, width, 3), 200, np.uint8)


@pytest.fixture(scope="module")
def desktop():
    spec = importlib.util.spec_from_file_location("desktop_app", ROOT / "camera_wall.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CameraReader = FakeReader
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    return module, app


def pump(ms=200):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@pytest.fixture
def wall(desktop):
    module, _ = desktop
    cameras = [
        {"name": f"Camera {i}", "ip": f"192.0.2.{i}", "port": 554, "username": "u", "password": "p",
         "channel": 1, "stream": "main", "aspect_ratio": None}
        for i in range(1, 6)
    ]
    window = module.CameraWall({"rows": 2, "cols": 3, "cameras": cameras, "hardware_decoders": 0})
    window.resize(1200, 800)
    window.show()
    window.start()
    pump()
    yield window
    window.close()


def double_click(panel):
    QTest.mouseDClick(panel.video, Qt.MouseButton.LeftButton)  # on the video, as a user would
    pump()


def visible(window):
    return [i for i, panel in enumerate(window.panels) if panel.isVisible()]


def test_double_click_fills_the_window_and_double_click_again_brings_the_grid_back(wall):
    camera = wall.panels[4]  # second row, middle column
    grid_size = camera.reader.size
    double_click(camera)
    assert visible(wall) == [4]
    assert camera.video.width() > 2 * grid_size[0] and camera.reader.size[0] > 2 * grid_size[0]
    double_click(camera)
    assert visible(wall) == [0, 1, 2, 3, 4, 5]
    assert camera.reader.size == grid_size


def test_it_works_again_and_again(wall):
    for _ in range(3):
        double_click(wall.panels[0])
        assert visible(wall) == [0]
        double_click(wall.panels[0])
    assert visible(wall) == [0, 1, 2, 3, 4, 5]
    assert wall.grid.count() == 6  # every camera is still in the grid


def test_an_empty_cell_does_nothing(wall):
    double_click(wall.panels[5])  # 5 cameras: cell 6 is empty
    assert visible(wall) == [0, 1, 2, 3, 4, 5] and wall.focused is None
