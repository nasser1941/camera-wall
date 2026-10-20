"""Dahua RTSP streams: the URL, and a background reader per camera that decodes with PyAV.

PyAV is FFmpeg for Python. It is used instead of OpenCV because python-for-android builds
OpenCV without FFmpeg, which leaves it unable to open RTSP streams. PyAV also scales and
converts each frame to RGB in one FFmpeg call, outside the GIL.

The UI thread never waits on a camera: it reads the newest frame and status whenever it
redraws (:meth:`CameraReader.latest_frame`, :attr:`CameraReader.status`), so a slow or dead
camera can't freeze the wall, and frames the screen has no time for are simply skipped.

Readers decode with the video hardware when there is some: Android's MediaCodec (TV
processors badly need it for H.265), an NVIDIA GPU (NVDEC, which also scales the frames down
to the cell's size) or Intel Quick Sync. A decoder that doesn't work hands over to the next
one, down to software decoding, by itself. Android devices only run a few hardware decoders
at once, so there the wall decides which cameras get one (see hardware.py).
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

# Hardware decoders by codec, best first: Android's MediaCodec, NVIDIA's NVDEC (cuvid), Intel's
# Quick Sync (qsv). Each FFmpeg build has only some: python-for-android's has MediaCodec,
# PyAV's desktop wheels have cuvid and qsv.
HARDWARE_DECODERS = {
    "h264": ("h264_mediacodec", "h264_cuvid", "h264_qsv"),
    "hevc": ("hevc_mediacodec", "hevc_cuvid", "hevc_qsv"),
}
# Decoders that also scale the frames down on the GPU (their "resize" option), which spares the
# CPU most of the work per frame.
GPU_SCALING = frozenset({"h264_cuvid", "hevc_cuvid"})
# Re-make a GPU-scaling decoder when the cell's size changes by more than this fraction.
RESCALE_THRESHOLD = 0.1
# Packets a hardware decoder may take to show its first picture before we give up on it (~5 s).
HARDWARE_PATIENCE = 125

# Which hardware decoders worked or failed in this process. One that has failed twice and never
# worked doesn't work on this machine, and other cameras skip it. (A failure after others worked
# is a limit on how many run at once, which only concerns the camera that hit it.)
_decoder_results: dict[str, list[int]] = {}  # name -> [successes, failures]
_decoder_results_lock = threading.Lock()

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


def hardware_decoder_names(codec_name: str) -> list[str]:
    """The hardware decoders to try for ``codec_name``, best first: those this FFmpeg has, minus
    those that turned out not to work on this machine."""
    names = [name for name in HARDWARE_DECODERS.get(codec_name, ()) if name in av.codecs_available]
    with _decoder_results_lock:
        return [name for name in names if not _is_broken(name)]


def _is_broken(name: str) -> bool:
    successes, failures = _decoder_results.get(name, (0, 0))
    return successes == 0 and failures >= 2


def _record_decoder_result(name: str, worked: bool) -> None:
    with _decoder_results_lock:
        counts = _decoder_results.setdefault(name, [0, 0])
        counts[0 if worked else 1] += 1


def describe_decoder(name: str | None, scaled_to: tuple[int, int] | None, threads: int) -> str:
    """How a camera is decoded, for the log."""
    if name is None:
        return f"software decoding ({threads or 'auto'} thread(s))"
    if scaled_to:
        return f"hardware decoding ({name}, scaled on the GPU to {scaled_to[0]}x{scaled_to[1]})"
    return f"hardware decoding ({name})"


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
        self._failed_decoders: set[str] = set()  # hardware decoders that failed for this camera
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
            codec = stream.codec_context
            self._shown_aspect = self._aspect or stream_aspect(codec)
            candidates = [n for n in hardware_decoder_names(codec.name) if n not in self._failed_decoders]
            if not self._hardware:
                candidates = []
            decoder, name, scaled_to = self._make_decoder(stream, candidates)
            shape = f" shown as {self._shown_aspect:.2f}:1" if self._shown_aspect else ""
            log.info(
                "%s: connected, %s %dx%d%s, %s", self.name, codec.name, codec.width, codec.height, shape,
                describe_decoder(name, scaled_to, self._threads),
            )
            lag_tracker = LagTracker()
            skipping = False  # dropping packets to catch up with live
            need_keyframe = False  # dropping packets until a new decoder can start
            shown = 0  # pictures shown since connecting
            pictures = fed = 0  # pictures from, and packets fed to, the current decoder
            for packet in container.demux(stream):
                if self._stopping.is_set():
                    return
                if packet.pts is not None and packet.time_base is not None:
                    lag = lag_tracker.lag(float(packet.pts * packet.time_base), time.monotonic())
                    if skipping and packet.is_keyframe and lag <= CATCH_UP_LAG:
                        skipping = False
                    elif not skipping and lag > MAX_LAG and shown:
                        skipping = True
                        self._note_skip(lag)
                    if skipping:
                        continue  # drop it undecoded: the next keyframe starts a clean picture
                if need_keyframe:
                    if not packet.is_keyframe:
                        continue
                    need_keyframe = False
                if scaled_to and packet.is_keyframe and self._needs_rescale(codec, scaled_to):
                    # The cell changed size: scale on the GPU to the new size from this keyframe on
                    decoder = None
                    decoder, name, scaled_to = self._make_decoder(stream, [name])
                    pictures = fed = 0
                    log.info("%s: cell resized: %s", self.name, describe_decoder(name, scaled_to, self._threads))

                failure = None
                try:
                    frames = decoder.decode(packet)
                except av.error.FFmpegError as exc:
                    if name is None or pictures:
                        raise
                    frames, failure = [], str(exc)
                fed += 1
                for frame in frames:
                    self._publish(frame)
                    pictures += 1
                    shown += 1
                    if pictures == 1 and name:
                        _record_decoder_result(name, worked=True)
                if name and not pictures and not failure and fed >= HARDWARE_PATIENCE:
                    failure = f"no picture after {fed} packets"
                if failure:
                    # Hand over to the next decoder (software at last), from the next keyframe
                    self._failed_decoders.add(name)
                    _record_decoder_result(name, worked=False)
                    candidates = [n for n in candidates if n not in self._failed_decoders]
                    decoder = None
                    decoder, next_name, scaled_to = self._make_decoder(stream, candidates)
                    log.warning(
                        "%s: %s doesn't work (%s): %s", self.name, name, failure,
                        describe_decoder(next_name, scaled_to, self._threads),
                    )
                    name, pictures, fed, need_keyframe = next_name, 0, 0, True
        finally:
            decoder = None  # free the decoder now: devices have only a few hardware ones
            container.close()

    def _make_decoder(self, stream, candidates: list[str]):
        """``(decoder, name, scaled_to)``: the first of the hardware decoders ``candidates`` that
        can be created, else the stream's own software decoder (name None). ``scaled_to`` is the
        size the decoder scales frames to on the GPU, if it does."""
        software = stream.codec_context
        for name in candidates:
            try:
                decoder = av.CodecContext.create(name, "r")
                decoder.extradata = software.extradata  # parameter sets (VPS/SPS/PPS) from RTSP
                decoder.width, decoder.height = software.width, software.height
                scaled_to = None
                if name in GPU_SCALING:
                    scaled_to = self._target_size(software)
                    decoder.options = {"resize": f"{scaled_to[0]}x{scaled_to[1]}"}
                return decoder, name, scaled_to
            except Exception as exc:  # noqa: BLE001 - any failure: try the next one
                self._failed_decoders.add(name)
                _record_decoder_result(name, worked=False)
                log.warning("%s: can't use %s (%s)", self.name, name, exc)
        software.thread_count = self._threads
        if self._threads != 1:
            software.thread_type = "AUTO"  # frame + slice threads
        return software, None, None

    def _target_size(self, codec) -> tuple[int, int]:
        """The size the stream's frames are shown at in the cell, as it is now."""
        with self._lock:
            box_width, box_height = self._box
        return fit_size(codec.width, codec.height, box_width, box_height, self._shown_aspect)

    def _needs_rescale(self, codec, scaled_to: tuple[int, int]) -> bool:
        width, height = self._target_size(codec)
        return (
            abs(width - scaled_to[0]) > RESCALE_THRESHOLD * scaled_to[0]
            or abs(height - scaled_to[1]) > RESCALE_THRESHOLD * scaled_to[1]
        )

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
