"""
Transparent H.264 transcode cache for dashcam footage.

The Vantrue N4S records HEVC (H.265) video. Chrome (and most browsers) can't
decode HEVC in a <video> element, so every clip is transcoded to H.264 and
downscaled on first view.

Encoder is chosen by $DASHCAM_HWACCEL:
  - "none" (default): software libx264, "ultrafast" preset. Works anywhere.
  - "nvenc": NVIDIA GPU - CUDA decode -> scale_cuda -> h264_nvenc, entirely on
    the GPU. Needs an NVIDIA GPU exposed to the container (nvidia-container-
    toolkit). Probed once at startup; if it can't initialise, or a real encode
    later fails, the code logs and falls back to software per file.

Each source file is transcoded once and cached under the user's XDG cache
dir (outside the repo), keyed by its resolved path. Cached files older than
MAX_CACHE_AGE_DAYS are deleted automatically (see _prune_stale_cache) so the
cache doesn't grow forever - this is enforced by the app itself rather than
relying on /tmp or any OS/distro-specific cleanup policy.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
import time
from contextlib import nullcontext
from pathlib import Path

# "none" -> software libx264; "nvenc" -> NVIDIA GPU. See module docstring.
HWACCEL = os.environ.get("DASHCAM_HWACCEL", "none").strip().lower()

# Software libx264 knobs - also the fallback whenever nvenc isn't usable.
FFMPEG_PRESET = "ultrafast"
FFMPEG_CRF = "26"

# NVENC knobs. Presets p1 (fastest) .. p7 (slowest); p5 is comfortably above
# libx264 "ultrafast" quality and still several times faster than realtime.
# -cq is NVENC's CRF-equivalent constant-quality control.
NVENC_PRESET = "p5"
NVENC_CQ = "26"

SCALE_WIDTH = "1280"  # output width in pixels; height auto-scales to preserve aspect ratio

# GeForce cards cap simultaneous NVENC sessions (historically 3, more on recent
# drivers). Gate ffmpeg launches so a burst of cold-segment requests queues
# instead of erroring out. Only applied on the nvenc path.
_NVENC_MAX_CONCURRENT = max(1, int(os.environ.get("DASHCAM_HWACCEL_CONCURRENCY", "3")))
_nvenc_sem = threading.BoundedSemaphore(_NVENC_MAX_CONCURRENT)

_nvenc_state_lock = threading.Lock()
_nvenc_usable_cached: bool | None = None

# depend on the filesystem's atime tracking being enabled.
MAX_CACHE_AGE_DAYS = 14
_PRUNE_CHECK_INTERVAL_S = 3600  # how often to even check; pruning itself is cheap but no need to stat every file on every request

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "dashcam-viewer" / "transcoded"

_prune_guard = threading.Lock()
_last_pruned_at = 0.0


def _prune_stale_cache():
    global _last_pruned_at
    now = time.time()
    if now - _last_pruned_at < _PRUNE_CHECK_INTERVAL_S:
        return
    with _prune_guard:
        if time.time() - _last_pruned_at < _PRUNE_CHECK_INTERVAL_S:  # re-check: lost the race to another thread
            return
        _last_pruned_at = time.time()

    if not CACHE_DIR.is_dir():
        return
    cutoff = now - MAX_CACHE_AGE_DAYS * 86400
    for cached in CACHE_DIR.glob("*.mp4"):
        try:
            age_days = (now - cached.stat().st_mtime) / 86400
            if age_days > MAX_CACHE_AGE_DAYS:
                cached.unlink()
                _validated.discard(cached)
                # Plain print rather than Flask's app.logger: this can run from a
                # background prefetch thread with no Flask app context pushed,
                # where current_app.logger would raise RuntimeError. print()
                # lands in the same console as Flask's own request logs either way.
                print(f"Pruned stale transcoded cache file ({age_days:.1f}d old, "
                      f"limit {MAX_CACHE_AGE_DAYS}d): {cached.name}")
        except OSError:
            pass  # another thread/process may have already removed it - fine, not our problem

# A <video> element commonly fires more than one concurrent request for the
# same URL (an initial metadata probe, then range requests) - under
# app.run(threaded=True) those can arrive before the first transcode
# finishes. One lock per destination file serializes them so only one ffmpeg
# process ever runs per file; different files (e.g. front vs interior) still
# transcode fully in parallel since they use different locks.
_locks_guard = threading.Lock()
_locks: dict[Path, threading.Lock] = {}


def _lock_for(dest: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(dest, threading.Lock())


def _cache_path(src: Path) -> Path:
    key = hashlib.sha1(str(src.resolve()).encode()).hexdigest()
    return CACHE_DIR / f"{key}.mp4"


# In-flight transcode progress, keyed by destination cache path. Each update
# replaces the whole dict value in one assignment, so readers (progress_for,
# called from a different thread) never need a lock to get a consistent
# snapshot - relies on CPython dict/attribute assignment being atomic.
_progress: dict[Path, dict] = {}

# Cache paths whose contents have been sanity-checked (see _looks_complete)
# this process run, so the check's two ffprobe calls only happen once per file
# rather than on every /video request (a <video> element issues several).
_validated: set[Path] = set()

# A transcode more than this many seconds shorter than its source is treated
# as truncated/corrupt - not written to the cache, and evicted if found there.
# ffmpeg can exit 0 yet leave a short file when a read from flaky source media
# fails partway (seen on real USB footage: a 300s clip transcoded to 110s).
TRUNCATION_TOLERANCE_S = 3.0

# The last decodable video packet must land within this many seconds of the
# file's reported duration. Catches a file whose container header still claims
# the full length but whose payload was cut off (partial copy, disk
# corruption) - which the duration check alone misses, since the moov atom is
# written up front with -movflags +faststart.
TAIL_TOLERANCE_S = 5.0


# --------------------------------------------------------------------------- #
# Encoder selection (software libx264 vs NVIDIA nvenc)
# --------------------------------------------------------------------------- #

def _nvenc_usable() -> bool:
    """
    True if h264_nvenc can actually initialise here (GPU visible, driver OK, a
    session available). Probed once with a trivial encode and cached for the
    process; a hard failure during a real transcode flips it off (_disable_nvenc).
    """
    global _nvenc_usable_cached
    with _nvenc_state_lock:
        if _nvenc_usable_cached is not None:
            return _nvenc_usable_cached
        try:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
                 "-c:v", "h264_nvenc", "-f", "null", "-"],
                capture_output=True, text=True, timeout=30,
            )
            ok = r.returncode == 0
            if not ok:
                print(f"NVENC requested but not usable here - using software encoder. "
                      f"ffmpeg: {(r.stderr or '').strip()[-300:]}")
        except (OSError, subprocess.SubprocessError) as e:
            ok = False
            print(f"NVENC probe failed ({e}) - using software encoder.")
        _nvenc_usable_cached = ok
        return ok


def _disable_nvenc(reason: str) -> None:
    global _nvenc_usable_cached
    with _nvenc_state_lock:
        if _nvenc_usable_cached is not False:
            print(f"Disabling NVENC for the rest of this run ({reason}); using software.")
        _nvenc_usable_cached = False


def encoder_summary() -> str:
    """One-line description of the active encoder, for a startup log line."""
    if HWACCEL == "nvenc":
        if _nvenc_usable():
            return (f"NVIDIA nvenc (h264_nvenc {NVENC_PRESET}/cq{NVENC_CQ}, "
                    f"≤{_NVENC_MAX_CONCURRENT} concurrent), software libx264 fallback")
        return "software libx264 (DASHCAM_HWACCEL=nvenc set, but no usable GPU here)"
    return f"software libx264 ({FFMPEG_PRESET}, crf {FFMPEG_CRF})"


def _ffmpeg_cmd(src: Path, tmp_dest: Path, *, gpu: bool) -> list[str]:
    tail = [
        "-c:a", "aac",
        "-movflags", "+faststart",
        "-f", "mp4",  # tmp_dest ends ".mp4.partial" - ffmpeg can't infer the muxer
        "-progress", "pipe:1", "-nostats",
        str(tmp_dest),
    ]
    if gpu:
        # Decode on the GPU (-hwaccel cuda), keep frames in GPU memory
        # (-hwaccel_output_format cuda), resize and encode there too - no
        # CPU<->GPU frame copies anywhere in the pipeline.
        return [
            "ffmpeg", "-y",
            "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
            "-i", str(src),
            "-vf", f"scale_cuda={SCALE_WIDTH}:-2",
            "-c:v", "h264_nvenc", "-preset", NVENC_PRESET, "-cq", NVENC_CQ, "-b:v", "0",
            *tail,
        ]
    return [
        "ffmpeg", "-y",
        "-i", str(src),
        "-vf", f"scale={SCALE_WIDTH}:-2",
        "-c:v", "libx264", "-preset", FFMPEG_PRESET, "-crf", FFMPEG_CRF,
        *tail,
    ]


def _run_transcode(src: Path, tmp_dest: Path, dest: Path, total_s: float, *, gpu: bool) -> None:
    """
    One ffmpeg pass into tmp_dest. Streams progress into _progress[dest] as it
    runs; raises RuntimeError if ffmpeg fails or the output is truncated/corrupt.
    The caller renames tmp_dest -> dest on success. Logs one line per successful
    transcode naming the encoder actually used - the startup "Transcoder:" line
    only says what's configured/probed, not what ran for a given file, and a
    silent per-file GPU->software fallback would otherwise be invisible unless
    it happened to fail loudly.
    """
    started_at = time.monotonic()
    _progress[dest] = {"total_s": total_s, "current_s": 0.0, "started_at": started_at}
    label = "nvenc" if gpu else "libx264"
    limiter = _nvenc_sem if gpu else nullcontext()
    try:
        with limiter:
            # stderr to a temp file, not a pipe: read back only on failure.
            # Two live pipes (progress on stdout, logs on stderr) without
            # threads/select risks a classic subprocess deadlock.
            with tempfile.TemporaryFile(mode="w+") as stderr_log:
                proc = subprocess.Popen(
                    _ffmpeg_cmd(src, tmp_dest, gpu=gpu),
                    stdout=subprocess.PIPE, stderr=stderr_log, text=True, bufsize=1,
                )
                for line in proc.stdout:
                    key, _, value = line.strip().partition("=")
                    if key == "out_time_us":
                        try:
                            _progress[dest] = {**_progress[dest], "current_s": int(value) / 1_000_000}
                        except ValueError:
                            pass
                proc.wait()
                if proc.returncode != 0:
                    stderr_log.seek(0)
                    raise RuntimeError(f"ffmpeg ({label}) failed for {src}:\n{stderr_log.read()[-2000:]}")

        # ffmpeg can exit 0 yet leave a truncated file if a source read failed
        # partway (flaky media) - don't cache that, don't let it look "done".
        if not _looks_complete(tmp_dest, src):
            out_s = _probe_duration_seconds(tmp_dest)
            raise RuntimeError(
                f"ffmpeg ({label}) produced an incomplete file for {src}: "
                f"{out_s}s of {total_s}s source - read error on the source media?"
            )

        elapsed = time.monotonic() - started_at
        speed = f"{total_s / elapsed:.1f}x realtime" if elapsed > 0 else "?"
        print(f"Transcoded {src.name} with {label} in {elapsed:.1f}s ({speed})")
    finally:
        _progress.pop(dest, None)


def _probe_duration_seconds(src: Path) -> float | None:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(src)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def probe_duration_seconds(src: Path) -> float | None:
    """
    Public entry point for other modules that need a file's real playable
    duration without duplicating the ffprobe invocation - currently
    video_matcher.scan_video_dir(measure_duration=True), which needs it to
    compute a Parking-mode Timelapse segment's compression_ratio.
    """
    return _probe_duration_seconds(src)


def _last_video_packet_seconds(path: Path, duration_s: float) -> float | None:
    """
    pts_time of the last decodable video packet in roughly the final 15s of
    `path`, or None if nothing decodes there. Cheap - ffprobe seeks to the
    interval rather than scanning the whole file (~0.1s in practice).
    """
    start = max(0.0, duration_s - 15.0)
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-read_intervals", f"{start}%",
         "-select_streams", "v", "-show_entries", "packet=pts_time",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    times = []
    for line in result.stdout.splitlines():
        try:
            times.append(float(line.strip().rstrip(",")))
        except ValueError:
            pass
    return max(times) if times else None


def progress_for(src: Path) -> dict:
    """
    Real-time transcode status for src, meant for a progress bar.

    "fraction" is exact - it's ffmpeg's own reported encode position
    (out_time_us) divided by the source's real duration from ffprobe, not an
    estimate. "eta_seconds" is a live estimate recomputed from the observed
    encode speed so far each time this is called (same idea as any download
    progress bar's ETA - self-corrects as more data comes in) - it's not
    exact, and is None until there's enough progress to extrapolate from.
    """
    dest = _cache_path(src)
    # Only report "done" once the cached file has actually passed its
    # completeness check (ensure_transcoded adds it to _validated) - a
    # corrupt/truncated file on disk that's about to be re-transcoded must not
    # make the UI stop its progress poll early.
    if dest in _validated and dest.exists():
        return {"status": "done", "fraction": 1.0, "eta_seconds": 0}

    entry = _progress.get(dest)
    if entry is None:
        return {"status": "not_started", "fraction": 0.0, "eta_seconds": None}

    fraction = min(1.0, entry["current_s"] / entry["total_s"]) if entry["total_s"] else 0.0
    eta_seconds = None
    if fraction > 0.02:
        elapsed = time.monotonic() - entry["started_at"]
        eta_seconds = round(elapsed / fraction * (1 - fraction), 1)
    return {"status": "transcoding", "fraction": round(fraction, 4), "eta_seconds": eta_seconds}


def _looks_complete(dest: Path, src: Path) -> bool:
    """
    Is the cached transcode at `dest` a usable, complete copy of `src`?

    False if it's empty, doesn't probe as playable video, is more than
    TRUNCATION_TOLERANCE_S shorter than the source, or has no decodable video
    packet within TAIL_TOLERANCE_S of its own reported end. Between them these
    catch a well-formed but truncated ffmpeg output (source read error
    mid-transcode) and a file whose payload was cut off behind an intact
    +faststart moov (partial copy, disk corruption) - a cache hit isn't
    trustworthy on its own.
    """
    try:
        if dest.stat().st_size == 0:
            return False
    except OSError:
        return False
    dest_s = _probe_duration_seconds(dest)
    if dest_s is None or dest_s <= 0:
        return False  # unreadable / no valid duration -> treat as corrupt
    src_s = _probe_duration_seconds(src)
    if src_s is not None and src_s - dest_s > TRUNCATION_TOLERANCE_S:
        return False  # well-formed but truncated output (the observed failure)
    tail = _last_video_packet_seconds(dest, dest_s)
    if tail is None or dest_s - tail > TAIL_TOLERANCE_S:
        return False  # header claims the full length but the payload is cut off
    return True


def _cache_state(dest: Path, src: Path) -> str:
    """
    'ok'      - dest is present and passes the completeness check
    'missing' - dest isn't on disk
    'corrupt' - dest is on disk but truncated / unplayable

    Pure inspection: never deletes or transcodes. Files that pass are memoised
    in _validated so the check's ffprobe calls run once per file per run.
    """
    if dest in _validated:
        return "ok" if dest.exists() else "missing"
    if not dest.exists():
        return "missing"
    if _looks_complete(dest, src):
        _validated.add(dest)
        return "ok"
    return "corrupt"


def ensure_transcoded(src: Path, *, rebuild_corrupt: bool = True) -> Path | None:
    """
    Return a path to a complete H.264 version of src.

    A cached copy is returned only if it passes the completeness check. A
    corrupt cached copy is always deleted; whether it (or a missing one) is
    then rebuilt here depends on rebuild_corrupt:
      - True (default, the on-demand GET /video path): always transcode, so the
        caller gets a usable file back.
      - False (background prefetch): transcode only a genuinely missing copy;
        if the copy was corrupt, evict it and return None, leaving the rebuild
        for whenever the file is actually requested.

    Uses the GPU (nvenc) when DASHCAM_HWACCEL=nvenc and it's usable, retrying
    once with software libx264 if the GPU pass fails. Raises RuntimeError only
    if that fallback also fails or the output is truncated.
    """
    _prune_stale_cache()

    dest = _cache_path(src)
    if _cache_state(dest, src) == "ok":
        return dest

    with _lock_for(dest):
        state = _cache_state(dest, src)  # may have changed while we waited for the lock
        if state == "ok":
            return dest

        if state == "corrupt":
            dest.unlink(missing_ok=True)
            _validated.discard(dest)
            print(f"WARNING: evicted corrupt cached transcode for {src.name} "
                  f"({'re-transcoding now' if rebuild_corrupt else 'will rebuild when next requested'})")
            if not rebuild_corrupt:
                return None

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest.with_suffix(".mp4.partial")
        total_s = _probe_duration_seconds(src) or 0.0

        # Try the GPU first when it's wanted and looks usable; on any failure
        # there, retry once with software (and stop using the GPU this run).
        want_gpu = HWACCEL == "nvenc" and _nvenc_usable()
        attempts = [True, False] if want_gpu else [False]
        last_error: Exception | None = None
        for gpu in attempts:
            try:
                _run_transcode(src, tmp_dest, dest, total_s, gpu=gpu)
                tmp_dest.rename(dest)  # atomic - concurrent requests never see a partial file
                _validated.add(dest)
                return dest
            except RuntimeError as e:
                last_error = e
                tmp_dest.unlink(missing_ok=True)
                if gpu:
                    _disable_nvenc(f"transcode of {src.name} failed")
                    print(f"WARNING: GPU transcode failed for {src.name}; retrying with software.\n{e}")

        raise last_error
