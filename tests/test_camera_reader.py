"""Tests for camera_wall.camera_reader's pure helpers (no camera or network needed)."""

import av
import pytest

from camera_wall.camera_reader import build_rtsp_url, describe_error, fit_size, redact_password


def camera(**overrides):
    cam = {"ip": "10.0.0.5", "port": 554, "username": "admin", "password": "secret",
           "channel": 1, "stream": "main"}
    cam.update(overrides)
    return cam


def test_rtsp_url_main_stream():
    assert build_rtsp_url(camera()) == (
        "rtsp://admin:secret@10.0.0.5:554/cam/realmonitor?channel=1&subtype=0&unicast=true&proto=Onvif"
    )


def test_rtsp_url_sub_stream_port_and_channel():
    url = build_rtsp_url(camera(stream="sub", port=8554, channel=3))
    assert "@10.0.0.5:8554/" in url
    assert "channel=3&subtype=1&" in url


def test_rtsp_url_quotes_special_characters_in_credentials():
    url = build_rtsp_url(camera(username="ad min", password="p@ss:w/rd#1"))
    assert url.startswith("rtsp://ad%20min:p%40ss%3Aw%2Frd%231@10.0.0.5:554/")


def test_redact_password_in_url_and_in_error_messages():
    url = build_rtsp_url(camera(password="p@ss:w/rd"))
    assert "p%40ss" not in redact_password(url)
    assert redact_password(url).startswith("rtsp://admin:***@10.0.0.5:554/")
    message = f"[Errno 1094995529] Invalid data found when processing input: '{url}'"
    assert "p%40ss" not in redact_password(message)
    assert redact_password("no url here") == "no url here"


@pytest.mark.parametrize(
    "frame, box, expected",
    [
        ((2560, 1440), (640, 360), (640, 360)),  # same shape: fills the box
        ((2560, 1440), (640, 480), (640, 360)),  # wider than the box: limited by width
        ((704, 576), (640, 300), (366, 300)),  # taller than the box: limited by height
        ((704, 576), (1920, 1080), (704, 576)),  # never enlarged
        ((705, 577), (2000, 2000), (704, 576)),  # even sizes
        ((1920, 1080), (1, 1), (2, 2)),  # never below 2x2
    ],
)
def test_fit_size(frame, box, expected):
    assert fit_size(*frame, *box) == expected


def test_describe_error_is_short_and_specific():
    unauthorized = av.error.HTTPUnauthorizedError(1, "Unauthorized", "rtsp://admin:secret@10.0.0.5/")
    assert describe_error(unauthorized) == "wrong username or password"
    assert describe_error(TimeoutError()) == "no answer (check the address)"
    invalid = av.error.InvalidDataError(1094995529, "Invalid data found when processing input")
    assert "enabled" in describe_error(invalid)
    assert describe_error(RuntimeError("anything")) == "check the camera"


def test_lag_is_measured_against_the_freshest_packet():
    from camera_wall.camera_reader import LagTracker

    tracker = LagTracker()
    # Camera clock 1000 s ahead of ours: offsets cancel out, a live stream has no lag
    assert tracker.lag(media_time=1000.0, now=0.0) == 0
    assert abs(tracker.lag(media_time=1001.0, now=1.0)) < 1e-9
    # A backlog builds up: packets arrive later and later than their media time
    assert abs(tracker.lag(media_time=1002.0, now=4.0) - 2.0) < 1e-9
    # Draining the backlog (packets read faster than real time) brings the lag back down
    assert abs(tracker.lag(media_time=1004.0, now=4.2) - 0.2) < 1e-9


def test_a_startup_burst_of_old_frames_does_not_count_as_live():
    from camera_wall.camera_reader import LagTracker

    tracker = LagTracker()
    # The first frames come in a burst (2 s of buffered video in 0.1 s), then live frames
    tracker.lag(media_time=0.0, now=0.0)
    tracker.lag(media_time=2.0, now=0.1)  # this one is the freshest: offset -1.9
    assert abs(tracker.lag(media_time=3.0, now=1.1)) < 1e-9  # live again: no lag


def test_fit_size_shows_a_squeezed_sub_stream_in_its_true_shape():
    # Dahua D1 sub stream: a 16:9 picture squeezed into 704x576 pixels
    assert fit_size(704, 576, 620, 440, aspect=16 / 9) == (620, 348)
    # Without the hint it keeps the (wrong) nearly square shape
    assert fit_size(704, 576, 620, 440) == (536, 440)
    # A tall cell: limited by the width
    assert fit_size(704, 576, 300, 440, aspect=16 / 9) == (300, 168)
