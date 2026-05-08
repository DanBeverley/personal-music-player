from __future__ import annotations

import os
from typing import Tuple


_HOME_HISTORY_POOL_CAP = max(
    12,
    int(os.environ.get("AURALIS_HOME_HISTORY_POOL_CAP", "24")),
)
_HOME_COLLAB_POOL_CAP = max(
    12,
    int(os.environ.get("AURALIS_HOME_COLLAB_POOL_CAP", "24")),
)
_HOME_FALLBACK_POOL_CAP = max(
    16,
    int(os.environ.get("AURALIS_HOME_FALLBACK_POOL_CAP", "32")),
)
_HOME_ANCHOR_LIMIT = max(
    1,
    int(os.environ.get("AURALIS_HOME_ANCHOR_LIMIT", "3")),
)
_HOME_ARTIST_NEIGHBOR_LIMIT = max(
    1,
    int(os.environ.get("AURALIS_HOME_ARTIST_NEIGHBOR_LIMIT", "3")),
)
_HOME_POOL_CANDIDATE_CAP = max(
    16,
    int(os.environ.get("AURALIS_HOME_POOL_CANDIDATE_CAP", "48")),
)
_HOME_ALBUM_CAP = max(
    8,
    int(os.environ.get("AURALIS_HOME_ALBUM_CAP", "18")),
)
_HOME_ARTIST_CAP = max(
    6,
    int(os.environ.get("AURALIS_HOME_ARTIST_CAP", "12")),
)
_HOME_ARTIST_NEIGHBOR_TRACK_LIMIT = max(
    6,
    int(os.environ.get("AURALIS_HOME_ARTIST_NEIGHBOR_TRACK_LIMIT", "10")),
)
_HOME_ARTIST_MEMORY_TTL_SECONDS = max(
    900,
    int(os.environ.get("AURALIS_HOME_ARTIST_MEMORY_TTL_SECONDS", "21600")),
)
_HOME_TODAYS_PICK_CANDIDATE_CAP = max(
    24,
    int(os.environ.get("AURALIS_HOME_TODAYS_PICK_CANDIDATE_CAP", "64")),
)
_HOME_LAUNCH_ROW_CANDIDATE_CAP = max(
    12,
    int(os.environ.get("AURALIS_HOME_LAUNCH_ROW_CANDIDATE_CAP", "28")),
)
_HOME_LAUNCH_TODAYS_PICK_CANDIDATE_CAP = max(
    12,
    int(os.environ.get("AURALIS_HOME_LAUNCH_TODAYS_PICK_CANDIDATE_CAP", "24")),
)
_HOME_LAUNCH_MIX_TRACK_CAP = max(
    6,
    int(os.environ.get("AURALIS_HOME_LAUNCH_MIX_TRACK_CAP", "10")),
)
_HOME_MIX_TRACK_CAP = max(
    8,
    int(os.environ.get("AURALIS_HOME_MIX_TRACK_CAP", "12")),
)
_HOME_MIX_MIN_COUNT = max(
    2,
    int(os.environ.get("AURALIS_HOME_MIX_MIN_COUNT", "2")),
)
_HOME_MIX_MAX_COUNT = max(
    _HOME_MIX_MIN_COUNT,
    min(6, int(os.environ.get("AURALIS_HOME_MIX_MAX_COUNT", "5"))),
)
_HOME_GENRE_TAB_LIMIT = max(
    3,
    min(6, int(os.environ.get("AURALIS_HOME_GENRE_TAB_LIMIT", "5"))),
)
_HOME_GENRE_TRACK_CAP = max(
    8,
    int(os.environ.get("AURALIS_HOME_GENRE_TRACK_CAP", "8")),
)
_HOME_GENRE_CANDIDATE_CAP = max(
    72,
    int(os.environ.get("AURALIS_HOME_GENRE_CANDIDATE_CAP", "160")),
)
_ROW_TRACK_PAGE_SIZE = max(
    4,
    int(os.environ.get("AURALIS_ROW_TRACK_PAGE_SIZE", "4")),
)

_HOME_MIX_ACCENTS: Tuple[str, ...] = (
    "#7C69FF",
    "#4B89FF",
    "#F2EEE6",
    "#59B38C",
    "#E7A64A",
    "#C86B6B",
)
