"""Tests for camera_wall.hardware: which cameras get the device's hardware decoders."""

from camera_wall import hardware
from camera_wall import camera_reader
from camera_wall.camera_reader import hardware_decoder_names
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
    assert plan_decoders(cams("main", "sub"), None) == [True, True]  # desktop: no known limit


def test_one_thread_each_when_cameras_outnumber_cores():
    assert software_threads(4, cpu_count=4) == 1
    assert software_threads(6, cpu_count=4) == 1
    assert software_threads(6, cpu_count=32) == 0  # desktop: let FFmpeg choose
    assert software_threads(0, cpu_count=4) == 0


def test_no_limit_off_android(monkeypatch):
    monkeypatch.setattr(hardware, "is_android", lambda: False)
    hardware_decoder_limit.cache_clear()
    try:
        assert hardware_decoder_limit() is None
    finally:
        hardware_decoder_limit.cache_clear()


def test_hardware_decoders_in_order_of_preference(monkeypatch):
    import av

    monkeypatch.setattr(av, "codecs_available", {"hevc", "hevc_cuvid", "hevc_qsv", "h264", "h264_mediacodec"})
    monkeypatch.setattr(camera_reader, "_decoder_results", {})
    assert hardware_decoder_names("hevc") == ["hevc_cuvid", "hevc_qsv"]  # desktop FFmpeg
    assert hardware_decoder_names("h264") == ["h264_mediacodec"]  # Android FFmpeg
    assert hardware_decoder_names("mjpeg") == []


def test_a_decoder_that_never_works_is_skipped(monkeypatch):
    import av

    monkeypatch.setattr(av, "codecs_available", {"hevc", "hevc_cuvid", "hevc_qsv"})
    monkeypatch.setattr(camera_reader, "_decoder_results", {})
    camera_reader._record_decoder_result("hevc_qsv", worked=False)
    assert hardware_decoder_names("hevc") == ["hevc_cuvid", "hevc_qsv"]  # one failure: maybe a fluke
    camera_reader._record_decoder_result("hevc_qsv", worked=False)
    assert hardware_decoder_names("hevc") == ["hevc_cuvid"]  # never worked, failed twice: broken


def test_a_decoder_that_worked_is_kept_after_failures(monkeypatch):
    import av

    # e.g. a TV that runs 5 hardware decoders at once: the 6th camera's failure isn't the decoder's fault
    monkeypatch.setattr(av, "codecs_available", {"h264", "h264_mediacodec"})
    monkeypatch.setattr(camera_reader, "_decoder_results", {})
    camera_reader._record_decoder_result("h264_mediacodec", worked=True)
    for _ in range(3):
        camera_reader._record_decoder_result("h264_mediacodec", worked=False)
    assert hardware_decoder_names("h264") == ["h264_mediacodec"]
