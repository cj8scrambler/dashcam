"""
Local (non-git) cache of user settings - currently just the data directory path,
so you don't have to pass --data-dir on every run.

Stored outside the repo, under the user's XDG config dir, so it's never at risk
of being committed even if this directory later becomes a git repo.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# $DASHCAM_CONFIG_DIR wins (the Docker image sets it to /config, a bind mount);
# otherwise the XDG location. auth.py reads users.json / secret_key from here too.
CONFIG_DIR = Path(
    os.environ.get("DASHCAM_CONFIG_DIR")
    or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dashcam-viewer"
)
CONFIG_PATH = CONFIG_DIR / "config.json"


def load_data_dir() -> str | None:
    """Return the cached data directory path, or None if nothing is cached yet."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f).get("data_dir")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def save_data_dir(data_dir: str) -> None:
    """Cache the data directory path for future runs."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump({"data_dir": data_dir}, f)
