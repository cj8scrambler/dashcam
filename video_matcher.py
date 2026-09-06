"""
Matches a GPS timestamp to the dashcam video segment(s) that cover it.

Filename format (Vantrue N4S, "Normal" recording mode):
    YYYYMMDD_HHMMSS_<seq>_N_<channel>.MP4
    e.g. 20260904_175735_00001_N_A.MP4

<seq> is a 5-digit per-boot sequence number (unused, ignored).
<channel> is A (front), B (interior); C (rear). Loop segment length
is detected from the data itself - see _detect_loop_seconds().

This scans only "Normal" (continuous driving) recordings. Dedicated
parking-mode clips - lower framerate, time-lapse style - live in a separate
Parking/ subdirectory that isn't scanned yet.

A segment's coverage window ends at start + detected loop length, but is
capped at the next same-channel segment's start when the camera resumed
sooner: a Normal segment cut short when the ignition turned off (this rig
has no extended-power wiring, so the camera only runs briefly on accessory
power after shutdown), a camera restart, or the first/last segment of a
drive. See scan_video_dir() and VideoSegment.coverage_end().
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

FILENAME_PATTERN = re.compile(
    r"(?P<date>\d{8})_(?P<time>\d{6})_(?P<seq>\d+)_N_(?P<channel>[A-Za-z])\.(mp4|MP4)"
)

CHANNEL_MAP = {
    "A": "front",
    "B": "interior",
    "C": "rear",
}

# Only used as a last resort when there's not enough data to detect the real
# loop length (e.g. a single video file total, so there's no gap to measure).
DEFAULT_SEGMENT_LENGTH_SECONDS = 300


@dataclass
class VideoSegment:
    path: Path
    start_time: datetime         # UTC, tz-aware
    channel: str                 # "front" / "rear" / "interior"
    duration_s: float            # detected loop-recording length, in seconds
    # Coverage window end. Nominally start_time + duration_s, but capped at the
    # next same-channel segment's start when the camera resumed sooner than a
    # full loop (ignition-off power cut mid-segment, camera restart, a drive's
    # last segment). Set by scan_video_dir() once neighbors are known; falls
    # back to the nominal value if a VideoSegment is built directly.
    end_time: datetime | None = None

    def coverage_end(self) -> datetime:
        return self.end_time or self.start_time + timedelta(seconds=self.duration_s)


def _detect_loop_seconds(start_times_by_channel: dict[str, list[datetime]]) -> float:
    """
    Loop-recording length isn't a fixed constant across camera models/
    firmware, and it's a user-changeable setting (Vantrue offers 1/3/5
    minutes) - so detect it from the actual gaps between consecutive
    same-channel segment start times instead of assuming a value.

    Uses the median gap rather than an exact-match mode: real data has
    occasional short/long gaps (camera restarts, an ignition-off power cut,
    a truncated final segment) that would otherwise dilute or skew a vote-
    based approach, but a real fixed loop length still dominates the
    distribution, so the median lands on it cleanly.
    """
    gaps = []
    for times in start_times_by_channel.values():
        times = sorted(times)
        for a, b in zip(times, times[1:]):
            delta = (b - a).total_seconds()
            if delta > 0:
                gaps.append(delta)

    if not gaps:
        return DEFAULT_SEGMENT_LENGTH_SECONDS
    return round(statistics.median(gaps))


def scan_video_dir(directory: str | Path, record_tz: ZoneInfo) -> list[VideoSegment]:
    """
    Scan a directory for dashcam video files and parse their start times.

    record_tz is the timezone the camera's clock was set to (the filename
    timestamp is naive wall-clock time); it's localized to record_tz and
    converted to UTC so segment times are directly comparable to GpsFix
    timestamps from gps_parser.py.
    """
    directory = Path(directory)
    parsed = []  # (path, start_time_utc, channel), before we know the loop length
    for path in directory.iterdir():
        m = FILENAME_PATTERN.match(path.name)
        if not m:
            continue
        dt_local = datetime.strptime(
            m.group("date") + m.group("time"), "%Y%m%d%H%M%S"
        ).replace(tzinfo=record_tz)
        dt_utc = dt_local.astimezone(timezone.utc)
        channel = CHANNEL_MAP.get(m.group("channel").upper(), m.group("channel"))
        parsed.append((path, dt_utc, channel))

    start_times_by_channel = defaultdict(list)
    for _, dt_utc, channel in parsed:
        start_times_by_channel[channel].append(dt_utc)
    duration_s = _detect_loop_seconds(start_times_by_channel)

    segments = [VideoSegment(path, dt_utc, channel, duration_s) for path, dt_utc, channel in parsed]
    segments.sort(key=lambda s: s.start_time)

    # Cap each segment's coverage window at the next same-channel segment's
    # start. Real data has segments far shorter than duration_s (the camera
    # cut off when the ignition turned off, a restart, a drive's last
    # segment), sometimes with a real gap before recording resumes - without
    # this cap a short segment would "cover" a dead span its file can't fill,
    # and synced playback would stall trying to advance through it.
    next_start: dict[str, datetime] = {}
    for seg in reversed(segments):
        nominal_end = seg.start_time + timedelta(seconds=seg.duration_s)
        following = next_start.get(seg.channel)
        seg.end_time = min(nominal_end, following) if following else nominal_end
        next_start[seg.channel] = seg.start_time

    return segments


def find_segments_for_timestamp(
    segments: list[VideoSegment], ts: datetime
) -> dict[str, VideoSegment]:
    """
    Return the video segment(s) - one per channel - that cover the given timestamp.
    """
    result: dict[str, VideoSegment] = {}
    for seg in segments:
        if seg.start_time <= ts < seg.coverage_end():
            result[seg.channel] = seg
    return result
