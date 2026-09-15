"""
Dashcam viewer - speed-colored GPS track on a zoomable map, synced to
front/interior/rear video playback.

Usage:
    python3 app.py --data-dir /path/to/data

--data-dir must contain two subdirectories:
    Normal/   dashcam video files
    GPS/      *.dat GPS log files (each may span multiple days)

Then open http://localhost:5000 in a browser.
"""
from __future__ import annotations

import argparse
import bisect
import os
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

import auth
import config
import gps_parser
import video_transcode
from gps_parser import load_gps_dir
from video_matcher import scan_video_dir, find_segments_for_timestamp

app = Flask(__name__)

# The parsed-data snapshot every request reads. Rebuilt by reload_state() and
# swapped in with a single assignment (atomic in CPython) - a request sees either
# the whole old snapshot or the whole new one, never a mix, so no read lock is
# needed. Routes must always index STATE fresh (STATE["fixes"]), never capture a
# reference across the swap. Populated once at startup by _install_state().
STATE = {
    "fixes": [],
    "video_segments": [],
    "video_dir": None,
    "parking_segments": [],      # Parking-mode Timelapse ("T") segments, [] if Parking/ absent
    "display_tz": None,
    "segments_by_channel": {},   # channel -> that channel's Normal segments, time-sorted
    "segment_by_filename": {},   # e.g. "20260904_182500_00002_N_A.MP4" -> VideoSegment; both Normal + Parking
    "loaded_at": None,
    "_data_dir": None,           # kept so reload_state() can rebuild with the same config
    "_record_tz": None,
}

# Serializes rebuilds so the periodic re-scan and the trigger-file re-scan can't
# run load_gps_dir()/scan_video_dir() over each other.
_reload_lock = threading.Lock()
_rescan_thread_started = False

# Plain-text, one-filename-per-line list of videos flagged for deletion - see
# api_delete_request(). /data is read-only to this process by design, so this
# is only ever a request list; the maintainer deletes the real files by hand.
PENDING_DELETES_PATH = config.CONFIG_DIR / "pending_deletes.txt"


def _local_date(dt_utc, tz) -> str:
    return dt_utc.astimezone(tz).date().isoformat()


def _day_bounds_utc(date_str: str, tz) -> tuple[datetime, datetime]:
    """
    UTC [start, end) for one display-timezone calendar day, e.g. "2026-09-10" in
    America/Chicago -> that day's local midnight through the next one, converted
    to UTC (width varies across a DST transition).

    Unlike _local_date() above - a POINT test used by /api/track and /api/stops
    for GPS fixes and stop endpoints - a VideoSegment's coverage window is a
    SPAN. Filtering spans by _local_date(seg.start_time) alone would wrongly
    drop a segment that starts just before local midnight but covers into the
    target day. Used by /api/coverage, which tests span overlap instead.

    Raises ValueError on a malformed date_str (unlike /api/track's and
    /api/stops's date param, which is never parsed, only string-compared, so
    garbage there just silently matches nothing) - the caller turns this into
    a 400, since this endpoint actually has to parse it to compute bounds.
    """
    y, m, d = (int(p) for p in date_str.split("-"))
    local_midnight = datetime(y, m, d, tzinfo=tz)
    start = local_midnight.astimezone(timezone.utc)
    end = (local_midnight + timedelta(days=1)).astimezone(timezone.utc)
    return start, end


@app.route("/")
def index():
    return render_template("index.html", auth_enabled=auth.enabled())


@app.route("/healthz")
def healthz():
    """
    Unauthenticated liveness/readiness probe for the container healthcheck.
    200 only once the GPS data has actually loaded; 503 otherwise.
    """
    ready = bool(STATE["fixes"]) or bool(STATE["video_segments"])
    return (
        jsonify({"status": "ok" if ready else "loading",
                 "fixes": len(STATE["fixes"]),
                 "segments": len(STATE["video_segments"]),
                 "loaded_at": STATE["loaded_at"].isoformat() if STATE["loaded_at"] else None}),
        200 if ready else 503,
    )


@app.route("/api/days")
def api_days():
    """
    Return every calendar day (in the display timezone) that has GPS data,
    for driving the calendar chooser: [{"date": "2026-07-04", "count": 812}, ...]
    """
    tz = STATE["display_tz"]
    counts: dict[str, int] = defaultdict(int)
    for fx in STATE["fixes"]:
        counts[_local_date(fx.timestamp, tz)] += 1
    days = [{"date": d, "count": n} for d, n in sorted(counts.items())]
    return jsonify(days)


@app.route("/api/track")
def api_track():
    """
    Return the GPS track as JSON for the map to draw.

    Optional ?date=YYYY-MM-DD (interpreted in the display timezone) restricts
    the result to fixes recorded on that calendar day; omitted, returns
    everything.
    """
    tz = STATE["display_tz"]
    date_raw = request.args.get("date")
    fixes = STATE["fixes"]
    if date_raw:
        fixes = [fx for fx in fixes if _local_date(fx.timestamp, tz) == date_raw]

    points = [
        {
            "t": fx.timestamp.isoformat(),               # UTC - used as the lookup key for /api/videos
            "t_local": fx.timestamp.astimezone(tz).isoformat(),  # display timezone - used for showing to the user
            "lat": fx.lat,
            "lon": fx.lon,
            "speed_mph": round(fx.speed_mph, 1),
            "speed_kmh": round(fx.speed_kmh, 1),
            "alt_m": fx.altitude_m,
        }
        for fx in fixes
    ]
    return jsonify(points)


def _stop_min_duration_s() -> float:
    try:
        minutes = float(os.environ.get("DASHCAM_STOP_MIN_MINUTES", "5"))
    except ValueError:
        minutes = 5.0
    return max(minutes, 0.0) * 60.0


def _stop_max_speed_mph() -> float:
    try:
        mph = float(os.environ.get("DASHCAM_STOP_MAX_SPEED_MPH", "2"))
    except ValueError:
        mph = 2.0
    return max(mph, 0.0)


def _detect_stationary_stops(fixes, min_duration_s: float, max_speed_mph: float):
    """
    Find runs of consecutive fixes (from a time-sorted list) whose speed never
    exceeds max_speed_mph, sustained for at least min_duration_s - e.g. a
    stoplight shouldn't turn into a "stop" entry, a 20-minute errand should.
    Returns [(start_fix, end_fix), ...], the first and last fix of each
    qualifying run. A GPS data gap inside an otherwise-stationary run doesn't
    split it - only a fix that actually reports speed above the threshold
    does; the vehicle presumably didn't move just because reception briefly
    dropped while parked.
    """
    stops = []
    run_start = None
    prev = None
    for fx in fixes:
        if fx.speed_mph <= max_speed_mph:
            if run_start is None:
                run_start = fx
            prev = fx
            continue
        if run_start is not None and (prev.timestamp - run_start.timestamp).total_seconds() >= min_duration_s:
            stops.append((run_start, prev))
        run_start = None
        prev = fx
    if run_start is not None and (prev.timestamp - run_start.timestamp).total_seconds() >= min_duration_s:
        stops.append((run_start, prev))
    return stops


def _last_fix_at_or_before(ts):
    """
    Binary search STATE["fixes"] (time-sorted, checked globally rather than
    just the requested day - the relevant fix may be from earlier, even a
    prior day) for the last real GPS fix at or before `ts`. Used to backfill a
    location for a Parking-mode stop, whose own time window may have no GPS
    coverage at all (the vehicle can be fully powered down while parked) -
    since the vehicle doesn't move during a stop, its last known position
    beforehand is still accurate.
    """
    fixes = STATE["fixes"]
    idx = bisect.bisect_right(fixes, ts, key=lambda fx: fx.timestamp) - 1
    return fixes[idx] if idx >= 0 else None


def _stop_json(start_utc, end_utc, lat, lon, tz, *, video_start_utc=None, video_count=0) -> dict:
    d = {
        "start": start_utc.isoformat(),
        "start_local": start_utc.astimezone(tz).isoformat(),
        "end": end_utc.isoformat(),
        "end_local": end_utc.astimezone(tz).isoformat(),
        "lat": lat,
        "lon": lon,
        "duration_s": (end_utc - start_utc).total_seconds(),
        # Real video FILES (each channel counted separately, e.g. a 3-channel
        # Parking chunk counts as 3) whose window overlaps this stop, at the
        # moment the list was generated - see _count_overlapping_segments.
        # Deliberately counts every file regardless of pending-delete status,
        # so this number doesn't shift under you as you queue deletions from
        # the same list.
        "video_count": video_count,
    }
    # Only present when it differs from `start` - see _first_parking_start_within.
    if video_start_utc is not None and video_start_utc != start_utc:
        d["video_start"] = video_start_utc.isoformat()
        d["video_start_local"] = video_start_utc.astimezone(tz).isoformat()
    return d


def _count_overlapping_segments(start, end, segments) -> int:
    """Count of segments (any channel/source) whose [start_time, coverage_end()) overlaps [start, end)."""
    return sum(1 for s in segments if s.start_time < end and s.coverage_end() > start)


def _first_parking_start_within(start_utc, end_utc):
    """
    Earliest Parking/Timelapse segment start time within [start_utc, end_utc)
    across any channel, or None if no Parking segment starts in that window.

    Used to pick a better "jump to this video" instant for a GPS-detected stop
    than its literal start: the GPS fix that first crosses the stationary-speed
    threshold is commonly still covered by the tail of the last Normal
    (driving) segment - Normal loop-recording windows run a few minutes past
    the instant you actually stop - so clicking a long parked stop would
    otherwise open a few seconds of you still pulling in, not any parked
    footage. Confirmed against real data (2026-09-08): the stop's GPS-detected
    start was ~09:33:30, still inside the last Normal segment's 09:31:54-
    09:36:54 coverage, with the first Parking clip not starting until 09:37:42.
    Parking-derived stops (built directly from a parking_segments window, not
    this GPS path) don't need this - they already start exactly at their own
    segment's start_time.
    """
    candidates = [
        seg.start_time for seg in STATE["parking_segments"]
        if start_utc <= seg.start_time < end_utc
    ]
    return min(candidates) if candidates else None


def _overlaps_any(start, end, intervals) -> bool:
    return any(start < iv_end and end > iv_start for iv_start, iv_end in intervals)


def _merge_coverage_intervals(intervals):
    """
    Standard sweep-line interval merge: given [(start, end), ...] (unsorted,
    possibly overlapping), return a sorted, non-overlapping, non-adjacent list.
    Used by /api/coverage to collapse per-segment (start_time, coverage_end())
    windows - every channel, both Normal and Parking/Timelapse - into "is there
    ANY footage covering this instant". Permissive across channels: a single
    covered channel counts as covered - this answers "where are the gaps", not
    "which channels are present".
    """
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: iv[0])
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return merged


@app.route("/api/stops")
def api_stops():
    """
    Return "stops" - spans of no vehicle movement, regardless of source - for
    ?date=YYYY-MM-DD (interpreted in the display timezone; omitted returns
    every day's, matching /api/track's convention). Two sources:

      - GPS-detected stationary intervals within ordinary Normal-mode driving
        (DASHCAM_STOP_MIN_MINUTES / DASHCAM_STOP_MAX_SPEED_MPH thresholds). A
        GPS-detected stop also carries video_start/video_start_local when a
        Parking clip starts partway through it - see
        _first_parking_start_within - so the frontend can click into actual
        parked footage instead of the tail of the driving clip that happens
        to still cover the stop's literal (GPS-threshold-crossing) start.
      - Parking-mode Timelapse clips (STATE["parking_segments"]), whose
        location is backfilled from the last real GPS fix before the clip.

    A Parking clip whose window falls inside a GPS-detected stop is dropped
    rather than listed as its own entry: confirmed against real data
    (2026-09-08) that when the camera's GPS stays powered/logging throughout
    a long parked stretch (no per-clip GPS gap), the GPS-detected stop already
    spans the whole thing as one entry, and its video_start (above) plus the
    stop-scrubber's smart-seek can already reach any instant in that span -
    including every individual Parking clip - so listing each ~3-minute clip
    as its own additional stop is pure duplication (this was the actual bug
    report: dozens of "3 minute" entries repeating right under one long one
    covering the same hours). A Parking clip with NO covering GPS-detected
    stop - e.g. GPS genuinely lost the fix during that clip, so there's no
    stationary run to represent it - is still listed on its own; that's not
    duplication, it's the only entry for that span.

    Every stop is returned even if no video actually covers it (a real
    recording gap, or footage that hasn't synced from the camera yet - this
    app can't tell those apart) - clicking it just gets the same "no video
    found" result /api/videos already gives today; this is also how a gap
    gets *noticed* rather than stumbled onto by chance.
    """
    tz = STATE["display_tz"]
    date_raw = request.args.get("date")

    fixes = STATE["fixes"]
    if date_raw:
        fixes = [fx for fx in fixes if _local_date(fx.timestamp, tz) == date_raw]

    all_segments = STATE["video_segments"] + STATE["parking_segments"]

    detected = _detect_stationary_stops(fixes, _stop_min_duration_s(), _stop_max_speed_mph())
    gps_intervals = [(start_fx.timestamp, end_fx.timestamp) for start_fx, end_fx in detected]
    stops = [
        _stop_json(
            start_fx.timestamp, end_fx.timestamp, start_fx.lat, start_fx.lon, tz,
            video_start_utc=_first_parking_start_within(start_fx.timestamp, end_fx.timestamp),
            video_count=_count_overlapping_segments(start_fx.timestamp, end_fx.timestamp, all_segments),
        )
        for start_fx, end_fx in detected
    ]

    # A Timelapse "chunk" is usually 2-3 files (one per channel) sharing the
    # same (start_time, coverage_end) - one stop entry per window, not one per
    # channel's file.
    seen_windows = set()
    for seg in STATE["parking_segments"]:
        if date_raw and _local_date(seg.start_time, tz) != date_raw:
            continue
        window = (seg.start_time, seg.coverage_end())
        if window in seen_windows:
            continue
        seen_windows.add(window)
        if _overlaps_any(*window, gps_intervals):
            continue  # already represented by the GPS-detected stop covering this span
        loc_fix = _last_fix_at_or_before(seg.start_time)
        if loc_fix is None:
            continue  # no GPS history at all yet before this clip - skip rather than guess a location
        stops.append(_stop_json(
            seg.start_time, seg.coverage_end(), loc_fix.lat, loc_fix.lon, tz,
            video_count=_count_overlapping_segments(seg.start_time, seg.coverage_end(), all_segments),
        ))

    stops.sort(key=lambda s: s["start"])
    return jsonify(stops)


@app.route("/api/coverage")
def api_coverage():
    """
    Return merged video-coverage intervals for ?date=YYYY-MM-DD (interpreted in
    the display timezone, same convention as /api/track and /api/stops) - used
    by the map to dash/fade the speed-colored track wherever footage is
    missing, without the frontend ever creating a <video> element (which would
    trigger a real transcode) just to find out.

    Merges (start_time, coverage_end()) windows from STATE["video_segments"]
    (Normal) and STATE["parking_segments"] (Parking/Timelapse), any channel
    counts as covered. Pure metadata; never touches video_transcode. Unlike
    /api/track's/api/stops's date filter (a point test on each fix's own local
    date), segments are SPANS, so filtering is by day-window overlap
    (_day_bounds_utc), not the segment's start_time's local date alone. date
    must be well-formed if given (400 on a bad value), since this endpoint
    actually has to parse it to compute day bounds (unlike those two routes).

    Returns [{"start": ..., "end": ...}, ...] (UTC ISO), sorted, non-overlapping.
    """
    tz = STATE["display_tz"]
    date_raw = request.args.get("date")
    segments = STATE["video_segments"] + STATE["parking_segments"]

    if date_raw:
        try:
            day_start, day_end = _day_bounds_utc(date_raw, tz)
        except ValueError:
            return jsonify({"error": "invalid date format, expected YYYY-MM-DD"}), 400
        segments = [s for s in segments if s.start_time < day_end and s.coverage_end() > day_start]

    merged = _merge_coverage_intervals([(s.start_time, s.coverage_end()) for s in segments])
    return jsonify([{"start": start.isoformat(), "end": end.isoformat()} for start, end in merged])


@app.route("/api/segment-boundary")
def api_segment_boundary():
    """
    Given ?ts=<ISO> and ?direction=next|prev, return the nearest OTHER video
    segment start_time strictly after (next) or before (prev) ts, across
    every channel and both Normal and Parking/Timelapse. Powers the
    stop-scrubber's skip buttons - jump directly to the next/previous video
    chunk within a stop instead of dragging continuously, for walking through
    (and deleting) a sequence of clips one at a time.

    Same-chunk files across channels share an identical start_time (a
    Parking chunk's front/interior/rear filenames all encode the same
    timestamp, just a different trailing channel letter), so the single next
    distinct start_time naturally advances by a whole chunk, not a fraction
    of a second.

    Doesn't know or care about "stop" boundaries - stops are a frontend-
    visible concept (built from STATE["fixes"] + STATE["parking_segments"] by
    /api/stops) that this lookup doesn't need to re-derive; the frontend
    clamps the result against the currently-open stop's [start, end) itself.

    Returns {"ts": "<ISO>"}, or {"ts": null} if there's nothing in that direction.
    """
    ts_raw = request.args.get("ts")
    direction = request.args.get("direction")
    if direction not in ("next", "prev"):
        return jsonify({"error": "direction must be 'next' or 'prev'"}), 400
    try:
        ts = datetime.fromisoformat((ts_raw or "").replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return jsonify({"error": "invalid ts format, expected ISO 8601"}), 400

    starts = {s.start_time for s in STATE["video_segments"] + STATE["parking_segments"]}
    if direction == "next":
        candidates = [s for s in starts if s > ts]
        result = min(candidates) if candidates else None
    else:
        candidates = [s for s in starts if s < ts]
        result = max(candidates) if candidates else None
    return jsonify({"ts": result.isoformat() if result else None})


@app.route("/api/videos")
def api_videos():
    """
    Given ?ts=<ISO timestamp, UTC>, return the video segment(s) covering that
    moment, one per channel, with the seek offset (seconds into the file)
    needed to land exactly on that timestamp. Checks Normal segments first,
    then Parking/Timelapse for any channel Normal didn't cover - the frontend
    doesn't need to know or care which source a given moment's footage came
    from, it's the same click-to-load flow either way.
    """
    ts_raw = request.args.get("ts")
    if not ts_raw:
        return jsonify({"error": "missing ts parameter"}), 400
    try:
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return jsonify({"error": "invalid ts format, expected ISO 8601"}), 400

    matches = find_segments_for_timestamp(STATE["video_segments"], ts)
    if STATE["parking_segments"]:
        for channel, seg in find_segments_for_timestamp(STATE["parking_segments"], ts).items():
            matches.setdefault(channel, seg)  # Normal wins if both somehow cover the same instant

    pending = _read_pending_deletes()
    result = {}
    for channel, seg in matches.items():
        # compression_ratio is 1.0 for realtime (Normal) segments, so this is
        # the same formula as before for them; for Parking/Timelapse it
        # translates "N real seconds into the segment" into "N * ratio seconds
        # into the file" - see VideoSegment.compression_ratio.
        offset = (ts - seg.start_time).total_seconds() * seg.compression_ratio
        result[channel] = {
            "filename": seg.path.name,
            "url": f"/video/{seg.path.name}",
            "offset_seconds": offset,
            "start_time": seg.start_time.isoformat(),
            "end_time": seg.coverage_end().isoformat(),
            "pending_delete": _relative_video_path(seg) in pending,
        }
    return jsonify(result)


@app.route("/api/transcode-progress")
def api_transcode_progress():
    """
    Real-time transcode progress for one video file, for the frontend's
    progress bar. See video_transcode.progress_for() for what "fraction" and
    "eta_seconds" actually mean (measured vs. estimated).
    """
    filename = request.args.get("filename")
    seg = STATE["segment_by_filename"].get(filename) if filename else None
    if not seg:
        return jsonify({"error": "unknown filename"}), 404
    return jsonify(video_transcode.progress_for(seg.path))


def _relative_video_path(seg) -> str:
    """
    "Normal/<filename>" or "Parking/<filename>" - the path relative to the
    data directory. A bare filename alone doesn't tell the maintainer which
    directory to delete it from when processing pending_deletes.txt by hand,
    so this is what actually gets written there (see api_delete_request) and
    checked against (see api_videos's pending_delete flag). seg.path's
    immediate parent is always exactly the directory scan_video_dir() was
    called against - this reads that directory's own real name off the
    already-trusted, scan-discovered path, never anything client-supplied.
    """
    return f"{seg.path.parent.name}/{seg.path.name}"


def _read_pending_deletes() -> set[str]:
    """
    Current pending-deletes list (see api_delete_request) as a set of
    "Normal/<filename>"-style relative paths, for cheap membership checks.
    Empty set if the file doesn't exist yet. Shared by api_delete_request (to
    merge into) and api_videos (to flag a channel that's already queued, so
    the frontend can show that instead of letting it look identical to an
    unflagged one).
    """
    try:
        return {line for line in PENDING_DELETES_PATH.read_text().splitlines() if line}
    except FileNotFoundError:
        return set()


def _cancel_transcodes_for(filenames) -> list[str]:
    """
    Cancel any in-flight transcode for each of the given filenames (an
    unknown filename is skipped silently, not an error - callers that need
    strict validation, like api_delete_request, already did it themselves
    before calling this). Returns the subset that actually had something
    running to kill. Shared by api_delete_request (marked for deletion) and
    api_cancel_transcode (navigated away from) - both are "stop transcoding
    whatever's in this list," just triggered for different reasons.
    """
    cancelled = []
    for f in filenames:
        seg = STATE["segment_by_filename"].get(f)
        if seg and video_transcode.cancel_transcode(seg.path):
            cancelled.append(f)
    return cancelled


@app.route("/api/delete-request", methods=["POST"])
def api_delete_request():
    """
    Add filenames to the plain-text pending-deletes list at
    CONFIG_DIR/pending_deletes.txt (one filename per line, sorted) - never
    deletes anything itself. /data is mounted read-only into this container
    deliberately (see CLAUDE.md "Live data reload"); the maintainer deletes
    the actual files by hand from this list, on their own schedule - there is
    no processing script.

    Body: {"filenames": [...]} - bare filenames, matching what /api/videos
    returns. Every filename must already be a real, currently-scanned video -
    checked against STATE["segment_by_filename"], the same allowlist
    _resolve_video_path() uses for /video and /original - so this can never
    be used to write an arbitrary string into a file the maintainer might
    later feed into a shell loop. What's actually written to the list is the
    "Normal/<filename>" or "Parking/<filename>" relative path (see
    _relative_video_path) - a bare filename alone doesn't tell the maintainer
    which directory to delete it from.

    Merge behavior: the existing list is first filtered down to relative
    paths STATE still reports as present - so an entry already deleted by
    hand silently drops off the very next request, with no separate cleanup
    step needed (a filename the maintainer deleted seconds ago, before the
    next periodic/triggered rescan, is harmlessly carried forward one more
    round - same staleness STATE always has, nothing new) - then the newly
    requested files are added (de-duplicated via a set), then the whole list
    is rewritten atomically (tmp-file-then-rename, same pattern as
    auth._write_users) so a concurrent read never sees a half-written file.

    Also cancels any transcode currently in progress for the newly requested
    filenames (video_transcode.cancel_transcode) - scrolling through a stop's
    clips can queue up a lot of transcoding, and there's no point finishing
    one for a file that was just marked for deletion. Harmless no-op per file
    if nothing was actually transcoding. This does NOT block a future
    transcode of the same file - if it's viewed again before actually being
    deleted from disk, it transcodes fresh like any other file.
    """
    body = request.get_json(silent=True) or {}
    filenames = body.get("filenames")
    if not isinstance(filenames, list) or not filenames:
        return jsonify({"error": "filenames must be a non-empty list"}), 400
    unknown = [f for f in filenames if f not in STATE["segment_by_filename"]]
    if unknown:
        return jsonify({"error": f"unknown filename(s): {unknown}"}), 400

    requested_paths = {_relative_video_path(STATE["segment_by_filename"][f]) for f in filenames}
    valid_paths = {_relative_video_path(seg) for seg in STATE["segment_by_filename"].values()}
    existing = _read_pending_deletes()
    still_there = existing & valid_paths
    merged = sorted(still_there | requested_paths)

    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PENDING_DELETES_PATH.with_suffix(".txt.tmp")
        tmp.write_text("\n".join(merged) + ("\n" if merged else ""))
        tmp.replace(PENDING_DELETES_PATH)
    except OSError as e:
        return jsonify({"error": f"could not save pending deletes: {e}"}), 500

    _cancel_transcodes_for(filenames)

    return jsonify({"pending_count": len(merged), "filenames": merged}), 200


@app.route("/api/cancel-transcode", methods=["POST"])
def api_cancel_transcode():
    """
    Cancel any in-flight transcode for the given filenames - used when the
    frontend navigates away from a point/segment before its videos finished
    loading (see templates/index.html's onPointClick). Without this, clicking
    through several clips quickly (e.g. the stop-scrubber's skip buttons)
    leaves a growing backlog of superseded jobs hogging TRANSCODE_CONCURRENCY's
    scarce slots, so the video you're actually looking at now ends up queued
    behind several you've already navigated away from - confirmed live
    2026-09-15, this is what "UI lockup while scrolling through a stop"
    actually was (not thread starvation - DASHCAM_THREADS=64 already fixed
    that; this is wasted work piling up ahead of the request that matters).

    Unlike api_delete_request, this never touches pending_deletes.txt and is
    lenient about unknown filenames (skips rather than rejecting the whole
    request) - it's a best-effort housekeeping call, not a confirmed user
    action, and most calls will cancel nothing since most navigations happen
    after a video already finished loading.

    Body: {"filenames": [...]}. Returns {"cancelled": [...]} - the subset
    that actually had something running to kill.
    """
    body = request.get_json(silent=True) or {}
    filenames = body.get("filenames")
    if not isinstance(filenames, list):
        return jsonify({"error": "filenames must be a list"}), 400
    return jsonify({"cancelled": _cancel_transcodes_for(filenames)})


def _resolve_video_path(filename):
    """
    Resolve filename to a path this process itself discovered by directory
    scanning (STATE["segment_by_filename"], covering both Normal and
    Parking/Timelapse) - never by reconstructing a path from the
    attacker-controlled `filename` and merely checking it stays inside some
    base directory. An unscanned filename is always a 404 regardless of what
    path-traversal tricks it contains, since scan_video_dir keys this dict by
    Path.name, which by definition can never itself contain "/" or "..". Also
    means a video from either directory resolves through the same lookup, no
    "which base dir is this filename under" logic needed. Shared by /video
    and /original so this only has to be gotten right in one place. Don't
    revert to reconstructing a path from `filename` when touching this route.
    """
    seg = STATE["segment_by_filename"].get(filename)
    if not seg or not seg.path.is_file():
        abort(404)
    return seg.path


@app.route("/video/<path:filename>")
def serve_video(filename):
    """
    Serve a video file with Range support (needed for seeking/scrubbing).

    The camera records HEVC, which browsers won't decode in a <video>
    element (plays audio/duration, shows a black frame) - so this transcodes
    to H.264 on first request and serves the cached result on every request
    after that. The first request for a given file blocks until the
    transcode finishes; app.run(threaded=True) (dev) / gunicorn's threads
    (deployed) keep that from stalling any other concurrent request (other
    videos, API calls, etc) - see gunicorn.conf.py's DASHCAM_THREADS comment.

    A 503 (not the generic 500 an unhandled exception would give) means
    video_transcode.TRANSCODE_CONCURRENCY's slots were all busy and stayed
    that way past TRANSCODE_QUEUE_TIMEOUT_S - a real "try again shortly", not
    a broken file; Retry-After suggests when. A 409 means this exact request
    was cancelled - either directly (marked for deletion mid-transcode) or
    because it was still queued behind another attempt for the same file that
    got cancelled out from under it (see video_transcode.cancel_transcode) -
    expected whenever the frontend navigates away before a video finishes
    loading, not a real failure.
    """
    src = _resolve_video_path(filename)
    try:
        transcoded = video_transcode.ensure_transcoded(src)  # on-demand: always returns a path (or raises)
    except video_transcode.TranscodeBusy as e:
        resp = jsonify({"error": str(e)})
        resp.status_code = 503
        resp.headers["Retry-After"] = str(int(video_transcode.TRANSCODE_QUEUE_TIMEOUT_S))
        return resp
    except video_transcode.TranscodeCancelled as e:
        return jsonify({"error": str(e)}), 409

    seg = STATE["segment_by_filename"].get(filename)
    if seg:
        _prefetch_neighbors(seg)

    return send_from_directory(transcoded.parent, transcoded.name, conditional=True)


@app.route("/original/<path:filename>")
def serve_original(filename):
    """
    Direct download of the original, untranscoded camera file - full
    resolution HEVC, not the downscaled H.264 preview /video serves for
    in-browser playback. as_attachment=True so the browser saves it rather
    than trying (and failing) to play HEVC inline.
    """
    src = _resolve_video_path(filename)
    return send_from_directory(src.parent, src.name, conditional=True, as_attachment=True)


# Segments we've already kicked off neighbor-prefetch for this run, so a burst
# of range requests while a <video> loads/scrubs doesn't spawn a new pair of
# ffprobe/ffmpeg threads each time (that CPU churn is enough to stutter the
# clip that's actually playing). Never reset - re-warming after a cache prune
# just happens on demand instead, which is fine.
_prefetched_from: set[str] = set()


def _prefetch_neighbors(seg):
    """
    The first time a segment is served, kick off background transcoding of that
    same channel's adjacent segments - so scrubbing forward/backward along the
    track, or auto-advancing at end-of-segment, doesn't re-hit the ~1 minute
    first-view wait. Fires in daemon threads and doesn't block the response.

    Prefetch passes rebuild_corrupt=False: a neighbour that was never
    transcoded gets warmed, but one whose cached file is corrupt is only
    evicted, not rebuilt - that waits until the file is actually requested for
    playback.

    Only looks at STATE["segments_by_channel"] (Normal). A Parking/Timelapse
    `seg` isn't a member of that list, so `siblings.index(seg)` below raises
    ValueError and this is a silent no-op for it - the clip itself still
    serves fine, it just doesn't get neighbor-prefetch warming.

    Skips a neighbor that's already in pending_deletes.txt - marking a file
    for deletion doesn't remove it from disk (the maintainer does that by
    hand later), so it's still a perfectly normal-looking prefetch target
    with no idea anyone's asked for it to go away. Confirmed live 2026-09-15
    on a 4-core host: a deleted-and-cancelled file's prefetch (queued earlier,
    from before it was deleted) went on to transcode anyway minutes later,
    consuming a scarce TRANSCODE_CONCURRENCY slot - and 4 concurrent
    "ultrafast" software encodes are enough to saturate 4 cores entirely,
    starving the Python process itself, not just the transcode queue. This is
    speculative work only, so skipping it is a pure win with no downside - if
    the maintainer deliberately reopens the file later, the on-demand /video
    path still transcodes it fresh, same as any other file (see
    api_delete_request's docstring).
    """
    if seg.path.name in _prefetched_from:
        return
    _prefetched_from.add(seg.path.name)

    siblings = STATE["segments_by_channel"].get(seg.channel, [])
    try:
        idx = siblings.index(seg)
    except ValueError:
        return
    pending = _read_pending_deletes()
    neighbors = [s for s in (
        siblings[idx - 1] if idx > 0 else None,
        siblings[idx + 1] if idx + 1 < len(siblings) else None,
    ) if s is not None and _relative_video_path(s) not in pending]
    for neighbor in neighbors:
        threading.Thread(target=_background_transcode, args=(neighbor.path,), daemon=True).start()


def _background_transcode(path):
    try:
        video_transcode.ensure_transcoded(path, rebuild_corrupt=False)
    except Exception as e:
        print(f"WARNING: background prefetch transcode failed for {path}: {e}")


def resolve_data_dir(cli_value: str | None) -> str:
    """
    An explicit data dir (--data-dir or $DASHCAM_DATA_DIR) always wins and gets
    cached for next time. Otherwise fall back to the cached path from a previous
    run.
    """
    if cli_value:
        resolved = str(Path(cli_value).resolve())
        try:
            config.save_data_dir(resolved)
        except OSError as e:
            # Read-only rootfs / unwritable XDG_CONFIG_HOME (common in a
            # container, where the path is passed in every run anyway) - not
            # fatal, we already have the value.
            print(f"WARNING: could not cache data dir ({e}); pass it again next run.")
        return resolved

    cached = config.load_data_dir()
    if cached:
        return cached

    sys.exit(
        "No data directory configured yet. Pass --data-dir /path/to/data "
        "(or set $DASHCAM_DATA_DIR). It will be remembered for future runs."
    )


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _build_state(data_dir: Path, record_tz: ZoneInfo, display_tz: ZoneInfo) -> dict:
    """
    Parse the GPS logs and scan the video dirs under `data_dir` and return a
    fresh STATE dict. Pure - touches no globals - so reload_state() can build a
    new snapshot without disturbing the one requests are reading. Raises
    FileNotFoundError if GPS/ or Normal/ is missing.

    Parking/ (Timelapse, mode "T") is scanned too, but optionally - unlike
    GPS/Normal it's fine for a data dir to have none yet (no hardwired parking
    power, or an older setup). Kept in a separate segments list from Normal's,
    per video_matcher's warning that Timelapse's per-session-variable cadence
    would poison Normal's loop-length median if scanned together.
    """
    gps_dir = data_dir / "GPS"
    video_dir = data_dir / "Normal"
    for required in (gps_dir, video_dir):
        if not required.is_dir():
            raise FileNotFoundError(f"Expected directory not found: {required}")

    fixes = load_gps_dir(gps_dir, record_tz=record_tz)
    segments = scan_video_dir(video_dir, record_tz=record_tz)

    parking_dir = data_dir / "Parking"
    parking_segments = []
    if parking_dir.is_dir():
        parking_segments = scan_video_dir(
            parking_dir, record_tz=record_tz, mode="T",
            measure_duration=True, extend_to_next=True,
        )

    by_channel = defaultdict(list)
    for seg in segments:  # already time-sorted, so each per-channel list stays time-sorted too
        by_channel[seg.channel].append(seg)

    # Filenames are unique across every recording mode/directory, so one
    # combined lookup serves both /video and /original regardless of source.
    segment_by_filename = {seg.path.name: seg for seg in segments}
    segment_by_filename.update((seg.path.name, seg) for seg in parking_segments)

    return {
        "fixes": fixes,
        "video_segments": segments,
        "video_dir": str(video_dir),
        "parking_segments": parking_segments,
        "display_tz": display_tz,
        "segments_by_channel": dict(by_channel),
        "segment_by_filename": segment_by_filename,
        "loaded_at": datetime.now(timezone.utc),
        "_data_dir": data_dir,
        "_record_tz": record_tz,
    }


def _install_state(data_dir: Path, record_tz: ZoneInfo, display_tz: ZoneInfo) -> None:
    """Initial load at startup. A missing data dir is fatal here (fail loudly)."""
    global STATE
    try:
        STATE = _build_state(data_dir, record_tz, display_tz)
    except FileNotFoundError as e:
        sys.exit(str(e))
    parking_note = f", {len(STATE['parking_segments'])} parking clips" if STATE["parking_segments"] else ""
    print(f"Loaded {len(STATE['fixes'])} GPS fixes, {len(STATE['video_segments'])} "
          f"video segments{parking_note} from {data_dir}")
    print(f"Transcoder: {video_transcode.encoder_summary()}")
    if STATE["fixes"] and not STATE["video_segments"]:
        print("WARNING: no video segments matched - check video_matcher.py's "
              "FILENAME_PATTERN against your actual filenames.")


def reload_state() -> dict:
    """
    Re-scan the data dir and swap in a fresh STATE snapshot. Serialized by
    _reload_lock. On any failure the current STATE is left untouched and the
    exception propagates - the caller decides how to report it. Returns a small
    summary (counts + deltas vs. the previous snapshot).
    """
    global STATE
    with _reload_lock:
        prev = STATE
        new = _build_state(prev["_data_dir"], prev["_record_tz"], prev["display_tz"])
        STATE = new  # single assignment - readers never see a half-updated snapshot
        # New footage often lands at the "live edge"; clear the prefetch guard so
        # the next /video request re-warms neighbours around it.
        _prefetched_from.clear()
        summary = {
            "fixes": len(new["fixes"]),
            "segments": len(new["video_segments"]),
            "fixes_added": len(new["fixes"]) - len(prev["fixes"]),
            "segments_added": len(new["video_segments"]) - len(prev["video_segments"]),
            "loaded_at": new["loaded_at"].isoformat(),
        }
    if summary["fixes_added"] or summary["segments_added"]:
        print(f"Reload: {summary['fixes']} fixes (+{summary['fixes_added']}), "
              f"{summary['segments']} segments (+{summary['segments_added']})")
    return summary


# "init" = never checked; "absent" = checked, file wasn't there; int = last mtime_ns.
_trigger_state: object = "init"


def _trigger_fired(trigger_path: str) -> bool:
    """
    True when the trigger file has just appeared or its mtime has changed since
    the last check. The file is only ever read (the data mount is read-only), so
    the sender just re-uploads / touches it after each batch. A file that
    already exists at the first check does NOT fire - startup did a full load.
    """
    global _trigger_state
    prev = _trigger_state
    try:
        m: int | None = Path(trigger_path).stat().st_mtime_ns
    except OSError:
        m = None
    _trigger_state = "absent" if m is None else m

    if m is None or prev == "init":
        return False            # gone, or pre-existing at startup
    if prev == "absent":
        return True             # the file just appeared
    return m != prev            # touched again


def _rescan_loop(interval_s: float, trigger_path: str | None) -> None:
    poll = 5.0 if trigger_path else max(interval_s, 30.0)
    since_reload = 0.0
    while True:
        time.sleep(poll)
        since_reload += poll
        fire = (trigger_path and _trigger_fired(trigger_path)) or \
               (interval_s and since_reload >= interval_s)
        if not fire:
            continue
        since_reload = 0.0
        try:
            reload_state()
        except Exception as e:  # keep the loop (and the current STATE) alive
            print(f"WARNING: re-scan failed, keeping current data: {e}")


def start_rescan_thread() -> None:
    """
    Start the background re-scan loop. Runs if DASHCAM_RESCAN_INTERVAL > 0 (a
    timed re-scan, 30 s floor) and/or DASHCAM_RELOAD_TRIGGER is set (a file whose
    mtime the loop watches - the footage-sync side touches it after a batch, and
    it needs no open port). Idempotent. Must run in the request-serving process
    (the gunicorn worker via post_fork, not the preload master whose STATE the
    worker only copies).
    """
    global _rescan_thread_started
    if _rescan_thread_started:
        return
    try:
        interval = max(float(os.environ.get("DASHCAM_RESCAN_INTERVAL", "0")), 0.0)
    except ValueError:
        interval = 0.0
    if interval:
        interval = max(interval, 30.0)  # floor - don't hammer the disk
    trigger = os.environ.get("DASHCAM_RELOAD_TRIGGER") or None
    if not interval and not trigger:
        return
    _rescan_thread_started = True
    threading.Thread(target=_rescan_loop, args=(interval, trigger), daemon=True).start()
    bits = []
    if interval:
        bits.append(f"every {interval:g}s")
    if trigger:
        bits.append(f"on {trigger} change")
    print(f"Background re-scan: {', '.join(bits)}")


def create_app():
    """
    WSGI entry point for a production server. Config comes entirely from the
    DASHCAM_* environment variables (there are no request-time args to parse):

        gunicorn -c gunicorn.conf.py 'app:create_app()'

    Returns the module-level Flask `app` with STATE populated and auth wired up.
    """
    data_dir = Path(resolve_data_dir(os.environ.get("DASHCAM_DATA_DIR")))
    record_tz = ZoneInfo(os.environ.get("DASHCAM_RECORD_TZ", "America/Chicago"))
    display_raw = os.environ.get("DASHCAM_DISPLAY_TZ")
    display_tz = ZoneInfo(display_raw) if display_raw else record_tz
    _install_state(data_dir, record_tz, display_tz)
    # NB: the re-scan thread is started from gunicorn.conf.py's post_fork (in the
    # worker), not here - this runs in the preload master, whose STATE the worker
    # only inherits a copy of.

    # Secure cookies by default here - a deployment is behind TLS (nginx). Set
    # DASHCAM_SECURE_COOKIE=0 only if you're knowingly serving plain HTTP.
    auth.init_app(app, secure_cookie=_env_bool("DASHCAM_SECURE_COOKIE", True))
    if _env_bool("DASHCAM_REQUIRE_AUTH", True) and not auth.enabled():
        sys.exit(
            "DASHCAM_REQUIRE_AUTH is on but no users are configured. Create one:\n"
            "  docker compose run --rm dashcam-viewer python app.py adduser <name>\n"
            "(or set DASHCAM_REQUIRE_AUTH=0 to run without a login)."
        )
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("DASHCAM_DATA_DIR"),
        help="Directory containing 'Normal' (videos) and 'GPS' (*.dat logs) subdirectories. "
             "Remembered for future runs once passed; omit to reuse the last one. "
             "Env: DASHCAM_DATA_DIR.",
    )
    parser.add_argument(
        "--record-timezone",
        default=os.environ.get("DASHCAM_RECORD_TZ", "America/Chicago"),
        help="IANA timezone the camera's clock was set to when recording (default: America/Chicago, i.e. CDT/CST). "
             "All timestamps are converted to UTC internally using this zone. Env: DASHCAM_RECORD_TZ.",
    )
    parser.add_argument(
        "--display-timezone",
        default=os.environ.get("DASHCAM_DISPLAY_TZ") or None,
        help="IANA timezone to convert UTC back to for display in the UI. Defaults to --record-timezone. "
             "Env: DASHCAM_DISPLAY_TZ.",
    )
    parser.add_argument("--host", default=os.environ.get("DASHCAM_HOST", "127.0.0.1"),
                        help="Interface to bind (default 127.0.0.1). Env: DASHCAM_HOST.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("DASHCAM_PORT", "5000")),
                        help="Port to listen on (default 5000). Env: DASHCAM_PORT.")
    parser.add_argument("--debug", action="store_true",
                        default=os.environ.get("DASHCAM_DEBUG", "").lower() in ("1", "true", "yes"),
                        help="Flask debug mode (reloader + debugger). Env: DASHCAM_DEBUG.")

    sub = parser.add_subparsers(dest="command")
    p_add = sub.add_parser("adduser", help="Create or update a login (prompts for a password).")
    p_add.add_argument("username")
    p_del = sub.add_parser("deluser", help="Remove a login.")
    p_del.add_argument("username")
    sub.add_parser("listusers", help="List configured logins.")

    args = parser.parse_args()

    # User-management subcommands: touch only auth's files, then exit.
    if args.command == "adduser":
        auth.add_user_interactive(args.username)
        return
    if args.command == "deluser":
        auth.delete_user(args.username)
        return
    if args.command == "listusers":
        auth.list_users()
        return

    data_dir = Path(resolve_data_dir(args.data_dir))
    record_tz = ZoneInfo(args.record_timezone)
    display_tz = ZoneInfo(args.display_timezone) if args.display_timezone else record_tz
    _install_state(data_dir, record_tz, display_tz)
    start_rescan_thread()  # no fork here, so start it directly

    # Dev / single-user path. Auth still applies if users exist, but insecure
    # cookies by default so login works over plain-HTTP localhost. threaded=True
    # so a first-view transcode (~1 min) doesn't stall other requests. For a
    # deployment, run create_app() under gunicorn - see gunicorn.conf.py.
    auth.init_app(app, secure_cookie=_env_bool("DASHCAM_SECURE_COOKIE", False))
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
