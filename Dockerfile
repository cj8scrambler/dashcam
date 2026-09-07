# Vantrue N4S Dashcam Viewer
#
#   docker compose up --build
#
# Config is entirely via env vars (see .env.example / docker-compose.yml);
# no CLI args are needed. Mount the dashcam data directory read-only at /data
# and give /cache a persistent volume so transcodes survive restarts.

FROM python:3.12-slim

# ffmpeg + ffprobe: the camera records HEVC, which browsers won't play in a
# <video> element, so every clip is transcoded to H.264 on first view.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run unprivileged. /cache and /config must be writable by this user; /data is
# mounted read-only at runtime. (Before COPY so a code change doesn't rebuild it.)
RUN useradd --create-home --uid 1000 dashcam \
 && mkdir -p /data /cache /config \
 && chown dashcam:dashcam /cache /config

# App code, owned by the runtime user (so its own file mode on the host - which
# may be restrictive - doesn't block the import). .dockerignore keeps out .git,
# .venv, docs and dev files.
COPY --chown=dashcam:dashcam . .
USER dashcam

ENV DASHCAM_HOST=0.0.0.0 \
    DASHCAM_PORT=5000 \
    DASHCAM_THREADS=16 \
    XDG_CACHE_HOME=/cache \
    XDG_CONFIG_HOME=/config \
    PYTHONUNBUFFERED=1

EXPOSE 5000

# urlopen raises (non-zero exit) on any non-2xx or connection failure. /api/days
# needs the parsed GPS state, so this also confirms the data load succeeded.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/days', timeout=4)"]

# gunicorn with the app factory. Config (crucially workers=1) is in
# gunicorn.conf.py. Override the command to get a shell or `python app.py --help`.
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:create_app()"]
