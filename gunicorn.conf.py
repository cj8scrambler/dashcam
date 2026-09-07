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
threads = int(os.environ.get("DASHCAM_THREADS", "16"))

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
