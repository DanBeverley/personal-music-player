"""Rank provider candidates once. Ranking never rejects an inventory item."""
from __future__ import annotations

from .intelligence import search_text_similarity, search_query_key


def normalize_provider_top(entries):
    """Respect provider browse identity, even when its renderer type is wrong."""
    normalized = []
    for entry in entries:
        item = dict(entry)
        if item.get('resultType') == 'artist':
            artist = next((a for a in item.get('artists') or []
                           if isinstance(a, dict) and a.get('id')), {})
            item['browseId'] = item.get('browseId') or artist.get('id')
            item['artist'] = item.get('artist') or item.get('name') or item.get('title') or artist.get('name')
        if item.get('resultType') == 'album' and str(item.get('browseId') or '').startswith('VL'):
            item['resultType'] = 'playlist'
        normalized.append(item)
    return normalized


def rank_provider_inventory(query, tracks, provider_top, *, semantic_scores=None):
    top = next(iter(provider_top), {})
    top_id = top.get('videoId')
    top_kind = top.get('resultType')
    top_artists = {a.get('id') for a in top.get('artists') or [] if isinstance(a,dict) and a.get('id')}
    top_album = top.get('browseId') if top_kind=='album' else None
    # A provider official-video lead is stronger than a same-title cover shelf.
    official_top = top.get('videoType') in {'MUSIC_VIDEO_TYPE_OMV','MUSIC_VIDEO_TYPE_ATV'}
    semantic_scores = semantic_scores or {}
    ranked=[]
    for index,track in enumerate(tracks):
        kind=track.get('search_provider_kind')
        rank=max(1,int(track.get('search_provider_rank') or index+1))
        score=1/(rank+1) if kind=='songs' else .28/(rank+1)
        title=track.get('title') or ''
        title_match=search_text_similarity(query,title)
        # This is a positive ranking signal, never an admission threshold.
        score += .16*title_match
        if track['id']==top_id and (top_kind=='song' or official_top):
            score+=.6
        if top_album and track.get('album_id')==top_album:
            score+=.6
        elif top_kind=='album' and top_artists.intersection(track.get('artist_ids') or []):
            score+=.45
        # Optional semantic scores participate here, not in a second resolver.
        semantic=max(0.,min(1.,float(semantic_scores.get(track['id'],0))))
        score+=.12*semantic
        item=dict(track,score=round(score,6),ranking_features={
            'provider_kind':kind,'provider_rank':rank,'title_similarity':round(title_match,4),
            'semantic_similarity':round(semantic,4) if semantic_scores else None})
        ranked.append((score,-index,item))
    ranked.sort(key=lambda row:(row[0],row[1]),reverse=True)
    return [r[2] for r in ranked],{'policy':'provider_order_v1','semantic_used':bool(semantic_scores),
        'semantic_status':'qualified_scores' if semantic_scores else 'not_qualified',
        'input_count':len(tracks),'output_count':len(ranked)}
