from __future__ import annotations

from typing import Any, Dict, List


ROW_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "todays_pick": {
        "title": "Hot for you",
        "order": -20,
        "required_default": False,
        "row_tier": "launch",
        "allocator": {},
        "ranking": {
            "quality_floor": 1.0,
            "min_items": 1,
            "max_same_artist": 1,
            "max_feed_same_artist": 1,
        },
    },
    "mixed_for_you": {
        "title": "Mixed for you",
        "order": -10,
        "required_default": False,
        "row_tier": "launch",
        "allocator": {},
        "ranking": {},
    },
    "continue_listening": {
        "title": "Continue the vibe",
        "order": 0,
        "required_default": True,
        "row_tier": "launch",
        "live_refreshable": True,
        "allocator": {
            "candidate_limit": 64,
            "max_pools": 4,
            "model_key": "home_row_allocator_continue_v1",
        },
        "ranking": {
            "model_key": "home_continue_ranker_v1",
            "quality_floor": 1.0,
            "min_items": 2,
            "max_same_artist": 3,
            "max_feed_same_artist": 2,
            "base_bias": 0.7,
            "similarity_weights": {
                "anchor": 4.8,
                "short": 4.2,
            },
            "presence_bias": {
                "recent_bonus": -0.6,
            },
        },
    },
    "because_you_played": {
        "title": "Because you played recently",
        "order": 1,
        "required_default": True,
        "row_tier": "launch",
        "allocator": {
            "candidate_limit": 64,
            "max_pools": 4,
            "model_key": "home_row_allocator_because_v1",
        },
        "ranking": {
            "model_key": "home_because_played_ranker_v1",
            "quality_floor": 1.0,
            "min_items": 2,
            "max_same_artist": 2,
            "max_feed_same_artist": 2,
            "base_bias": 0.4,
            "similarity_weights": {
                "anchor": 6.2,
                "short": 2.4,
                "artist": 1.6,
            },
            "feature_weights": {
                "peer_artist_bonus": 0.35,
                "scene_affinity": 0.8,
                "era_affinity": 0.35,
                "adjacent_era_affinity": 0.15,
                "type_affinity": 0.12,
                "script_affinity": 0.1,
            },
            "presence_bias": {
                "recent_bonus": -1.4,
            },
        },
    },
    "listeners_like_you": {
        "title": "Listeners like you also played",
        "order": 2,
        "required_default": False,
        "row_tier": "deferred",
        "allocator": {
            "candidate_limit": 48,
            "max_pools": 4,
            "model_key": "home_row_allocator_listeners_v1",
        },
        "ranking": {
            "model_key": "home_global_ranker_v4",
            "quality_floor": 1.0,
            "min_items": 2,
            "max_same_artist": 2,
            "max_feed_same_artist": 1,
            "base_bias": 0.2,
            "similarity_weights": {
                "taste": 1.2,
                "artist": 0.6,
            },
            "feature_weights": {
                "peer_artist_bonus": 0.8,
                "scene_affinity": 1.05,
                "peer_scene_bonus": 0.7,
                "era_affinity": 0.45,
                "adjacent_era_affinity": 0.25,
                "script_affinity": 0.12,
                "dominant_artist_penalty": -0.25,
            },
        },
    },
    "frequently_listened": {
        "title": "Frequently listened",
        "order": 3,
        "required_default": False,
        "row_tier": "deferred",
        "allocator": {
            "candidate_limit": 40,
            "max_pools": 3,
            "model_key": "home_row_allocator_frequent_v1",
        },
        "ranking": {
            "model_key": "home_global_ranker_v4",
            "quality_floor": 1.35,
            "min_items": 3,
            "max_same_artist": 3,
            "max_feed_same_artist": 2,
            "similarity_weights": {
                "long": 4.8,
                "short": 1.6,
            },
            "presence_bias": {
                "top_bonus": 2.1,
            },
            "absence_bias": {
                "top_bonus": -1.8,
            },
        },
    },
    "rediscover": {
        "title": "Rediscover these",
        "order": 4,
        "required_default": False,
        "row_tier": "launch",
        "allocator": {
            "candidate_limit": 56,
            "max_pools": 4,
            "model_key": "home_row_allocator_rediscover_v1",
        },
        "ranking": {
            "model_key": "home_discovery_ranker_v1",
            "quality_floor": 1.15,
            "min_items": 2,
            "max_same_artist": 2,
            "max_feed_same_artist": 1,
            "similarity_weights": {
                "long": 5.6,
                "artist": 1.8,
                "short": -2.8,
            },
            "feature_weights": {
                "peer_artist_bonus": 0.75,
                "scene_affinity": 1.0,
                "peer_scene_bonus": 0.6,
                "era_affinity": 0.7,
                "adjacent_era_affinity": 0.28,
                "type_affinity": 0.2,
                "script_affinity": 0.12,
                "dominant_artist_penalty": -0.55,
            },
            "presence_bias": {
                "top_bonus": 1.0,
                "recent_bonus": -2.4,
            },
        },
    },
    "trending_by_genre": {
        "title": "Trending by genre",
        "order": 5,
        "required_default": False,
        "row_tier": "deferred",
        "allocator": {},
        "ranking": {},
    },
    "recommended_albums": {
        "title": "Recommended albums",
        "order": 6,
        "required_default": False,
        "row_tier": "launch",
        "allocator": {
            "candidate_limit": 18,
            "max_pools": 0,
            "model_key": "home_row_allocator_albums_v1",
        },
        "ranking": {
            "model_key": "home_global_ranker_v4",
            "quality_floor": 1.0,
            "min_items": 1,
            "max_same_artist": 2,
            "max_feed_same_artist": 1,
        },
    },
    "recommended_artists": {
        "title": "Recommended artists",
        "order": 7,
        "required_default": False,
        "row_tier": "launch",
        "allocator": {
            "candidate_limit": 12,
            "max_pools": 0,
            "model_key": "home_row_allocator_artists_v1",
        },
        "ranking": {
            "model_key": "home_artist_ranker_v1",
            "quality_floor": 1.0,
            "min_items": 1,
            "max_same_artist": 1,
            "max_feed_same_artist": 1,
        },
    },
    "deep_cuts": {
        "title": "Deep cuts for you",
        "order": 8,
        "required_default": False,
        "row_tier": "deferred",
        "allocator": {
            "candidate_limit": 56,
            "max_pools": 4,
            "model_key": "home_row_allocator_deep_cuts_v1",
        },
        "ranking": {
            "model_key": "home_discovery_ranker_v1",
            "quality_floor": 1.15,
            "min_items": 2,
            "max_same_artist": 2,
            "max_feed_same_artist": 1,
            "base_bias": 0.9,
            "similarity_weights": {
                "long": 4.8,
                "artist": 3.2,
                "short": -1.4,
            },
            "feature_weights": {
                "peer_artist_bonus": 1.15,
                "scene_affinity": 1.4,
                "peer_scene_bonus": 0.95,
                "era_affinity": 0.9,
                "adjacent_era_affinity": 0.42,
                "type_affinity": 0.3,
                "script_affinity": 0.15,
                "dominant_artist_penalty": -0.95,
            },
            "presence_bias": {
                "top_bonus": -1.8,
            },
        },
    },
    "offline_ready": {
        "title": "Ready offline",
        "order": 9,
        "required_default": False,
        "row_tier": "deferred",
        "allocator": {
            "candidate_limit": 40,
            "max_pools": 3,
            "model_key": "home_row_allocator_offline_v1",
        },
        "ranking": {
            "model_key": "home_global_ranker_v4",
            "quality_floor": 1.35,
            "min_items": 3,
            "max_same_artist": 3,
            "max_feed_same_artist": 2,
            "similarity_weights": {
                "taste": 1.8,
                "long": 1.4,
            },
            "presence_priority_bias": [
                ("offline_bonus", 3.0),
                ("library_bonus", 1.0),
            ],
            "presence_priority_default": -2.5,
        },
    },
    "trending_for_you": {
        "title": "Trending for you",
        "order": 10,
        "required_default": True,
        "row_tier": "deferred",
        "allocator": {
            "candidate_limit": 72,
            "max_pools": 6,
            "model_key": "home_row_allocator_trending_v1",
        },
        "ranking": {
            "model_key": "home_trending_ranker_v1",
            "quality_floor": 1.0,
            "min_items": 2,
            "max_same_artist": 2,
            "max_feed_same_artist": 1,
            "base_bias": 0.25,
            "similarity_weights": {
                "taste": 3.9,
                "artist": 1.5,
                "short": 0.8,
                "long": 0.55,
            },
            "feature_weights": {
                "peer_artist_bonus": 1.45,
                "scene_affinity": 1.7,
                "peer_scene_bonus": 1.05,
                "era_affinity": 0.95,
                "adjacent_era_affinity": 0.5,
                "type_affinity": 0.22,
                "script_affinity": 0.18,
                "dominant_artist_penalty": -1.05,
            },
        },
    },
    "quiet_picks": {
        "title": "Quiet picks",
        "order": 11,
        "required_default": True,
        "row_tier": "deferred",
        "allocator": {
            "candidate_limit": 120,
            "max_pools": 7,
            "model_key": "home_row_allocator_quiet_v1",
        },
        "ranking": {
            "model_key": "home_quiet_ranker_v1",
            "quality_floor": 1.0,
            "min_items": 2,
            "max_same_artist": 2,
            "max_feed_same_artist": 1,
            "base_bias": 0.3,
            "similarity_weights": {
                "taste": 3.7,
                "artist": 1.2,
                "anchor": 0.8,
                "short": 1.6,
                "long": 1.2,
            },
            "feature_weights": {
                "peer_artist_bonus": 1.2,
                "scene_affinity": 1.45,
                "peer_scene_bonus": 1.0,
                "genre_affinity": 1.0,
                "subgenre_affinity": 0.65,
                "era_affinity": 0.85,
                "adjacent_era_affinity": 0.35,
                "language_affinity": 0.24,
                "type_affinity": 0.12,
                "script_affinity": 0.18,
                "dominant_artist_penalty": -0.85,
            },
            "presence_bias": {
                "recent_bonus": -0.45,
                "top_bonus": 0.3,
            },
        },
    },
}


def row_definition(row_kind: str) -> Dict[str, Any]:
    return dict(ROW_DEFINITIONS.get(row_kind) or {})


def ordered_row_kinds() -> List[str]:
    return [
        row_kind
        for row_kind, _definition in sorted(
            ROW_DEFINITIONS.items(),
            key=lambda item: int((item[1] or {}).get("order") or 0),
        )
    ]


def row_order_index(row_kind: str) -> int:
    return int((ROW_DEFINITIONS.get(row_kind) or {}).get("order") or 100)


def row_title_template(row_kind: str) -> str:
    definition = ROW_DEFINITIONS.get(row_kind) or {}
    return str(definition.get("title") or row_kind.replace("_", " ").title())


def allocator_settings(row_kind: str) -> Dict[str, Any]:
    definition = ROW_DEFINITIONS.get(row_kind) or {}
    return dict(definition.get("allocator") or {})


def ranking_settings(row_kind: str) -> Dict[str, Any]:
    definition = ROW_DEFINITIONS.get(row_kind) or {}
    return dict(definition.get("ranking") or {})


def max_feed_same_artist(row_kind: str) -> int:
    return int((ranking_settings(row_kind) or {}).get("max_feed_same_artist") or 2)


def default_required_row_kinds() -> List[str]:
    return [
        row_kind
        for row_kind in ordered_row_kinds()
        if bool((ROW_DEFINITIONS.get(row_kind) or {}).get("required_default"))
    ]


def row_tier(row_kind: str) -> str:
    return str((ROW_DEFINITIONS.get(row_kind) or {}).get("row_tier") or "deferred")


def launch_row_kinds() -> List[str]:
    return [
        row_kind
        for row_kind in ordered_row_kinds()
        if row_tier(row_kind) == "launch"
    ]


def deferred_row_kinds() -> List[str]:
    return [
        row_kind
        for row_kind in ordered_row_kinds()
        if row_tier(row_kind) == "deferred"
    ]


def live_refreshable_row_kinds() -> List[str]:
    return [
        row_kind
        for row_kind in ordered_row_kinds()
        if bool((ROW_DEFINITIONS.get(row_kind) or {}).get("live_refreshable"))
    ]


_THIN_SNAPSHOT_ROW_KINDS = {
    "mixed_for_you",
    "continue_listening",
    "because_you_played",
    "frequently_listened",
    "recommended_albums",
    "recommended_artists",
}


def supports_thin_snapshot(row_kind: str) -> bool:
    return row_kind in _THIN_SNAPSHOT_ROW_KINDS


def thin_snapshot_row_kinds() -> List[str]:
    return [
        row_kind
        for row_kind in ordered_row_kinds()
        if supports_thin_snapshot(row_kind)
    ]


def rich_snapshot_row_kinds() -> List[str]:
    return ordered_row_kinds()
