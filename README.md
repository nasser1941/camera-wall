# camera-wall

Live view of Dahua IP cameras in a grid. It plays each camera's RTSP stream directly (no NVR
needed), reconnects on its own when a stream drops, and fits the window to your screen.

## Run it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python camera_wall.py
```

The first time, there's no configuration yet: the app writes an example to
`~/.camera_wall/config.json` and tells you so. Put your cameras in it (below) and start it again.

The window opens at 90% of the screen it starts on. Each video is scaled into its cell, so you can
maximize the window, move it to another screen or resize it freely.

## The configuration file

```json
{
  "grid": "2x3",
  "cameras": [
    {"name": "Main door", "ip": "192.168.1.101", "username": "admin", "password": "CHANGE_ME", "stream": "main"},
    {"name": "Parking",   "ip": "192.168.1.102", "username": "admin", "password": "CHANGE_ME", "stream": "main"}
  ]
}
```

[`config.example.json`](config.example.json) has a complete example with six cameras.

The app looks for the file in this order:

1. the path you give with `--config`, e.g. `camera_wall.py --config ~/office-cameras.json`;
2. the `CAMERA_WALL_CONFIG` environment variable;
3. `~/.camera_wall/config.json`.

It holds your cameras' passwords, so keep it out of git (`config.json` is in `.gitignore`) and
readable only by you: `chmod 600 ~/.camera_wall/config.json`.

### Adding a camera

Add an entry to `cameras`. Cameras fill the grid left to right, top to bottom, in the order of the
list.

| Field | | Default |
|---|---|---|
| `ip` | The camera's address (required) | |
| `name` | Shown under the video | `Camera N` |
| `username`, `password` | The camera's login | `admin`, empty |
| `stream` | `main` (full quality) or `sub` (lighter: use it for many cameras or a slow network) | `main` |
| `channel` | The channel, for a camera or encoder with several | `1` |
| `port` | The RTSP port | `554` |

### Changing the grid

Set `grid` to `rows x columns`, from `1x1` to `8x8`:

| `grid` | Cameras |
|---|---|
| `2x3` | 6 (the default) |
| `3x3` | 9 |
| `3x4` | 12 |
| `4x4` | 16 |

With more cameras than cells, the first ones are shown; empty cells say "No camera". To try a
grid without editing the file: `camera_wall.py --grid 3x3`.

More cameras at full quality take more network and CPU. If the video stutters, switch some
cameras to `"stream": "sub"`.

## Status

The border and the line under each video show its state: grey while connecting, green when
connected, orange while reconnecting, red when it keeps failing (check the camera's address,
login and RTSP settings). The status bar counts the connected cameras.

## A single-file build

```bash
.venv/bin/pip install pyinstaller
.venv/bin/pyinstaller --name camera-wall --windowed --onefile camera_wall.py
./dist/camera-wall
```

The build reads the same configuration file; nothing about your cameras is built into it.

## Troubleshooting

- **No video:** check that the camera answers (`ping 192.168.1.101`) and that RTSP is on in its
  settings.
- **"Error — check the camera":** the login is wrong, or the camera allows no more RTSP
  connections (close other viewers).
- **Stuttering:** use `"stream": "sub"` for some cameras.
- **Logs:** run it from a terminal to see what each camera does (passwords are masked).

## License

MIT
