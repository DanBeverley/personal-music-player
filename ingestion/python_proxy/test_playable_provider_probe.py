from __future__ import annotations

import json
import os
import pathlib
import sys
import unittest


CURRENT_DIR = pathlib.Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from auralis_backend.discovery.structured_providers import (  # noqa: E402
    CanonicalRecording,
    LastFmClient,
    ListenBrainzClient,
    configured_provider_value,
)
from auralis_backend.search.musicbrainz import (  # noqa: E402
    MusicBrainzClient,
    musicbrainz_recording_to_item,
)


PROBE_SEEDS = (
    ("Bohemian Rhapsody", "Queen"),
    ("Highway to Hell", "AC/DC"),
    ("Holy Diver", "Dio"),
    ("Enter Sandman", "Metallica"),
    ("Sweet Child O' Mine", "Guns N' Roses"),
    ("Stairway to Heaven", "Led Zeppelin"),
    ("Creep", "Radiohead"),
    ("Layla", "Derek and the Dominos"),
    ("Hotel California", "Eagles"),
    ("Californication", "Red Hot Chili Peppers"),
)


def _log(stage: str, **payload) -> None:
    print(
        f"[provider-probe][{stage}] "
        + json.dumps(payload, ensure_ascii=True, sort_keys=True),
        flush=True,
    )


def _canonical_recordings() -> list[CanonicalRecording]:
    client = MusicBrainzClient()
    output: list[CanonicalRecording] = []
    for title, artist in PROBE_SEEDS:
        query = f'recording:"{title}" AND artist:"{artist}"'
        rows = client.search_recordings(query, limit=5)
        item = next(
            (
                musicbrainz_recording_to_item(row, query=query)
                for row in rows
                if isinstance(row, dict)
            ),
            {"title": title, "artist": artist},
        )
        recording = CanonicalRecording.from_item(item)
        if recording.title and recording.artist:
            output.append(recording)
    return output


@unittest.skipUnless(
    os.environ.get("RUN_PROVIDER_PROBE", "").strip() == "1",
    "set RUN_PROVIDER_PROBE=1 for live provider acceptance",
)
class ProductionProviderProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import server as production_server

        cls.server = production_server
        cls.recordings = _canonical_recordings()
        if len(cls.recordings) < 8:
            raise AssertionError("MusicBrainz canonicalized fewer than eight probe seeds")

    def test_listenbrainz_supplies_useful_artist_catalogs(self) -> None:
        client = ListenBrainzClient(self.server)
        sampled = [row for row in self.recordings if row.artist_mbid]
        useful = 0
        for recording in sampled:
            try:
                rows = client.top_recordings(recording.artist_mbid, limit=20)
            except Exception as error:
                rows = []
                _log(
                    "listenbrainz_failure",
                    artist=recording.artist,
                    error=type(error).__name__,
                )
            canonical = {
                str(row.get("musicbrainz_recording_id") or "")
                for row in rows
                if row.get("musicbrainz_recording_id")
            }
            useful += int(len(canonical) >= 5)
            _log("listenbrainz", artist=recording.artist, canonical_recordings=len(canonical))
        self.assertTrue(sampled)
        self.assertGreaterEqual(useful / len(sampled), 0.8)

    @unittest.skipUnless(configured_provider_value("LASTFM_API_KEY"), "LASTFM_API_KEY is required")
    def test_lastfm_supplies_ten_unique_canonicalizable_neighbours(self) -> None:
        client = LastFmClient(self.server)
        useful = 0
        for recording in self.recordings:
            rows = client.similar_tracks(recording, limit=30)
            unique = {
                (
                    str(row.get("musicbrainz_recording_id") or ""),
                    str(row.get("title") or "").casefold(),
                    str(row.get("artist") or "").casefold(),
                )
                for row in rows
                if row.get("musicbrainz_recording_id")
                or (row.get("title") and row.get("artist"))
            }
            useful += int(len(unique) >= 10)
            _log("lastfm", track_key=recording.track_key, unique_neighbours=len(unique))
        self.assertGreaterEqual(useful / len(self.recordings), 0.8)

if __name__ == "__main__":
    unittest.main(verbosity=2)
