# camera-wall

Multi-camera live viewer for Dahua IP cameras.

Shows cameras in a grid layout that fills the screen. Each camera cell
stretches to fill its available space while preserving aspect ratio.

## Quick Start

```bash
# Unpack the tarball
tar xzf camera-wall-v0.1.0-linux-x86_64.tar.gz

# Run
./release/camera-wall
```

Or run directly from source:

```bash
pip install -r requirements.txt
python camera_wall.py
```

## Configuration

All settings live in a single JSON file:

```
~/.camera_wall/config.json
```

If the file doesn't exist, the app starts with 6 default cameras in a
2×3 grid.

### Default config

```json
{
  "grid_layout": "2x3",
  "cameras": [
    {
      "name": "Camera 1",
      "ip": "172.16.1.110",
      "username": "admin",
      "password": "your_password",
      "stream": "main"
    }
  ]
}
```

### Adding cameras

Add entries to the `"cameras"` array. Each entry needs:

| Field      | Description                              |
|------------|------------------------------------------|
| `name`     | Display name (shown in the status label) |
| `ip`       | Camera IP address                        |
| `username` | RTSP username (e.g. `admin`)             |
| `password` | RTSP password                            |
| `stream`   | `main` (high quality) or `sub` (low)     |

Example with 9 cameras in a 3×3 grid:

```json
{
  "grid_layout": "3x3",
  "cameras": [
    {"name": "Front Gate", "ip": "172.16.1.110", "username": "admin", "password": "***REMOVED***", "stream": "main"},
    {"name": "Parking Lot", "ip": "172.16.1.111", "username": "admin", "password": "***REMOVED***", "stream": "main"},
    {"name": "Entrance", "ip": "172.16.1.112", "username": "admin", "password": "***REMOVED***", "stream": "main"},
    {"name": "Back Door", "ip": "172.16.1.113", "username": "admin", "password": "***REMOVED***", "stream": "main"},
    {"name": "Corridor A", "ip": "172.16.1.114", "username": "admin", "password": "***REMOVED***", "stream": "main"},
    {"name": "Corridor B", "ip": "172.16.1.115", "username": "admin", "password": "***REMOVED***", "stream": "main"},
    {"name": "Lobby", "ip": "172.16.1.116", "username": "admin", "password": "***REMOVED***", "stream": "main"},
    {"name": "Roof", "ip": "172.16.1.117", "username": "admin", "password": "***REMOVED***", "stream": "main"},
    {"name": "Loading Bay", "ip": "172.16.1.118", "username": "admin", "password": "***REMOVED***", "stream": "main"}
  ]
}
```

### Changing the grid layout

Set `"grid_layout"` to one of:

| Value   | Layout          | Description                  |
|---------|-----------------|------------------------------|
| `1x1`   | 1×1             | Single camera, full screen   |
| `2x2`   | 2×2             | 4 cameras                    |
| `3x3`   | 3×3             | 9 cameras                    |
| `4x4`   | 4×4             | 16 cameras                   |
| `2x3`   | 2×3             | 6 cameras (default)          |
| `3x2`   | 3×2             | 6 cameras (portrait)         |
| `1+5`   | 1 big + 5 small | Spotlight camera + 5 others  |
| `1+3`   | 1 big + 3 small | Spotlight camera + 3 others  |
| `1+2`   | 1 big + 2 small | Spotlight camera + 2 others  |
| `1+6`   | 1 big + 6 small | Spotlight camera + 6 others  |

After editing the config file, restart the app for changes to take effect.

### Stream selection

- `main` — high-resolution main stream (default)
- `sub` — low-resolution sub stream (better for many cameras or limited bandwidth)

## Troubleshooting

- **Camera shows "Reconnecting..."**: Check that the IP, username, and
  password are correct. Dahua cameras accept at most 6 simultaneous RTSP
  connections by default.
- **No video but status says "Connected"**: Try switching the camera
  stream from `main` to `sub` — some Dahua firmware versions have issues
  with the main stream over RTSP.
- **App won't start**: Make sure you have the required libraries:
  `opencv-python-headless`, `PyQt6`, and `numpy`.
