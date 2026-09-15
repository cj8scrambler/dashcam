"""
Gunicorn config for the dashcam viewer.

    gunicorn -c gunicorn.conf.py 'app:create_app()'

`workers` MUST stay 1. The app keeps per-process in-memory state that a second
worker would corrupt or split:
  - app.STATE  - the parsed GPS fixes and video-segment index, loaded once at
    startup (there is no reload endpoint).
  - video_transcode._locks / _progress / _validated - the per-file transcode
    locks, the live ffmpeg progress the UI polls, and the cache-validation
    memo. A second worker would run ffmpeg against the same cache file in
    parallel and report progress the first worker's clients never see.
Scale with threads, not workers.
"""
import os

bind = f"{os.environ.get('DASHCAM_HOST', '0.0.0.0')}:{os.environ.get('DASHCAM_PORT', '5000')}"

workers = 1
worker_class = "gthread"
# Higher than it looks like it should need to be, on purpose: video_transcode's
# TRANSCODE_CONCURRENCY caps actual ffmpeg processes (default 4) separately -
# most threads here are just *waiting* on that semaphore, not doing CPU work,
# so they're cheap. Too few threads and a burst of /video requests (scrolling
# fast through a stop's clips) exhausts the whole pool waiting on that cap,
# and since workers=1, that starves every OTHER route too (/api/*, even
# /healthz) - confirmed live 2026-09-15, the whole UI froze under load with
# the old default of 16.
threads = int(os.environ.get("DASHCAM_THREADS", "64"))

# A first-view transcode holds its request open for the length of the encode
# (~1 min for a 5-minute clip, more on a slow host) - far past gunicorn's 30s
# default, which would kill the worker mid-encode.
timeout = int(os.environ.get("DASHCAM_TIMEOUT", "600"))
graceful_timeout = 30

# Import the app in the master before forking, so a config error (missing data
# dir, bad timezone) fails loudly at boot instead of a worker-respawn loop.
preload_app = True

accesslog = "-"
errorlog = "-"


def post_fork(server, worker):
    # The periodic re-scan thread must live in the worker: preload_app runs
    # create_app() in the master, and the worker only inherits a *copy* of its
    # STATE, so a thread started in the master would reload state the worker
    # never sees. post_fork runs in each worker after the fork.
    import app
    app.start_rescan_thread()
