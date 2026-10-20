"""
Matches a GPS timestamp to the dashcam video segment(s) that cover it.

Filename format (Vantrue N4S) is the same shape for every recording mode:
    YYYYMMDD_HHMMSS_<seq>_<mode>_<channel>.MP4
    e.g. 20260904_175735_00001_N_A.MP4 (Normal), 20260907_185256_00182_T_A.MP4
    (Parking-mode Timelapse)

<seq> is a 5-digit per-boot sequence number (unused, ignored).
<channel> is A (front), B (interior); C (rear). <mode> is "N" for Normal
(continuous driving, the default scan_video_dir() mode), "T" for Parking-mode
Timelapse, "E" for a motion/impact Event - inside the top-level Event/
directory (see scan_event_dir/EventClip) or inside Parking/ (that second
location still not handled - see CLAUDE.md's "Possible future work"). Loop
segment length is detected from the data itself - see _detect_loop_seconds().

A segment's coverage window ends at start + detected loop length, but is
capped at the next same-channel segment's start when the camera resumed
sooner: a Normal segment cut short when the ignition turned off (this rig
has no extended-power wiring, so the camera only runs briefly on accessory
power after shutdown), a camera restart, or the first/last segment of a
drive. See scan_video_dir() and VideoSegment.coverage_end(). The same
gap-to-next-segment technique also turns out to correctly measure a Timelapse
file's real-world span - confirmed against real Parking/ footage by reading
the wall-clock timestamp the camera burns into its own frames (see
CLAUDE.md) - which is what makes VideoSegment.compression_ratio possible
below: Parking-mode recording compresses that whole real span into a much
shorter playable file (a configurable, non-constant amount - never assume a
rate, measure it per file from data already on hand, the same philosophy as
_detect_loop_seconds() itself).
"""
from __future__ import annotations

import re
import statistics
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import video_transcode

FILENAME_PATTERN = re.compile(
    r"(?P<date>\d{8})_(?P<time>\d{6})_(?P<seq>\d+)_N_(?P<channel>[A-Za-z])\.(mp4|MP4)"
)


def _filename_pattern(mode: str) -> re.Pattern[str]:
    """
    The Normal-mode ("N") pattern is FILENAME_PATTERN itself (kept as a public
    constant - other code may already reference it); every other mode is
    compiled on demand from the same filename shape with a different
    mode-marker letter.
    """
    if mode == "N":
        return FILENAME_PATTERN
    return re.compile(
        rf"(?P<date>\d{{8}})_(?P<time>\d{{6}})_(?P<seq>\d+)_{re.escape(mode)}_"
        rf"(?P<channel>[A-Za-z])\.(mp4|MP4)"
    )


CHANNEL_MAP = {
    "A": "front",
    "B": "interior",
    "C": "rear",
}

# Only used as a last resort when there's not enough data to detect the real
# loop length (e.g. a single video file total, so there's no gap to measure).
DEFAULT_SEGMENT_LENGTH_SECONDS = 300

# extend_to_next's ceiling on how large a same-channel gap can still be
# trusted as one continuous Timelapse session (see the long comment in
# scan_video_dir). Real confirmed per-chunk intervals topped out at 360s;
# this gives ~5x headroom above that before assuming two unrelated sessions.
MAX_TIMELAPSE_EXTEND_S = 30 * 60

# How often (seconds) scan_video_dir(measure_duration=True) prints a progress
# line while ffprobing files - see that loop's comment for why this exists at
# all. Time-based, not count-based, so a fully-cached re-scan (near-instant -
# see _probe_file_duration's cache) doesn't spam dozens of progress lines
# just because there happen to be thousands of files, while a genuinely slow
# cold probe (confirmed live 2026-10-15: ~4 minutes for 3732 real Parking
# files on first boot after a container rebuild) still gets a heartbeat.
DURATION_PROBE_LOG_INTERVAL_S = 5.0


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
    # file_duration_s / (coverage_end() - start_time) in real seconds - 1.0 for
    # realtime recording (Normal; also the default for a directly-built
    # segment). Parking-mode Timelapse compresses its whole coverage window
    # into a much shorter file, so seeking needs this to translate "N real
    # seconds into the segment" into "N * compression_ratio seconds into the
    # file". Set by scan_video_dir(measure_duration=True).
    compression_ratio: float = 1.0

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


# Duration-probe cache for measure_duration=True scans, keyed by resolved path
# string -> (mtime_ns, size, ffprobe-measured duration_s). scan_video_dir()
# never opens files for a plain (Normal) scan; this cost only applies to
# Parking/Timelapse/Event scans, and the cache keeps a LATER re-scan from
# re-probing files that haven't changed. A plain in-memory dict - NOT
# persisted anywhere - so it only helps re-scans WITHIN this same running
# process (DASHCAM_RESCAN_INTERVAL, the reload trigger file); it's empty
# again after every process restart (a container rebuild/recreate, a
# gunicorn worker respawn), and that first scan always re-probes everything
# regardless of how recently a PREVIOUS process already measured those same
# files - confirmed live 2026-10-15, not a bug (see _duration_cached's
# docstring for the progress-line confusion this caused). Guarded the same
# way as gps_parser's _parse_cache, since a periodic re-scan and a
# trigger-file re-scan could otherwise race.
_duration_cache: dict[str, tuple[int, int, float]] = {}
_duration_cache_lock = threading.Lock()


def _probe_file_duration(path: Path) -> float | None:
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    sig = (st.st_mtime_ns, st.st_size)
    with _duration_cache_lock:
        cached = _duration_cache.get(key)
        if cached and cached[:2] == sig:
            return cached[2]
    duration = video_transcode.probe_duration_seconds(path)
    if duration is not None:
        with _duration_cache_lock:
            _duration_cache[key] = (*sig, duration)
    return duration


def _duration_cached(path: Path) -> bool:
    """
    True if path already has a valid (matching mtime+size) entry in
    _duration_cache, without probing it if not - used only to report real
    numbers in scan_video_dir's progress line ("N already cached, M to
    probe"), since _duration_cache is a plain in-memory dict with NO
    persistence across a process restart: it only helps a later re-scan
    WITHIN the same running process (DASHCAM_RESCAN_INTERVAL, the reload
    trigger), never across a container restart/rebuild. Confirmed live
    2026-10-15: right after a rebuild, a scan of thousands of untouched
    Parking files still probed every one of them - expected, not a bug
    (cache is cold), but the progress line's wording made it read like the
    cache should already be active, so this exists to make the count
    honest instead.
    """
    try:
        st = path.stat()
    except OSError:
        return False
    with _duration_cache_lock:
        cached = _duration_cache.get(str(path))
    return bool(cached and cached[:2] == (st.st_mtime_ns, st.st_size))


def scan_video_dir(
    directory: str | Path, record_tz: ZoneInfo, *, mode: str = "N",
    measure_duration: bool = False, extend_to_next: bool = False,
) -> list[VideoSegment]:
    """
    Scan a directory for dashcam video files matching the given recording-mode
    letter and parse their start times.

    record_tz is the timezone the camera's clock was set to (the filename
    timestamp is naive wall-clock time); it's localized to record_tz and
    converted to UTC so segment times are directly comparable to GpsFix
    timestamps from gps_parser.py.

    mode selects which recording type to scan (see the module docstring for
    the mode-letter meanings) - "N" (Normal) by default.

    measure_duration additionally ffprobes each matched file's real playable
    duration and sets VideoSegment.compression_ratio (see that field's
    comment). Needed for Parking-mode Timelapse ("T"); leave False for Normal
    scans, where it would be pure overhead - realtime recording is always
    ratio 1.0, and unlike a filename-only scan this opens every file.

    extend_to_next changes how coverage_end() is capped - see the comment
    above that computation. Leave False for Normal (the default); pass True
    for Timelapse, where - unlike Normal's fixed, card-wide loop length -
    confirmed against real data that the real interval between chunks varies
    *per session* (seen: 180s, 300s, and 360s across sessions on the same
    card), so duration_s's single global median cannot be trusted to detect
    a truncated segment the way it can for Normal.
    """
    directory = Path(directory)
    pattern = _filename_pattern(mode)
    parsed = []  # (path, start_time_utc, channel), before we know the loop length
    for path in directory.iterdir():
        m = pattern.match(path.name)
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
    # start - two different reasons depending on mode, hence extend_to_next:
    #
    # Normal (extend_to_next=False, default): real data has segments far
    # shorter than duration_s (the camera cut off when the ignition turned
    # off, a restart, a drive's last segment), sometimes with a real gap
    # before recording resumes - min(nominal_end, following) makes a segment
    # cover the SHORTER of "its typical loop length" and "until the next one
    # actually starts", so a genuine parked gap between drives is never
    # wrongly claimed by the segment before it.
    #
    # Timelapse (extend_to_next=True): the opposite problem. duration_s here
    # is a single global median across every session on the card, but the
    # real chunking interval varies *per session* (confirmed: 180s/300s/360s
    # coexist) - so min() can wrongly cap a segment's coverage short using an
    # unrelated session's interval, discarding real footage. A Timelapse file
    # compresses its *entire* real session span into one file by design, so
    # the correct coverage end is simply "the next same-channel segment's
    # start" whenever one exists - PROVIDED that gap is still plausibly the
    # same continuous parking session. Confirmed real per-chunk intervals
    # topped out at 360s (6 min); MAX_TIMELAPSE_EXTEND_S gives that generous
    # headroom, but a gap far beyond it (observed: 9+ hours, spanning what was
    # obviously a full day including driving) means two unrelated sessions,
    # not one continuous stop - extending across it would misreport actual
    # drive time as "parked". Beyond the ceiling, fall back to the same
    # min(nominal_end, following) Normal uses - the global median is an
    # imperfect estimate here too, but nowhere near as wrong as bridging the
    # whole gap. duration_s/nominal_end is also the only option for an actual
    # session's last segment, where there's no next start to measure against.
    next_start: dict[str, datetime] = {}
    for seg in reversed(segments):
        nominal_end = seg.start_time + timedelta(seconds=seg.duration_s)
        following = next_start.get(seg.channel)
        if following:
            plausibly_continuous = extend_to_next and (following - seg.start_time).total_seconds() <= MAX_TIMELAPSE_EXTEND_S
            seg.end_time = following if plausibly_continuous else min(nominal_end, following)
        else:
            seg.end_time = nominal_end
        next_start[seg.channel] = seg.start_time

    if measure_duration:
        # This loop is the slow part of scanning a measure_duration directory
        # (Parking, Event): it opens every file via ffprobe, one subprocess
        # each. On a cold cache (a fresh container, or newly-arrived files)
        # that's real wall-clock time with NO other feedback otherwise - the
        # caller (app.py's _install_state/reload_state) only prints once the
        # whole scan returns, and gunicorn's preload_app doesn't even start
        # listening until that happens (see CLAUDE.md's "Docker" section) -
        # so without this, the process looks hung/dead for however long the
        # probe takes (confirmed live 2026-10-15: ~4 silent minutes for 3732
        # real Parking files). Progress is logged on a timer
        # (DURATION_PROBE_LOG_INTERVAL_S), not every N files, so a fully-
        # cached re-scan - the common case once warm - doesn't spam the log.
        total = len(segments)
        label = directory.name
        if total:
            # Real counts, not a generic claim about caching - _duration_cache
            # is per-process and empty on every fresh start (see
            # _duration_cached's docstring), so saying "cached by mtime+size"
            # without saying how many ACTUALLY are right now reads as a
            # promise this particular run doesn't keep.
            already_cached = sum(1 for seg in segments if _duration_cached(seg.path))
            print(f"Probing real duration for {total} {label} file(s) via ffprobe "
                  f"({already_cached} already cached from an earlier scan this process, "
                  f"{total - already_cached} to probe now)...",
                  flush=True)
        started = time.monotonic()
        last_logged = started
        for i, seg in enumerate(segments, start=1):
            real_span_s = (seg.coverage_end() - seg.start_time).total_seconds()
            file_duration_s = _probe_file_duration(seg.path)
            if file_duration_s and real_span_s > 0:
                seg.compression_ratio = file_duration_s / real_span_s
            now = time.monotonic()
            if i == total or now - last_logged >= DURATION_PROBE_LOG_INTERVAL_S:
                print(f"  {label} duration probe: {i}/{total} files ({now - started:.1f}s elapsed)",
                      flush=True)
                last_logged = now

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


@dataclass
class EventClip:
    """
    One channel's file from a motion/impact Event clip (mode "E", inside the
    top-level Event/ directory). Deliberately NOT a VideoSegment: events are
    sporadic and accelerometer-triggered (impact, at a configurable
    sensitivity - not purely motion, and not parking-only: confirmed capable
    of firing mid-drive too, e.g. hitting a pothole), not loop-recorded, so
    the loop-length / coverage-window-capped-at-the-next-segment machinery
    VideoSegment exists for (see scan_video_dir's docstring) doesn't apply -
    there's no "next same-channel segment" to cap against.

    duration_s (real, playable seconds - realtime, NOT a Timelapse-style
    compression ratio) is set by scan_event_dir(measure_duration=True).
    Confirmed via ffprobe against all 224 real files in one real Event/
    directory (2026-10-15): most (151/224) are exactly 300.000000s, and the
    rest range from ~21s to ~290s - the same truncation-on-early-stop
    pattern already documented for Normal/Parking (ignition off mid-
    recording, a camera restart, end of a session) rather than Timelapse's
    variable-per-session compression interval. This supersedes an earlier,
    unverified note in this docstring claiming "16.7s and 60s observed" -
    that didn't hold up against the real data once it was actually measured.
    0.0 (the default) means not yet measured.

    Still NOT verified: exactly where within the file the real trigger
    instant falls. The maintainer manually inspected one real clip
    (2026-10-15) and reports the impact at ~60s in, with ~4 more minutes
    recorded after - consistent with the common 300s duration above (a
    1-minute pre-roll + 4-minute post-roll buffer) but confirmed against
    only that one clip, not measured from the camera's own burned-in
    timestamp the way Timelapse's compression_ratio was before it was
    trusted. app.py's DASHCAM_EVENT_TRIGGER_OFFSET_S (default 60s) treats
    this as a tunable estimate, not a verified constant - don't harden
    assumptions around it without checking more real clips first.

    coverage_end() (added once duration_s existed to measure it with) makes
    an EventClip's real recorded window - [start_time, coverage_end()) -
    usable with find_segments_for_timestamp() exactly like a VideoSegment's,
    so clicking ANYWHERE along the map path within that window (not just the
    marker, which always dispatches start_time itself) resolves to this
    clip, seeked to the right real-time offset (confirmed live 2026-10-15:
    clicking a point partway through an Event's halo on the map previously
    hit "no video found", since app.py's /api/videos only matched an exact
    `ts == start_time`, never any instant *within* the clip). Realtime, not
    capped: NO "next same-channel segment" clamp the way VideoSegment's
    coverage_end() has, since Events are discrete, independent clips, not
    samples from a continuous loop - just start_time + the file's own
    measured duration_s. A clip with duration_s still 0 (unmeasured)
    produces an empty window (coverage_end() == start_time), so it matches
    nothing here rather than being guessed as covering anything.
    """
    path: Path
    start_time: datetime   # UTC, tz-aware - this file's own recording start
    channel: str
    duration_s: float = 0.0

    def coverage_end(self) -> datetime:
        return self.start_time + timedelta(seconds=self.duration_s)


def scan_event_dir(
    directory: str | Path, record_tz: ZoneInfo, *, measure_duration: bool = False
) -> list[EventClip]:
    """
    Scan directory (the top-level Event/ dir) for motion/impact Event clips
    (mode "E") and parse their start times - same filename shape and
    record_tz localization as scan_video_dir, but none of its loop-length
    detection or coverage-window capping, which assume continuous loop
    recording (see EventClip's docstring for why that doesn't apply here).

    measure_duration additionally ffprobes each file's real playable
    duration via the same cached _probe_file_duration() Parking/Timelapse
    scans use (keyed by (mtime_ns, size), so a re-scan only re-probes files
    that changed) - cheap here since an Event/ directory is typically a few
    hundred files, nowhere near Parking's scale. Grouping same-moment
    multi-channel files together, and locating them on the map via the
    nearest GPS fix, is app.py's job.
    """
    directory = Path(directory)
    pattern = _filename_pattern("E")
    # Just a start/end bracket, not a per-file heartbeat like scan_video_dir's
    # measure_duration loop below - an Event/ directory is typically a few
    # hundred files (ffprobed inline in the loop below when measure_duration),
    # nowhere near the multi-minute Parking case that loop's progress lines
    # exist for, so there's no real silent gap here to fill in. Deliberately
    # says nothing about caching (unlike that loop's header) rather than
    # guessing a hit/miss count without actually checking one - see
    # _duration_cached's docstring for why an unchecked claim about caching
    # here would risk the same confusion.
    if measure_duration:
        print(f"Scanning {directory.name}/ (ffprobing each file for real duration)...", flush=True)
    started = time.monotonic()
    clips = []
    for path in directory.iterdir():
        m = pattern.match(path.name)
        if not m:
            continue
        dt_local = datetime.strptime(
            m.group("date") + m.group("time"), "%Y%m%d%H%M%S"
        ).replace(tzinfo=record_tz)
        dt_utc = dt_local.astimezone(timezone.utc)
        channel = CHANNEL_MAP.get(m.group("channel").upper(), m.group("channel"))
        duration_s = (_probe_file_duration(path) or 0.0) if measure_duration else 0.0
        clips.append(EventClip(path, dt_utc, channel, duration_s))
    clips.sort(key=lambda c: c.start_time)
    if measure_duration:
        print(f"  {directory.name} scan: {len(clips)} clips in {time.monotonic() - started:.1f}s", flush=True)
    return clips
