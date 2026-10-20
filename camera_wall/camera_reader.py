"""Dahua RTSP streams: the URL, and a background reader per camera that decodes with PyAV.

PyAV is FFmpeg for Python. It is used instead of OpenCV because python-for-android builds
OpenCV without FFmpeg, which leaves it unable to open RTSP streams. PyAV also scales and
converts each frame to RGB in one FFmpeg call, outside the GIL.

The UI thread never waits on a camera: it reads the newest frame and status whenever it
redraws (:meth:`CameraReader.latest_frame`, :attr:`CameraReader.status`), so a slow or dead
camera can't freeze the wall, and frames the screen has no time for are simply skipped.

On Android a reader can decode with the device's video hardware (MediaCodec, through FFmpeg's
*_mediacodec decoders), which TV processors badly need for H.265. Devices only run a few of
those at once, so the wall decides which cameras get one (see hardware.py); a camera whose
hardware decoder fails goes back to software decoding by itself.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from urllib.parse import quote

import av

log = logging.getLogger("camera_wall.camera")

RECONNECT_DELAY = 3.0  # seconds between connection attempts
ERROR_AFTER = 15.0  # seconds of failing before the status turns red
OPEN_TIMEOUT = 10.0  # seconds to connect and read the stream's description
READ_TIMEOUT = 10.0  # seconds without data before the stream counts as dropped

# FFmpeg options for live RTSP: over TCP (lost UDP packets smear the picture, and UDP often
# doesn't get through Wi-Fi or NAT), without input buffering, with a socket timeout (µs).
RTSP_OPTIONS = {
    "rtsp_transport": "tcp",
    "fflags": "nobuffer",
    "timeout": str(int(READ_TIMEOUT * 1_000_000)),
}

# Live, not late: when a camera falls this far behind (seconds), skip to its next keyframe
# instead of playing the backlog (which a slow device would never catch up on).
MAX_LAG = 1.5
# ... and resume decoding at a keyframe no more than this far behind.
CATCH_UP_LAG = 0.5

# Android MediaCodec decoders in FFmpeg, by codec. FFmpeg on desktops doesn't have them.
HARDWARE_DECODERS = {"h264": "h264_mediacodec", "hevc": "hevc_mediacodec"}
# Packets a hardware decoder may take to show its first picture before we give up on it (~5 s).
HARDWARE_PATIENCE = 125

# Status colours, by name; the UI maps them to real colours.
GREY, GREEN, ORANGE, RED = "grey", "green", "orange", "red"
CONNECTED = "Connected"


def build_rtsp_url(cam: dict) -> str:
    """Dahua's RTSP address for a camera (as returned by ``config.load_config``).

    ``rtsp://{user}:{pass}@{ip}:{port}/cam/realmonitor?channel={ch}&subtype={st}&...``,
    where subtype 0 is the main stream and 1 the sub stream.
    """
    subtype = 0 if cam["stream"] == "main" else 1
    credentials = f"{quote(cam['username'], safe='')}:{quote(cam['password'], safe='')}@"
    return (
        f"rtsp://{credentials}{cam['ip']}:{cam['port']}"
        f"/cam/realmonitor?channel={cam['channel']}&subtype={subtype}&unicast=true&proto=Onvif"
    )


def redact_password(text: str) -> str:
    """Mask the password of every RTSP URL in ``text`` (URLs, FFmpeg error messages)."""
    return re.sub(r"(rtsp://[^:/@\s]*):[^@\s]*@", r"\1:***@", text)


def fit_size(
    width: int, height: int, box_width: int, box_height: int, aspect: float | None = None
) -> tuple[int, int]:
    """The size to scale a ``width`` x ``height`` frame to so it fits the box in its true shape.

    ``aspect`` is the picture's real width / height when its pixels aren't square (a 16:9
    camera's 704x576 sub stream); None takes the pixels as they are. Never enlarges beyond the
    picture's own size (the GPU does that for free) and returns even numbers, which the scaler
    handles best."""
    shown_width = height * aspect if aspect else width
    scale = min(box_width / shown_width, box_height / height, 1.0)
    return max(2, int(shown_width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)


def stream_aspect(codec) -> float | None:
    """The picture shape a stream itself declares (pixel aspect ratio), if it declares one."""
    sar = codec.sample_aspect_ratio
    if not sar or not codec.width or not codec.height:
        return None
    return codec.width * float(sar) / codec.height


def hardware_decoder_name(codec_name: str) -> str | None:
    """The FFmpeg decoder that uses the device's video hardware for ``codec_name``, if any."""
    name = HARDWARE_DECODERS.get(codec_name)
    return name if name is not None and name in av.codecs_available else None


class HardwareDecoderError(RuntimeError):
    """The hardware decoder can't decode this stream (so the camera goes back to software)."""


class LagTracker:
    """How far behind live a stream is, from its packets' timestamps.

    The packet with the smallest gap between arrival time and media time is taken as "live";
    a packet's lag is how much later it is than that. Camera clocks and network delay cancel
    out, and a burst of old frames when a stream starts doesn't count as live.
    """

    def __init__(self):
        self._best_offset: float | None = None

    def lag(self, media_time: float, now: float) -> float:
        offset = now - media_time
        if self._best_offset is None or offset < self._best_offset:
            self._best_offset = offset
        return offset - self._best_offset


def describe_error(exc: BaseException) -> str:
    """A short, password-free reason for a failed connection, fit for a status line."""
    if isinstance(exc, av.error.HTTPUnauthorizedError):
        return "wrong username or password"
    if isinstance(exc, (av.error.ExitError, TimeoutError)):
        return "no answer (check the address)"
    if isinstance(exc, av.error.ConnectionRefusedError):
        return "connection refused (check the port)"
    if isinstance(exc, av.error.HTTPNotFoundError):
        return "stream not found (check the channel)"
    if isinstance(exc, av.error.InvalidDataError):  # Dahua's answer for a disabled stream
        return "no stream (is this stream enabled on the camera?)"
    if isinstance(exc, IndexError):
        return "no video in the stream"
    return "check the camera"


class CameraReader:
    """Reads one camera's RTSP stream on a background thread and reconnects when it drops."""

    def __init__(self, name: str, url: str, hardware: bool = False, threads: int = 0, aspect: float | None = None):
        """``hardware``: try the device's hardware decoder first. ``threads``: decoder threads
        for software decoding (0: as many as FFmpeg likes). ``aspect``: the picture's true
        width / height, when the stream's pixels aren't square and it doesn't say so."""
        self.name = name
        self._url = url
        self._aspect = aspect
        self._shown_aspect: float | None = None  # what _publish uses, set when connected
        self._hardware = hardware
        self._threads = threads
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame = None  # newest frame: numpy uint8 array (height, width, 3), RGB
        self._frame_id = 0  # increases with every new frame
        self._box = (640, 360)  # frames are scaled down to fit this (pixels)
        self._status = ("Connecting…", GREY)
        self._failing_since: float | None = None
        self._last_error = ""
        self._skips = 0  # times this camera skipped ahead to stay live
        self._last_skip_log = float("-inf")

    # ── Called from the UI thread ────────────────────────────────────────

    def start(self) -> None:
        """Start reading in the background."""
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"cam-{self.name}")
        self._thread.start()

    def stop(self) -> None:
        """Ask the reader to stop, without waiting (see :meth:`join`)."""
        self._stopping.set()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the reader to stop. It notices :meth:`stop` at its next frame, or at the
        latest after READ_TIMEOUT when the camera has gone silent."""
        if self._thread:
            self._thread.join(timeout)

    def set_output_size(self, width: float, height: float) -> None:
        """Scale frames down to fit ``width`` x ``height`` pixels (the cell's video area)."""
        with self._lock:
            self._box = (max(2, int(width)), max(2, int(height)))

    def latest_frame(self):
        """``(frame_id, frame)``: the newest frame, or None before the first one. The id
        changes whenever a new frame arrives."""
        with self._lock:
            return self._frame_id, self._frame

    @property
    def status(self) -> tuple[str, str]:
        """``(text, colour name)`` describing the connection."""
        return self._status

    # ── Background thread ────────────────────────────────────────────────

    def _run(self) -> None:
        log.info("%s: connecting to %s", self.name, redact_password(self._url))
        while not self._stopping.is_set():
            try:
                self._play()
                if self._stopping.is_set():
                    break
                log.info("%s: stream ended, reconnecting", self.name)
                self._failing_since = time.monotonic()
                self._status = ("Reconnecting…", ORANGE)
            except Exception as exc:  # noqa: BLE001 - whatever failed, retry
                if self._stopping.is_set():
                    break
                self._on_failure(exc)
            self._stopping.wait(RECONNECT_DELAY)
        log.info("%s: stopped", self.name)

    def _play(self) -> None:
        """Connect and decode until the stream ends (returns) or fails (raises)."""
        container = av.open(self._url, options=RTSP_OPTIONS, timeout=(OPEN_TIMEOUT, READ_TIMEOUT))
        decoder = None
        try:
            stream = container.streams.video[0]
            decoder, how = self._open_decoder(stream)
            hardware = decoder is not stream.codec_context
            codec = stream.codec_context
            self._shown_aspect = self._aspect or stream_aspect(codec)
            shape = f" shown as {self._shown_aspect:.2f}:1" if self._shown_aspect else ""
            log.info("%s: connected, %s %dx%d%s, %s", self.name, codec.name, codec.width, codec.height, shape, how)
            pictures = 0
            lag_tracker = LagTracker()
            skipping = False
            for packets, packet in enumerate(container.demux(stream), 1):
                if self._stopping.is_set():
                    return
                if packet.pts is not None and packet.time_base is not None:
                    lag = lag_tracker.lag(float(packet.pts * packet.time_base), time.monotonic())
                    if skipping and packet.is_keyframe and lag <= CATCH_UP_LAG:
                        skipping = False
                    elif not skipping and lag > MAX_LAG and pictures:
                        skipping = True
                        self._note_skip(lag)
                    if skipping:
                        continue  # drop it undecoded: the next keyframe starts a clean picture
                try:
                    frames = decoder.decode(packet)
                except av.error.FFmpegError as exc:
                    if hardware and not pictures:
                        raise HardwareDecoderError(str(exc)) from exc
                    raise
                for frame in frames:
                    self._publish(frame)
                    pictures += 1
                if hardware and not pictures and packets >= HARDWARE_PATIENCE:
                    raise HardwareDecoderError(f"no picture after {packets} packets")
        except HardwareDecoderError as exc:
            self._hardware = False  # for good: reconnect with software decoding
            log.warning("%s: hardware decoding failed (%s): switching to software", self.name, exc)
            raise
        finally:
            decoder = None  # free the decoder now: devices have only a few hardware ones
            container.close()

    def _open_decoder(self, stream):
        """``(decoder, description)``: the hardware decoder when this reader may use one and it
        can be created, else the stream's own (software) decoder."""
        software = stream.codec_context
        name = hardware_decoder_name(software.name) if self._hardware else None
        if name:
            try:
                decoder = av.CodecContext.create(name, "r")
                decoder.extradata = software.extradata  # parameter sets (VPS/SPS/PPS) from RTSP
                decoder.width, decoder.height = software.width, software.height
                return decoder, f"hardware decoding ({name})"
            except Exception as exc:  # noqa: BLE001 - any failure: use software
                self._hardware = False
                log.warning("%s: no hardware decoder (%s): using software", self.name, exc)
        software.thread_count = self._threads
        if self._threads != 1:
            software.thread_type = "AUTO"  # frame + slice threads
        return software, f"software decoding ({self._threads or 'auto'} thread(s))"

    def _publish(self, frame) -> None:
        """Scale the frame to fit the cell, convert it to RGB and make it the newest one."""
        with self._lock:
            box_width, box_height = self._box
        width, height = fit_size(frame.width, frame.height, box_width, box_height, self._shown_aspect)
        image = frame.reformat(width=width, height=height, format="rgb24", interpolation="BILINEAR").to_ndarray()
        with self._lock:
            self._frame = image
            self._frame_id += 1
        if self._status[0] != CONNECTED:
            self._status = (CONNECTED, GREEN)
            self._failing_since = None
            self._last_error = ""

    def _note_skip(self, lag: float) -> None:
        """Log catching up, at most once a minute per camera."""
        now = time.monotonic()
        self._skips += 1
        if now - self._last_skip_log >= 60:
            log.info("%s: %.1f s behind live: skipping ahead (%d time(s) so far)", self.name, lag, self._skips)
            self._last_skip_log = now

    def _on_failure(self, exc: Exception) -> None:
        now = time.monotonic()
        if self._failing_since is None:
            self._failing_since = now
        error = redact_password(f"{type(exc).__name__}: {exc}")
        if error != self._last_error:  # log each new problem once, not every retry
            log.warning("%s: %s", self.name, error)
            self._last_error = error
        if now - self._failing_since >= ERROR_AFTER:
            self._status = (f"Error — {describe_error(exc)}", RED)
        else:
            self._status = ("Reconnecting…", ORANGE)
