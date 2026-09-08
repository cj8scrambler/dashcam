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
import os
import sys
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

import auth
import config
import video_transcode
from gps_parser import load_gps_dir
from video_matcher import scan_video_dir, find_segments_for_timestamp

app = Flask(__name__)

# Populated at startup by main()
STATE = {
    "fixes": [],
    "video_segments": [],
    "video_dir": None,
    "display_tz": None,
    "segments_by_channel": {},   # channel -> that channel's segments, time-sorted
    "segment_by_filename": {},   # e.g. "20260904_182500_00002_N_A.MP4" -> VideoSegment
}


def _local_date(dt_utc, tz) -> str:
    return dt_utc.astimezone(tz).date().isoformat()


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
                 "segments": len(STATE["video_segments"])}),
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


@app.route("/api/videos")
def api_videos():
    """
    Given ?ts=<ISO timestamp, UTC>, return the video segment(s) covering that
    moment, one per channel, with the seek offset (seconds into the file)
    needed to land exactly on that timestamp.
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
    result = {}
    for channel, seg in matches.items():
        offset = (ts - seg.start_time).total_seconds()
        result[channel] = {
            "filename": seg.path.name,
            "url": f"/video/{seg.path.name}",
            "offset_seconds": offset,
            "start_time": seg.start_time.isoformat(),
            "end_time": seg.coverage_end().isoformat(),
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


def _resolve_video_path(filename):
    """
    Resolve filename against STATE["video_dir"] and reject anything that
    escapes it. Shared by both /video and /original so the traversal check
    only has to be gotten right in one place.
    """
    video_dir = Path(STATE["video_dir"]).resolve()
    src = (video_dir / filename).resolve()
    if not src.is_relative_to(video_dir) or not src.is_file():
        abort(404)
    return src


@app.route("/video/<path:filename>")
def serve_video(filename):
    """
    Serve a video file with Range support (needed for seeking/scrubbing).

    The camera records HEVC, which browsers won't decode in a <video>
    element (plays audio/duration, shows a black frame) - so this transcodes
    to H.264 on first request and serves the cached result on every request
    after that. The first request for a given file blocks until the
    transcode finishes; app.run(threaded=True) keeps that from stalling any
    other concurrent request (other videos, API calls, etc).
    """
    src = _resolve_video_path(filename)
    transcoded = video_transcode.ensure_transcoded(src)  # on-demand: always returns a path (or raises)

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
    """
    if seg.path.name in _prefetched_from:
        return
    _prefetched_from.add(seg.path.name)

    siblings = STATE["segments_by_channel"].get(seg.channel, [])
    try:
        idx = siblings.index(seg)
    except ValueError:
        return
    neighbors = [s for s in (
        siblings[idx - 1] if idx > 0 else None,
        siblings[idx + 1] if idx + 1 < len(siblings) else None,
    ) if s is not None]
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


def _load_state(data_dir: Path, record_tz: ZoneInfo, display_tz: ZoneInfo) -> None:
    """
    Parse the GPS logs and scan the video dir under `data_dir`, populating the
    module-level STATE dict. Called once at startup - by main() for the CLI /
    dev server, and by create_app() for a WSGI server. There is no reload
    endpoint; changing the data dir's contents means restarting the process.
    """
    gps_dir = data_dir / "GPS"
    video_dir = data_dir / "Normal"
    for required in (gps_dir, video_dir):
        if not required.is_dir():
            sys.exit(f"Expected directory not found: {required}")

    STATE["fixes"] = load_gps_dir(gps_dir, record_tz=record_tz)
    STATE["video_dir"] = str(video_dir)
    STATE["video_segments"] = scan_video_dir(video_dir, record_tz=record_tz)
    STATE["display_tz"] = display_tz

    by_channel = defaultdict(list)
    for seg in STATE["video_segments"]:  # already time-sorted, so each per-channel list stays time-sorted too
        by_channel[seg.channel].append(seg)
    STATE["segments_by_channel"] = dict(by_channel)
    STATE["segment_by_filename"] = {seg.path.name: seg for seg in STATE["video_segments"]}

    print(f"Loaded {len(STATE['fixes'])} GPS fixes from {gps_dir}")
    print(f"Found {len(STATE['video_segments'])} video segments in {video_dir}")
    print(f"Transcoder: {video_transcode.encoder_summary()}")
    if STATE["fixes"] and not STATE["video_segments"]:
        print("WARNING: no video segments matched - check video_matcher.py's "
              "FILENAME_PATTERN against your actual filenames.")


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
    _load_state(data_dir, record_tz, display_tz)

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
    _load_state(data_dir, record_tz, display_tz)

    # Dev / single-user path. Auth still applies if users exist, but insecure
    # cookies by default so login works over plain-HTTP localhost. threaded=True
    # so a first-view transcode (~1 min) doesn't stall other requests. For a
    # deployment, run create_app() under gunicorn - see gunicorn.conf.py.
    auth.init_app(app, secure_cookie=_env_bool("DASHCAM_SECURE_COOKIE", False))
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
