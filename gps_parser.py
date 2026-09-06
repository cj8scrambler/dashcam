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

A single .dat file may contain rows spanning more than one calendar day.
Timestamps are naive wall-clock time in the camera's configured timezone
(--record-timezone); every fix is converted to UTC on load so the rest of
the app never has to reason about camera-local time.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


KNOTS_TO_MPH = 1.15078
KNOTS_TO_KMH = 1.852


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
            fixes.append(GpsFix(ts_utc, lat, lon, speed_knots, altitude_m))
    return fixes


def load_gps_dir(directory: str | Path, record_tz: ZoneInfo) -> list[GpsFix]:
    """Parse every .dat file in a directory and return one combined, time-sorted list."""
    directory = Path(directory)
    all_fixes: list[GpsFix] = []
    for dat_path in sorted(directory.glob("*.dat")):
        all_fixes.extend(parse_gps_log(dat_path, record_tz))
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
