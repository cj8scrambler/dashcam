# Vantrue N4S Dashcam Viewer

Speed-colored GPS track on a zoomable map (Leaflet/OpenStreetMap), synced to
front/interior/rear video playback, with a calendar sidebar for picking which
day's data to view.

## Setup

```bash
pip install -r requirements.txt
```

Also requires `ffmpeg`/`ffprobe` in PATH (used to transcode HEVC video for
browser playback - see "How it works" below). If you'd rather not install
anything, see [Docker](#docker) below.

## Run

Point it at the root data directory that contains `Normal/` (video files) and `GPS/` (`.dat` GPS logs) subdirectories.
In order to resolve the timestamps, you also need to pass the timezone that the camera records in (default is US Central).

```bash
python3 app.py --data-dir /path/to/data --record-timezone America/New_York
```

Then open http://localhost:5000 in a browser.

The data directory path is cached locally (outside the repo, under
`~/.config/dashcam-viewer/`) so you only need `--data-dir` once. Passing it
again on a later run overwrites the cached path.


If you want timestamps shown in a diferent timezone than the camera recorded them, then you can spacify:

```bash
python3 app.py --display-timezone America/Los_Angeles
```

`python3 app.py` runs Flask's development server - fine for local use. Every
flag also has a `DASHCAM_*` environment variable (`DASHCAM_DATA_DIR`,
`DASHCAM_RECORD_TZ`, `DASHCAM_DISPLAY_TZ`, `DASHCAM_HOST`, `DASHCAM_PORT`) - see
`python3 app.py --help`.

For a deployment, serve the app factory with gunicorn (this is what the Docker
image does):

```bash
gunicorn -c gunicorn.conf.py 'app:create_app()'
```

`gunicorn.conf.py` pins `workers = 1` on purpose - the app holds per-process
state (the parsed track index and the transcode coordination), so it scales with
threads, not worker processes.

## Docker

```bash
cp .env.example .env          # then edit DASHCAM_DATA_PATH to point at your data
docker compose up --build -d
```

Open <http://127.0.0.1:5000>. By default it's published on localhost only; set
`DASHCAM_BIND=0.0.0.0:5000` in `.env` to reach it from other machines.

- The data directory is mounted **read-only** at `/data`.
- Transcodes are cached in a named volume (`dashcam-cache`) so they survive
  restarts; the app prunes anything older than 14 days on its own.
- `docker compose logs -f` to watch it, `docker compose down` to stop (keeps the
  cache), `docker compose down -v` to also wipe the cache.

## How it works

- `gps_parser.py` - parses the Vantrue N4S GPS `.dat` log format (timestamp,
  lat/N-S, lon/E-W, speed in knots, altitude in meters). A single `.dat` file
  may contain rows spanning multiple calendar days; timestamps are naive
  camera-local wall-clock time, localized via `--record-timezone` and
  converted to UTC on load.
- `video_matcher.py` - scans the `Normal/` video directory and maps a GPS
  timestamp to the covering video segment per channel. Filenames look like
  `YYYYMMDD_HHMMSS_<seq>_N_<channel>.MP4` (e.g.
  `20260904_175735_00001_N_A.MP4`), channel A=front, B=interior, C=rear. The
   loop-recording segment length isn't assumed - it's detected from the
   actual gaps between consecutive segments' start times. A segment that ends
   early (the camera lost power when the ignition turned off, a restart, the
   last clip of a drive) only "covers" up to the next segment's start, not a
   full loop length. Dedicated parking-mode clips live in a separate
   `Parking/` directory and aren't handled yet.
- `config.py` - caches the last-used `--data-dir` path in
  `~/.config/dashcam-viewer/config.json`, outside the repo.
- `video_transcode.py` - the camera records HEVC, which Chrome/Firefox won't
  decode in a `<video>` element (plays audio, shows a black frame). This
  transcodes each clip to H.264 the first time it's viewed and caches the
  result under `~/.cache/dashcam-viewer/transcoded/`, subsequent views of
  the same clip are instant. First view of a clip takes roughly a minute
  with software encode; the frontend shows live progress. Each cached file is
  sanity-checked before use; a bad one is deleted and rebuilt the next time
  that clip is actually requested. Cached clips older
  than `MAX_CACHE_AGE_DAYS` are deleted automatically so the cache doesn't grow
  forever. Once a clip finishes, its neighboring segments start transcoding
  in the background so scrubbing along the track stays fast.
- `app.py` - Flask backend: serves the map page, `/api/days` (which calendar
  days have GPS data), `/api/track` (GPS track as JSON, optionally filtered
  to one day), `/api/videos?ts=...` (which video files + seek offset cover a
  given UTC timestamp), `/api/transcode-progress` (live transcode status for
  the progress bar), and `/video/<filename>` (the transcoded video stream).
- `templates/index.html` - Leaflet map with the track drawn as many short
  polyline segments, each colored by that segment's speed (blue -> green ->
  yellow -> red -> purple. A calendar widget in the sidebar marks which days
  have data; selecting one
  loads that day's track. Clicking any point on the path drops a marker there
  and loads the matching video(s) in the side panel, seeked to the right
  moment; the marker then follows actual playback position as the video
  plays. Playback is kept in sync across channels - play/pause/seek on any
  one video applies to all of them - and only the front camera plays audio
  (the others would just echo). When channels' clips are different lengths,
  the shorter one goes blank while the rest keep playing, and playback only
  advances to the next segment once every channel has finished, so time
  always moves forward. Playing a video switches to a larger
  "theater mode" layout; a "Back to map" button (or Escape) reverses it.
