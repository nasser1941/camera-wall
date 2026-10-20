"""How many cameras the device can decode in hardware at once (Android MediaCodec).

TV processors are slow at decoding video in software, H.265 above all, but the video hardware
next to them is fast. It only runs a few streams at once, though (2 on many TVs), so the wall
gives those to the heaviest cameras and decodes the rest in software.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from camera_wall.config import is_android

log = logging.getLogger("camera_wall.hardware")

# Decoders by MIME type, in the order we ask about them (most cameras send H.265 or H.264).
VIDEO_TYPES = ("video/hevc", "video/avc")
SOFTWARE_PREFIXES = ("omx.google.", "c2.android.", "c2.google.")


@lru_cache(maxsize=1)
def hardware_decoder_limit() -> int:
    """The number of video streams the device's hardware decoder handles at once; 0 when there
    is none, it can't be found out, or this isn't Android."""
    if not is_android():
        return 0
    try:
        return _query_media_codecs()
    except Exception as exc:  # noqa: BLE001 - jnius/Java errors: just don't use hardware
        log.warning("Couldn't query the hardware decoders: %s", exc)
        return 0


def _query_media_codecs() -> int:
    from jnius import autoclass  # pylint: disable=import-outside-toplevel,import-error

    media_codec_list = autoclass("android.media.MediaCodecList")
    infos = media_codec_list(media_codec_list.REGULAR_CODECS).getCodecInfos()
    for mime in VIDEO_TYPES:
        for info in infos:
            name = info.getName()
            lowered = name.lower()
            if info.isEncoder() or lowered.startswith(SOFTWARE_PREFIXES) or ".secure" in lowered:
                continue
            if mime not in [t.lower() for t in info.getSupportedTypes()]:
                continue
            limit = int(info.getCapabilitiesForType(mime).getMaxSupportedInstances())
            log.info("Hardware decoder %s (%s): %d stream(s) at once", name, mime, limit)
            return max(0, limit)
    log.info("No hardware video decoder found")
    return 0


def plan_decoders(cameras: list[dict], hardware_slots: int) -> list[bool]:
    """Which cameras get a hardware decoder: the main streams first (they're the heaviest),
    then the others in order, up to ``hardware_slots``."""
    order = sorted(range(len(cameras)), key=lambda i: (cameras[i]["stream"] != "main", i))
    chosen = set(order[: max(0, hardware_slots)])
    return [i in chosen for i in range(len(cameras))]


def software_threads(software_cameras: int, cpu_count: int) -> int:
    """Decoder threads per software-decoded camera. With as many cameras as cores or more, one
    thread each does the most work: extra threads only add overhead (and memory). Otherwise
    let FFmpeg choose (0)."""
    if software_cameras and software_cameras >= cpu_count:
        return 1
    return 0
