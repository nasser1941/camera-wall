"""Tests for camera_wall.hardware: which cameras get the device's hardware decoders."""

from camera_wall import hardware
from camera_wall.camera_reader import hardware_decoder_name
from camera_wall.hardware import hardware_decoder_limit, plan_decoders, software_threads


def cams(*streams):
    return [{"stream": stream} for stream in streams]


def test_main_streams_get_the_hardware_first():
    # Your TV: 2 hardware decoders; camera 3 is the only main stream
    assert plan_decoders(cams("sub", "sub", "main", "sub", "sub", "sub"), 2) == [
        True, False, True, False, False, False,
    ]


def test_plan_in_order_and_within_limits():
    assert plan_decoders(cams("sub", "sub", "sub"), 2) == [True, True, False]
    assert plan_decoders(cams("main", "sub"), 8) == [True, True]
    assert plan_decoders(cams("main", "main"), 0) == [False, False]
    assert plan_decoders([], 2) == []


def test_one_thread_each_when_cameras_outnumber_cores():
    assert software_threads(4, cpu_count=4) == 1
    assert software_threads(6, cpu_count=4) == 1
    assert software_threads(6, cpu_count=32) == 0  # desktop: let FFmpeg choose
    assert software_threads(0, cpu_count=4) == 0


def test_no_hardware_decoding_off_android(monkeypatch):
    monkeypatch.setattr(hardware, "is_android", lambda: False)
    hardware_decoder_limit.cache_clear()
    try:
        assert hardware_decoder_limit() == 0
    finally:
        hardware_decoder_limit.cache_clear()


def test_hardware_decoder_name_needs_ffmpeg_support(monkeypatch):
    import av

    monkeypatch.setattr(av, "codecs_available", {"hevc", "hevc_mediacodec", "h264"})
    assert hardware_decoder_name("hevc") == "hevc_mediacodec"
    assert hardware_decoder_name("h264") is None  # no h264_mediacodec in this FFmpeg
    assert hardware_decoder_name("mjpeg") is None
