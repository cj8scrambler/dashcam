"""
Parser for Vantrue N4S GPS log .dat files.

Format (one fix per row):
    YYYYMMDDHHMMSS,lat,N/S,lon,E/W,speed_knots,altitude_m

Example:
    20260904231934,38.883537,N,87.442368,W,55.690,115.800

Real files also contain a leading "#timestamp, latitude,..." comment row and
a bare "#" separator line between recording sessions - both fail float/date
parsing and are dropped by the same malformed-row handling as blank lines,
not treated specially.

Rows where a coordinate is 0.000000 (the camera still acquiring, or a degraded
fix while parked) are also dropped - they're well-formed but would otherwise
plot on the equator / prime meridian.

A single .dat file may contain rows spanning more than one calendar day.
Timestamps are naive wall-clock time in the camera's configured timezone
(--record-timezone); every fix is converted to UTC on load so the rest of
the app never has to reason about camera-local time.

load_gps_dir() caches each file's parsed fixes keyed by (mtime_ns, size, tz),
so a re-scan (app.reload_state) only re-parses the file(s) that actually grew -
which, once footage is syncing in, is normally just the current day's log.
"""
from __future__ import annotations

import csv
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


KNOTS_TO_MPH = 1.15078
KNOTS_TO_KMH = 1.852

# The camera writes 0.000000 for a coordinate it doesn't have yet (GPS still
# acquiring, or degraded while parked). Those rows are otherwise well-formed, so
# they'd otherwise plot as real fixes on the equator / prime meridian and draw a
# polyline straight out to Null Island and back. A coordinate that rounds to
# zero is never a real position for this camera's use, so drop the whole fix.
_ZERO_COORD_EPSILON = 1e-4


@dataclass
class GpsFix:
    timestamp: datetime          # UTC, tz-aware
    lat: float                   # decimal degrees, signed (+N / -S)
    lon: float                   # decimal degrees, signed (+E / -W)
    speed_knots: float
    altitude_m: float

    @property
    def speed_mph(self) -> float:
        return self.speed_knots * KNOTS_TO_MPH

    @property
    def speed_kmh(self) -> float:
        return self.speed_knots * KNOTS_TO_KMH


def parse_gps_log(path: str | Path, record_tz: ZoneInfo) -> list[GpsFix]:
    """
    Parse a Vantrue N4S GPS .dat log.

    Args:
        path: path to the .dat file.
        record_tz: timezone the camera's clock was set to when it recorded
            these timestamps (e.g. ZoneInfo("America/Chicago")). Every
            timestamp is localized to this zone, then converted to UTC.

    Returns:
        List of GpsFix records (UTC timestamps), in file order.
    """
    fixes: list[GpsFix] = []
    with open(path, newline="") as f:
        reader = csv.reader(f, skipinitialspace=True)
        for row in reader:
            row = [c.strip() for c in row if c.strip() != ""]
            if len(row) < 7:
                continue  # skip blank/malformed lines
            try:
                ts_raw, lat_raw, ns, lon_raw, ew, speed_raw, alt_raw = row[:7]
                ts_local = datetime.strptime(ts_raw, "%Y%m%d%H%M%S").replace(tzinfo=record_tz)
                ts_utc = ts_local.astimezone(timezone.utc)
                lat = float(lat_raw) * (1 if ns.upper() == "N" else -1)
                lon = float(lon_raw) * (1 if ew.upper() == "E" else -1)
                speed_knots = float(speed_raw)
                altitude_m = float(alt_raw)
            except ValueError:
                # Malformed row - skip rather than crash the whole load
                continue
            if abs(lat) < _ZERO_COORD_EPSILON or abs(lon) < _ZERO_COORD_EPSILON:
                continue  # 0.000000 in a coordinate = no fix, not a trip to Null Island
            fixes.append(GpsFix(ts_utc, lat, lon, speed_knots, altitude_m))
    return fixes


# path -> (mtime_ns, size, tz_key, [GpsFix]). Guarded by _cache_lock so
# concurrent load_gps_dir() calls (a periodic re-scan racing a POST /api/reload)
# don't corrupt it.
_parse_cache: dict[str, tuple[int, int, str, list[GpsFix]]] = {}
_cache_lock = threading.Lock()


def load_gps_dir(directory: str | Path, record_tz: ZoneInfo) -> list[GpsFix]:
    """
    Parse every .dat file in a directory and return one combined, time-sorted
    list. Files unchanged since the previous call (same mtime, size, timezone)
    are served from cache instead of being re-parsed; entries for files that
    have since disappeared are dropped.
    """
    directory = Path(directory)
    tz_key = str(record_tz)
    all_fixes: list[GpsFix] = []

    with _cache_lock:
        present: set[str] = set()
        for dat_path in sorted(directory.glob("*.dat")):
            key = str(dat_path.resolve())
            present.add(key)
            try:
                st = dat_path.stat()
            except OSError:
                continue  # vanished between glob and stat
            sig = (st.st_mtime_ns, st.st_size, tz_key)
            cached = _parse_cache.get(key)
            if cached and cached[:3] == sig:
                all_fixes.extend(cached[3])
            else:
                fixes = parse_gps_log(dat_path, record_tz)
                _parse_cache[key] = (*sig, fixes)
                all_fixes.extend(fixes)
        for gone in _parse_cache.keys() - present:
            del _parse_cache[gone]

    all_fixes.sort(key=lambda f: f.timestamp)
    return all_fixes


if __name__ == "__main__":
    import sys
    test_path = sys.argv[1] if len(sys.argv) > 1 else "sample_gps.dat"
    fixes = parse_gps_log(test_path, record_tz=ZoneInfo("America/Chicago"))
    print(f"Parsed {len(fixes)} fixes")
    for fx in fixes[:5]:
        print(f"  {fx.timestamp}  lat={fx.lat:.6f} lon={fx.lon:.6f} "
              f"speed={fx.speed_mph:.1f}mph alt={fx.altitude_m:.1f}m")
