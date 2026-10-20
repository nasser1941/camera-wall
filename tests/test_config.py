"""Tests for camera_wall.config (loading, validation, where the file is)."""

import json
from pathlib import Path

import pytest

from camera_wall import config
from camera_wall.config import ConfigError, get_config_path, load_config, parse_grid


def write(tmp_path: Path, data) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    return path


@pytest.mark.parametrize("text", ["2x3", "2X3", "2×3", " 2 x 3 ", "2 X 3"])
def test_parse_grid_accepts_common_spellings(text):
    assert parse_grid(text) == (2, 3)


@pytest.mark.parametrize("text", ["", "2", "2x", "x3", "2-3", "0x3", "9x1", "2x9", "a x b"])
def test_parse_grid_rejects_bad_grids(text):
    with pytest.raises(ConfigError):
        parse_grid(text)


def test_load_config_fills_in_defaults(tmp_path):
    path = write(tmp_path, {"cameras": [{"ip": "10.0.0.5"}]})
    assert load_config(path) == {
        "rows": 2,
        "cols": 3,
        "cameras": [
            {
                "name": "Camera 1",
                "ip": "10.0.0.5",
                "port": 554,
                "username": "admin",
                "password": "",
                "channel": 1,
                "stream": "main",
                "aspect_ratio": None,
            }
        ],
        "hardware_decoders": None,
    }


def test_load_config_reads_every_field(tmp_path):
    cam = {"name": "Gate", "ip": "10.0.0.9", "port": "8554", "username": "viewer",
           "password": "p@ss:word", "channel": 2, "stream": "SUB"}
    path = write(tmp_path, {"grid": "1x2", "cameras": [cam]})
    result = load_config(path)
    assert (result["rows"], result["cols"]) == (1, 2)
    assert result["cameras"][0] == {**cam, "port": 8554, "stream": "sub", "aspect_ratio": None}


def test_load_config_accepts_grid_layout_key(tmp_path):
    path = write(tmp_path, {"grid_layout": "3x3", "cameras": [{"ip": "10.0.0.5"}]})
    assert load_config(path)["rows"] == 3


def test_grid_override_wins_even_over_a_bad_grid(tmp_path):
    path = write(tmp_path, {"grid": "nonsense", "cameras": [{"ip": "10.0.0.5"}]})
    result = load_config(path, grid_override="1x1")
    assert (result["rows"], result["cols"]) == (1, 1)


def test_missing_file_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.json")


@pytest.mark.parametrize(
    "data",
    [
        "{not json",
        [],
        {"cameras": []},
        {"cameras": "10.0.0.5"},
        {"cameras": [{"name": "no ip"}]},
        {"cameras": ["10.0.0.5"]},
        {"cameras": [{"ip": "10.0.0.5", "stream": "hd"}]},
        {"cameras": [{"ip": "10.0.0.5", "port": "rtsp"}]},
        {"cameras": [{"ip": "10.0.0.5", "channel": None}]},
        {"grid": "0x0", "cameras": [{"ip": "10.0.0.5"}]},
        {"hardware_decoders": -1, "cameras": [{"ip": "10.0.0.5"}]},
        {"hardware_decoders": "2", "cameras": [{"ip": "10.0.0.5"}]},
        {"hardware_decoders": True, "cameras": [{"ip": "10.0.0.5"}]},
        {"aspect_ratio": "wide", "cameras": [{"ip": "10.0.0.5"}]},
        {"cameras": [{"ip": "10.0.0.5", "aspect_ratio": "16:0"}]},
        {"cameras": [{"ip": "10.0.0.5", "aspect_ratio": "100:1"}]},
    ],
)
def test_invalid_configs_raise_config_error(tmp_path, data):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, data))


def test_config_path_priority(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path / "app")
    monkeypatch.setattr(config, "BUNDLED_CONFIG", tmp_path / "bundled.json")
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    # Nothing exists: point at where the user's file should go
    assert get_config_path() == tmp_path / "app" / "config.json"

    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "env.json"))
    assert get_config_path() == tmp_path / "env.json"
    assert get_config_path(str(tmp_path / "given.json")) == tmp_path / "given.json"


def test_bundled_config_is_the_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path / "app")
    monkeypatch.setattr(config, "BUNDLED_CONFIG", tmp_path / "bundled.json")
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)

    (tmp_path / "bundled.json").write_text("{}")
    assert get_config_path() == tmp_path / "bundled.json"  # built into the APK

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "config.json").write_text("{}")
    assert get_config_path() == tmp_path / "app" / "config.json"  # a pushed config wins

    # --config and the environment variable never fall back to the bundled config
    assert get_config_path(str(tmp_path / "given.json")) == tmp_path / "given.json"
    monkeypatch.setenv(config.CONFIG_ENV, str(tmp_path / "env.json"))
    assert get_config_path() == tmp_path / "env.json"


def test_bundled_config_lives_in_the_package():
    assert config.BUNDLED_CONFIG.parent == Path(config.__file__).parent
    assert config.BUNDLED_CONFIG.name == "bundled_config.json"


def test_desktop_app_dir_is_in_home(monkeypatch):
    monkeypatch.delenv("ANDROID_ARGUMENT", raising=False)
    monkeypatch.setattr(config.sys, "platform", "linux")
    config.app_dir.cache_clear()
    try:
        assert not config.is_android()
        assert config.app_dir() == Path.home() / ".camera_wall"
    finally:
        config.app_dir.cache_clear()


def test_android_is_detected_from_python_for_android(monkeypatch):
    monkeypatch.setenv("ANDROID_ARGUMENT", "/data/user/0/org.camerawall.camerawall/files/app")
    assert config.is_android()


@pytest.mark.parametrize("value", [0, 2, 16])
def test_hardware_decoders_option(tmp_path, value):
    path = write(tmp_path, {"hardware_decoders": value, "cameras": [{"ip": "10.0.0.5"}]})
    assert load_config(path)["hardware_decoders"] == value


@pytest.mark.parametrize(
    "text, ratio",
    [("16:9", 16 / 9), ("4:3", 4 / 3), ("16/9", 16 / 9), (" 16 x 9 ", 16 / 9), ("1.78", 1.78), (2, 2.0),
     (None, None), ("auto", None), ("", None)],
)
def test_parse_aspect_ratio(text, ratio):
    from camera_wall.config import parse_aspect_ratio

    assert parse_aspect_ratio(text) == (pytest.approx(ratio) if ratio else None)


def test_aspect_ratio_for_all_cameras_and_per_camera(tmp_path):
    path = write(tmp_path, {
        "aspect_ratio": "16:9",
        "cameras": [{"ip": "10.0.0.5"}, {"ip": "10.0.0.6", "aspect_ratio": "4:3"}, {"ip": "10.0.0.7", "aspect_ratio": "auto"}],
    })
    ratios = [cam["aspect_ratio"] for cam in load_config(path)["cameras"]]
    assert ratios == [pytest.approx(16 / 9), pytest.approx(4 / 3), None]
