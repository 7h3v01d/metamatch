"""
config.py
Small persistence layer for settings that need to survive across restarts
but don't belong in the in-memory session state - currently just the TMDB
API key. Checked in order: TMDB_API_KEY environment variable, then a
local JSON file in the user's home directory.

TMDB requires a free API key (https://www.themoviedb.org/settings/api) -
MusicBrainz doesn't need one, which is why this module only exists for
the movie side of MetaMatch.
"""

from __future__ import annotations

import json
import os

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".metamatch")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


def _read_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_config(data: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


def get_tmdb_api_key() -> str | None:
    env_key = os.environ.get("TMDB_API_KEY")
    if env_key:
        return env_key
    return _read_config().get("tmdb_api_key") or None


def set_tmdb_api_key(key: str) -> None:
    data = _read_config()
    data["tmdb_api_key"] = key.strip()
    _write_config(data)


def mask_key(key: str) -> str:
    if not key or len(key) < 8:
        return "••••••••"
    return key[:4] + "…" + key[-4:]
