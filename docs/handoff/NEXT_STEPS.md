# Next Steps

Last reconciled: 2026-08-12

## 1. Two-Feed Ready Queue

Replace the single prepared artifact with a backend-owned queue containing at least two ready future feeds.

Required behavior:

- returning from Search does not force a feed refresh;
- pull-to-refresh immediately promotes the next ready feed;
- app launch may promote a newer ready feed according to an explicit age rule;
- promotion schedules replenishment;
- backup feeds blend retained canonical inventory with bounded fresh acquisition instead of merely reshuffling the same visible items;
- only the backend promotes feed versions; Flutter validates scope/version and provides offline recovery;
- preparation is serialized per user and search intent is consumed once.

Device gate:

- Home return is immediate and does not rebuild;
- pull refresh cannot interrupt another promotion;
- a ready feed remains available after one promotion;
- Popular Radio and other complete rows are not replaced by weaker variants.

## 2. Playback Continuity and Start Latency

Diagnose and fix silent second/later queue items before further cosmetic playback work.

Required behavior:

- canonical track keys remain separate from verified playback source IDs;
- user-requested resolution outranks background verification;
- local prefix/R2 data is served before live upstream resolution;
- bounded starting prefixes survive backend restart;
- alternate verified sources are retained;
- full R2 uploads remain asynchronous;
- tap-to-audio, source-selection, buffering, and audio-session timings are logged compactly;
- background warming remains bounded and does not create provider request floods.

Device gate:

- first, second, and third automatic queue items produce audio;
- manual skips work across queues;
- cached/prefix/R2 tracks approach sub-one-second start;
- never-seen upstream tracks are measured separately.

## 3. Track Metadata and Thumbnail Integrity

- hydrate history rows through the persisted source registry;
- reject canonical identity as proof of playability;
- derive a YouTube thumbnail only from a verified video source ID;
- make playback, details, and lyrics accept the same canonical track key;
- do not publish a playable card without a usable source or retain malformed album/track placeholders.

## 4. Popular Radio Correctness and Rotation

- use canonical seed-artist artwork, never arbitrary track artwork;
- preserve the previous complete card until a replacement has verified artwork and a playable catalog;
- keep mixed-artist collage behavior explicit;
- maintain an independent radio fingerprint and rotation policy;
- enrich from the publish minimum toward the target catalog size;
- prevent cross-card and main-feed duplication.

## 5. Complete Artist Profiles

- expose the canonical public discography with pagination;
- show playable-resolution state per recording/release;
- persist biography and verified artist artwork;
- provide legitimate statistics only when their source and meaning are clear;
- include related artists, complete albums, release dates, likes/listens/views when authoritative.

## 6. Release Metadata Coverage

- continue persisted release date/year enrichment;
- share exhausted lookup state across feeds;
- never substitute a YouTube upload year for a release year without evidence.

## 7. Behind the Lyrics

- verify lyrics payload transfer and canonical track lookup;
- distinguish model/provider failure from limited lyrical context;
- replace the generic fallback with an actionable failure state;
- persist useful structured interpretation without storing long lyric quotations.

## 8. Charts and Trending

Replace Search landing placeholders with researched live data:

- country, platform, and time-range filters;
- animated line/area graphs that draw from zero to current values;
- rank movement, totals, and detailed statistics;
- canonical playable drill-down from chart entries;
- consistent animation language across every graph.

## 9. Structural Cleanup

Only after the behavioral gates above:

- split oversized backend and Flutter files along existing domain boundaries;
- remove dormant fallback paths and obsolete compatibility code;
- preserve one recommendation engine, one search path, and one playback resolver;
- keep focused tests grouped by search, feed lifecycle, metadata, radio, and playback contracts.

## Focused Verification Commands

Use the smallest relevant set rather than running the entire repository:

```powershell
python -B -m pytest ingestion/python_proxy/test_search_snapshot_persistence.py -q
python -B -m pytest ingestion/python_proxy/test_search_latency.py -m search_artwork -q
python -B -m pytest ingestion/python_proxy/test_candidate_inventory_materialization.py ingestion/python_proxy/test_discovery_engine.py -q
python -B -m pytest ingestion/python_proxy/test_source_registry_pipeline.py ingestion/python_proxy/test_stream_prepare_latency.py -q
```
