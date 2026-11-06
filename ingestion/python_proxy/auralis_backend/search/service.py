"""One provider inventory, one ranking decision, immutable paged results."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor, wait, as_completed, FIRST_COMPLETED
from copy import deepcopy
import hashlib
import json
import re
import threading
import time
import weakref
from uuid import uuid4
from typing import Any, Dict, List

from ..domain.catalog import cache_search_payload, normalized_popularity
from ..recommend.store_runtime import open_recommendation_store_connection
from ..storage.cache_runtime import retained_size
from .intelligence import search_query_key, load_catalog_artist_records
from ..storage.artist_artwork import register_artist_metadata_listener, attach_persisted_artist_artwork, artist_artwork_is_pending
from .runtime import search_canonical_album_for_track
from .pipeline import rank_provider_inventory, normalize_provider_top
from .runtime import search_artists_direct_cached, semantic_search_suggestion_items
from .server_adapter import SearchServerAdapter
from .upstream_runtime import normalize_song_result, _search_http_deadline

_SEARCH_SNAPSHOT_PERSIST_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='search-persist')
_SEARCH_BACKGROUND = ThreadPoolExecutor(max_workers=2, thread_name_prefix='search-refresh')
_BACKGROUND_ADMISSION = threading.BoundedSemaphore(16)
_PERSIST_ADMISSION = threading.BoundedSemaphore(32)
_NAMESPACE = 'provider-search-v2||'
_MAX_ENTRIES = 96
_MAX_BYTES = 32 * 1024 * 1024
_FRESH_SECONDS = 900
_EMPTY_FRESH_SECONDS = 5
_RETRY_SECONDS = 30
_PROVIDER_SECONDS = 3.0
_SURFACES = ('tracks', 'artists', 'albums', 'playlists')
_OWNERS = weakref.WeakSet()
_MEMORY_IDLE_SECONDS = 1800
_METADATA_MAX_PASSES = 48


def prune_search_memory(now=None):
    now = time.time() if now is None else now
    for owner in list(_OWNERS):
        with owner._condition:
            for key, accessed in list(owner._accessed.items()):
                if now - accessed >= _MEMORY_IDLE_SECONDS and key not in owner._inflight and key not in owner._pending:
                    owner._snapshots.pop(key, None)
                    owner._sizes.pop(key, None)
                    owner._accessed.pop(key, None)


def _artwork_updated(artist):
    for owner in list(_OWNERS):
        owner._artwork_updated(artist)


register_artist_metadata_listener(_artwork_updated)


def _identity(item):
    return str(item.get('id') or item.get('videoId') or item.get('browseId') or '')


def _unique(items):
    output, seen = [], {}
    for item in items:
        identity = _identity(item)
        if identity and identity not in seen:
            seen[identity] = len(output)
            output.append(dict(item))
        elif identity:
            target = output[seen[identity]]
            target.update({k:v for k,v in item.items() if v and not target.get(k)})
    return output


def _key(query):
    return _NAMESPACE + search_query_key(query)


def _empty_inventory(snapshot):
    return not any(snapshot.get(surface) for surface in _SURFACES)


def _snapshot_stale(snapshot, now):
    age = _EMPTY_FRESH_SECONDS if _empty_inventory(snapshot) else _FRESH_SECONDS
    return now - snapshot['fetched_at'] >= age


def _persist(server, key, snapshot):
    connection = None
    try:
        connection = open_recommendation_store_connection(server)
        connection.execute('''INSERT INTO search_snapshots
            (snapshot_key,payload_json,revision,last_accessed,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(snapshot_key) DO UPDATE SET payload_json=excluded.payload_json,
            revision=excluded.revision,last_accessed=excluded.last_accessed,updated_at=excluded.updated_at
            WHERE search_snapshots.revision <= excluded.revision''',
            (key,json.dumps(snapshot,ensure_ascii=False),snapshot['revision'],time.time(),time.time()))
        # This namespace owns its retention; older rollback snapshots are untouched.
        connection.execute('''DELETE FROM search_snapshots WHERE snapshot_key IN (
            SELECT snapshot_key FROM search_snapshots WHERE snapshot_key LIKE ?
            ORDER BY last_accessed DESC LIMIT -1 OFFSET ?)''', (_NAMESPACE+'%',_MAX_ENTRIES))
        connection.commit()
    except Exception as exc:
        print(f'[EBB:search] event=persist_failed error={type(exc).__name__}', flush=True)
    finally:
        if connection is not None:
            connection.close()


class SearchService:
    def __init__(self, server: Any):
        self._server = server
        self._condition = threading.Condition(threading.RLock())
        self._snapshots = OrderedDict()
        self._sizes = {}
        self._accessed = {}
        self._pending = set()
        self._inflight = {}
        _OWNERS.add(self)

    @staticmethod
    def _same_artist(artist, details):
        ids = {str(details.get(k) or '') for k in ('id','provider_artist_id','browseId','artist_id')}
        return bool(_identity(artist) in ids and search_query_key(artist.get('name') or '')
                    == search_query_key(details.get('name') or ''))

    def _merge_artist(self, snapshot, artist_id, details):
        for artist in [*snapshot.get('artists',[]), *snapshot.get('similar_artists',[]),
                       snapshot.get('lead_artist') or {}, (snapshot.get('top_result') or {}).get('item') or {}]:
            if _identity(artist) == artist_id:
                if (str(artist.get('thumbnail') or '').startswith('/artist_artwork/')
                        and not str(details.get('thumbnail') or '').startswith('/artist_artwork/')):
                    continue
                for field in ('thumbnail','artwork_source_url','artwork_source_urls','artwork_cache_identity','artwork_cache_token','artwork_cache_status'):
                    if details.get(field): artist[field] = deepcopy(details[field])
        if artist_id==(snapshot.get('lead_artist') or {}).get('id') and 'related_artists' in details:
            snapshot['similar_artists']=[a for a in _unique(details['related_artists'] or []) if _identity(a)!=artist_id]
            snapshot['relationships_checked']=True

    def _artwork_updated(self, details):
        if not details.get('thumbnail'): return
        with self._condition:
            for key,current in list(self._snapshots.items()):
                matches = [a for a in [*current.get('artists',[]), *current.get('similar_artists',[])]
                           if self._same_artist(a,details) and a.get('thumbnail') != details['thumbnail']]
                if matches:
                    updated = deepcopy(current)
                    for artist in matches: self._merge_artist(updated, _identity(artist), details)
                    self._publish(key,updated,expected_inventory=current['inventory_id'])

    def _cached_portraits(self, snapshot, *, persisted=False):
        artists = [*snapshot.get('artists',[]), *snapshot.get('similar_artists',[])]
        records = load_catalog_artist_records(self._server, artist_names=[a.get('name','') for a in artists]) if persisted and getattr(self._server,'RECOMMENDATION_STORE_DB_PATH',None) else {}
        lookup = getattr(self._server,'_cache_lookup',None)
        for artist in artists:
            needs_related = _identity(artist)==(snapshot.get('lead_artist') or {}).get('id') and not snapshot.get('relationships_checked')
            if artist.get('thumbnail') and not needs_related: continue
            details = records.get('provider:'+_identity(artist).casefold()) or {}
            if details and self._same_artist(artist,details):
                verified = attach_persisted_artist_artwork(self._server,details)
                self._merge_artist(snapshot,_identity(artist),verified)
            if callable(lookup) and (not artist.get('thumbnail') or needs_related):
                for suffix in (':expanded:light',':expanded:full',':basic:light',':basic:full'):
                    cached = lookup(self._server.detail_result_cache,self._server.detail_result_cache_lock,'artist',_identity(artist)+suffix)
                    if cached and self._same_artist(artist,cached) and (cached.get('thumbnail') or needs_related and 'related_artists' in cached):
                        self._merge_artist(snapshot,_identity(artist),cached)
                        if not needs_related or snapshot.get('relationships_checked'): break

    def _search_server(self):
        return SearchServerAdapter(self._server)

    def _load(self, key):
        with self._condition:
            current = self._snapshots.get(key)
            if current is not None:
                self._snapshots.move_to_end(key)
                self._accessed[key] = time.time()
                return deepcopy(current)
        connection = None
        try:
            connection = open_recommendation_store_connection(self._server)
            row = connection.execute('SELECT payload_json FROM search_snapshots WHERE snapshot_key=?',(key,)).fetchone()
            snapshot = json.loads(row[0]) if row else None
        except Exception:
            snapshot = None
        finally:
            if connection is not None:
                connection.close()
        if snapshot and snapshot.get('schema') == 1:
            with self._condition:
                if key not in self._snapshots:
                    self._remember(key,snapshot)
                return deepcopy(self._snapshots[key])
        return None

    def _remember(self, key, snapshot):
        self._snapshots[key] = snapshot
        self._accessed[key] = time.time()
        self._snapshots.move_to_end(key)
        self._sizes[key] = retained_size(snapshot)
        while len(self._snapshots)>_MAX_ENTRIES or sum(self._sizes.values())>_MAX_BYTES:
            removed,_ = self._snapshots.popitem(last=False)
            self._sizes.pop(removed,None)
            self._accessed.pop(removed,None)

    def _publish(self, key, snapshot, *, expected_inventory=None, expected_fetched_at=None):
        with self._condition:
            previous = self._snapshots.get(key)
            if previous and previous['fetched_at']>snapshot['fetched_at']:
                return deepcopy(previous)
            if expected_inventory is not None and (not previous or previous['inventory_id']!=expected_inventory):
                return deepcopy(previous) if previous else None
            if expected_fetched_at is not None and (not previous or previous['fetched_at'] != expected_fetched_at):
                return deepcopy(previous) if previous else None
            snapshot = deepcopy(snapshot)
            if expected_inventory and previous:
                for artist in [*previous.get('artists',[]), *previous.get('similar_artists',[])]:
                    if str(artist.get('thumbnail') or '').startswith('/artist_artwork/'):
                        incoming = next((a for a in [*snapshot.get('artists',[]), *snapshot.get('similar_artists',[])]
                                         if _identity(a)==_identity(artist)),{})
                        if not str(incoming.get('thumbnail') or '').startswith('/artist_artwork/'):
                            self._merge_artist(snapshot,_identity(artist),artist)
            snapshot['revision'] = max(int(snapshot.get('revision') or 0),int((previous or {}).get('revision') or 0))+1
            if previous and previous['inventory_id']!=snapshot['inventory_id']:
                self._remember(key+'||'+previous['inventory_id'],previous)
            self._remember(key,snapshot)
            self._condition.notify_all()
        if _PERSIST_ADMISSION.acquire(blocking=False):
            try:
                future = _SEARCH_SNAPSHOT_PERSIST_EXECUTOR.submit(_persist,self._server,key,deepcopy(snapshot))
                future.add_done_callback(lambda _: _PERSIST_ADMISSION.release())
            except Exception:
                _PERSIST_ADMISSION.release()
        return deepcopy(snapshot)

    def _fetch(self, query, previous=None):
        server = self._search_server()
        deadline = time.monotonic()+_PROVIDER_SECONDS
        futures = {}
        def lookup(kind):
            token = _search_http_deadline.set(deadline)
            try:
                # limit=1 suppresses continuation fetching, not the first shelf.
                return self._server.ytmusic.search(query,filter=kind,limit=1)
            finally:
                _search_http_deadline.reset(token)
        url_match=re.search(r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})',query)
        if url_match:
            source_id=url_match[1]
            future=server.submit_search_upstream(lambda:self._server.ytmusic.get_watch_playlist(videoId=source_id),deadline=deadline)
            try:
                payload=future.result(timeout=max(.01,deadline-time.monotonic())) if future else {}
                entry=next(iter(payload.get('tracks') or []),payload.get('videoDetails') or {})
                entry={**entry,'videoId':source_id,'resultType':'song'}
                shelves={'songs':[entry],'videos':[],'artists':[],'top':[]}
                states={'songs':'complete','videos':'complete','artists':'complete','top':'complete'}
            except Exception as exc:
                if future:future.cancel()
                shelves=deepcopy((previous or {}).get('provider_shelves') or {})
                states={'songs':type(exc).__name__,'videos':'complete','artists':'complete','top':'complete'}
        else:
            shelves=None
        # Two refresh owners share four provider slots. Rolling two calls per
        # query prevents one refresh from rejecting every shelf of its neighbour.
        remaining = iter([] if url_match else [('songs','songs'),('artists','artists'),('videos','videos'),('top',None)])
        active = set()
        def submit_next():
            for name,kind in remaining:
                future = server.submit_search_upstream(lambda kind=kind:lookup(kind),deadline=deadline)
                futures[name] = future
                if future is not None:
                    active.add(future)
                    return
        submit_next()
        submit_next()
        while active and time.monotonic() < deadline:
            completed, _ = wait(active,timeout=max(0,deadline-time.monotonic()),return_when=FIRST_COMPLETED)
            if not completed:
                break
            active.difference_update(completed)
            for _ in completed:
                submit_next()
        for name,_ in remaining:
            futures[name] = None
        shelves = shelves if url_match else deepcopy((previous or {}).get('provider_shelves') or {})
        states = states if url_match else {}
        for name,future in futures.items():
            try:
                if future is None or not future.done() or future.cancelled():
                    raise TimeoutError('provider unavailable')
                shelves[name] = list(future.result() or [])
                states[name] = 'complete'
            except Exception as exc:
                if future is not None:
                    future.cancel()
                states[name] = type(exc).__name__
        top_items = normalize_provider_top(shelves.get('top') or [])
        tracks = []
        for kind in ('songs','videos'):
            for index,entry in enumerate(shelves.get(kind) or []):
                track = normalize_song_result(server,entry)
                if not track:
                    track={'id':f'provider:unavailable:{kind}:{index}',
                        'title':entry.get('title') or entry.get('name') or 'Unavailable recording',
                        'channel':self._server.extract_artist(entry),
                        'thumbnail':self._server.extract_thumbnail(entry),'playable':False}
                track.update(search_provider_kind=kind,search_provider_rank=index+1,
                             search_provider_query=search_query_key(query))
                tracks.append(track)
        # Top can expose an official video absent from either typed shelf.
        for index,entry in enumerate(top_items):
            if entry.get('resultType') in {'song','video'}:
                track = normalize_song_result(server,entry)
                if track:
                    track.update(search_provider_kind='top',search_provider_rank=index+1)
                    tracks.append(track)
        tracks = _unique(tracks)
        tracks,ranking = rank_provider_inventory(query,tracks,top_items)
        artists,albums,playlists = [],[],[]
        for entry in shelves.get('artists') or []:
            if entry.get('browseId') and (entry.get('artist') or entry.get('name') or entry.get('title')):
                artists.append({'id':entry['browseId'],
                    'name':entry.get('artist') or entry.get('name') or entry.get('title'),
                    'thumbnail':self._server.extract_thumbnail(entry),'provider':'ytmusic'})
        top_entity = None
        for entry in top_items:
            kind = entry.get('resultType')
            if kind in {'song','video'}:
                item=next((track for track in tracks if track['id']==entry.get('videoId')),None)
                if top_entity is None and item:
                    top_entity={'entity_type':'track','item':item}
            elif kind=='artist' and entry.get('browseId'):
                item={'id':entry['browseId'],'name':entry.get('artist') or entry.get('name') or entry.get('title'),
                      'thumbnail':self._server.extract_thumbnail(entry),'provider':'ytmusic'}
                artists.append(item)
                if top_entity is None:
                    top_entity={'entity_type':'artist','item':item}
            elif kind=='album' and entry.get('browseId'):
                item={'id':entry['browseId'],'title':entry.get('title'),'artist':self._server.extract_artist(entry),
                      'thumbnail':self._server.extract_thumbnail(entry),'provider':'ytmusic'}
                albums.append(item)
                if top_entity is None:
                    top_entity={'entity_type':'album','item':item}
            elif kind=='playlist' and entry.get('browseId'):
                playlists.append({'id':entry['browseId'], 'title':entry.get('title'),
                    'thumbnail':self._server.extract_thumbnail(entry),'provider':'ytmusic'})
        for track in tracks:
            if track.get('album_id'):
                albums.append({'id':track['album_id'],'title':track.get('album'),'artist':track.get('channel'),
                               'thumbnail':track.get('thumbnail'),'provider':'ytmusic'})
        artists,albums = _unique(artists),_unique(albums)
        # A unique exact artist identity outranks an unrelated provider video.
        # Preserve the provider decision when a song title is an exact tie.
        query_key = search_query_key(query).replace(' ', '')
        exact_artists = [a for a in artists if a.get('id') and query_key and
                         search_query_key(a.get('name') or '').replace(' ', '') == query_key]
        exact_track = any(search_query_key(track.get('title') or '').replace(' ', '') == query_key
                          for track in tracks)
        if len(exact_artists) == 1 and not exact_track:
            top_entity = {'entity_type':'artist','item':exact_artists[0]}
        # Reuse the existing detail owner's TTL-checked cache. A known portrait
        # should not disappear until another network enrichment completes.
        self._cached_portraits({'artists':artists})
        if top_entity and top_entity['entity_type']=='artist':
            top_entity['item']=next((a for a in artists if a['id']==_identity(top_entity['item'])),top_entity['item'])
        if top_entity is None and tracks:
            top_entity={'entity_type':'track','item':tracks[0]}
        lead = None
        containing = None
        if top_entity:
            item=top_entity['item']
            if top_entity['entity_type']=='artist':
                lead=item
            elif top_entity['entity_type']=='track':
                lead=next((a for a in artists if a['id']==item.get('artist_id')),None)
                containing=next((a for a in albums if a['id']==item.get('album_id')),None)
            else:
                containing=item
        fetched_at = time.time()
        retry_seconds = _EMPTY_FRESH_SECONDS if not any((tracks, artists, albums, playlists)) else _RETRY_SECONDS
        snapshot={'schema':1,'query':query,'tracks':tracks,'artists':artists,'albums':albums,'playlists':playlists,
                  'similar_artists':[],'provider_shelves':shelves,'provider_states':states,'ranking':ranking,
                  'top_result':top_entity,'lead_artist':lead,'containing_album':containing,
                  'fetched_at':fetched_at,'retry_after':fetched_at+retry_seconds,
                  'metadata_version':3,'metadata_state':'pending' if lead or any(not a.get('thumbnail') for a in artists)
                      or top_entity and top_entity['entity_type']=='track' and not containing else 'complete'}
        self._cached_portraits(snapshot)
        if snapshot.get('similar_artists'): self._cached_portraits(snapshot)
        if (not any(not a.get('thumbnail') for a in [*snapshot['artists'], *snapshot['similar_artists']])
                and (not lead or snapshot.get('relationships_checked'))
                and (not top_entity or top_entity['entity_type']!='track' or containing)):
            snapshot['metadata_state']='complete'
        if previous and (
            all(state != 'complete' for state in states.values())
            or (_empty_inventory(snapshot) and not _empty_inventory(previous))
        ):
            snapshot=deepcopy(previous)
            now = time.time()
            if _empty_inventory(snapshot):
                snapshot.update(fetched_at=now, retry_after=now+_EMPTY_FRESH_SECONDS)
            else:
                snapshot['retry_after'] = now+_RETRY_SECONDS
            snapshot['provider_states'] = states
            snapshot['latest_refresh_empty'] = True
            snapshot['latest_provider_counts'] = {
                name: len(rows) if states.get(name) == 'complete' else 0
                for name, rows in shelves.items()
            }
        snapshot['inventory_id']=hashlib.sha256(json.dumps(
            [search_query_key(query),[[_identity(r) for r in snapshot.get(s) or []] for s in _SURFACES]],ensure_ascii=False).encode()).hexdigest()[:16]
        return snapshot

    def _containing_album(self, track):
        anchor = dict(track)
        if not anchor.get('album'):
            resolver = getattr(self._server,'_build_track_details_payload',None)
            details = resolver(_identity(track)) if callable(resolver) else {}
            if str(details.get('video_id') or details.get('id') or '') != _identity(track):
                return None
            anchor.update(album=details.get('album_title') or details.get('album'), album_id=details.get('album_id'))
        album = search_canonical_album_for_track(anchor,server=self._search_server())
        if not album or not album.get('id'): return None
        resolver = getattr(self._server,'_build_album_details_payload',None)
        details = resolver(album['id']) if callable(resolver) else {}
        tracks = [t for t in details.get('tracks') or [] if _identity(t) and t.get('playable') is not False]
        if _identity(details)!=album['id'] or not tracks: return None
        # The existing detail owner persists this usable payload; the snapshot
        # keeps only its card metadata, not another copy of the full track list.
        return {**album, **{k:details[k] for k in ('title','artist','thumbnail','year') if details.get(k)},
                'track_count':len(tracks)}

    def _enrich_metadata(self, key, current):
        updated = deepcopy(current)
        self._cached_portraits(updated,persisted=True)
        if updated != current:
            self._publish(key,updated,expected_inventory=current['inventory_id'],expected_fetched_at=current['fetched_at'])
        lead = updated.get('lead_artist') or {}
        attempts = updated.setdefault('artist_metadata_attempts',{})
        candidates = [a for a in _unique([*updated.get('artists',[]), *updated.get('similar_artists',[])]) if a.get('id') and
                      (not a.get('thumbnail') or a['id']==lead.get('id') and not updated.get('relationships_checked'))
                      and int(attempts.get(a['id'],0)) < 3]
        candidates.sort(key=lambda a:(int(attempts.get(a['id'],0)),a['id']!=lead.get('id')))
        top = updated.get('top_result') or {}
        jobs = []
        if top.get('entity_type')=='track' and not updated.get('containing_album') and int(updated.get('album_metadata_attempts',0))<3:
            track = top.get('item') or {}
            jobs.append(('album',track,lambda:self._containing_album(track)))
        for artist in candidates[:6]:
            jobs.append(('artist',artist,lambda artist=artist:self._search_server().build_artist_details_payload(
                artist['id'],enrich_related=artist['id']==lead.get('id'),lightweight=True)))
        deadline = time.monotonic()+_PROVIDER_SECONDS
        futures = {}
        def bounded(fn):
            token = _search_http_deadline.set(deadline)
            try: return fn()
            finally: _search_http_deadline.reset(token)
        for kind,item,fn in jobs[:2]:
            future = self._search_server().submit_search_upstream(lambda fn=fn:bounded(fn),deadline=deadline)
            if future is not None: futures[future]=(kind,item)
        try:
            for future in as_completed(futures,timeout=max(.01,deadline-time.monotonic())):
                kind,item = futures[future]
                try: details = future.result() or {}
                except Exception: details = {}
                if kind=='artist':
                    artist_id = item['id']
                    attempts[artist_id] = int(attempts.get(artist_id,0))+1
                    # This request was made by exact provider ID; validate its name too.
                    details = {'id':artist_id,**details}
                    if self._same_artist(item,details):
                        self._merge_artist(updated,artist_id,details)
                        if artist_id==lead.get('id'):
                            self._cached_portraits(updated)
                else:
                    updated['album_metadata_attempts'] = int(updated.get('album_metadata_attempts',0))+1
                    if details.get('id'):
                        updated['containing_album'] = details
                        for track in [*updated.get('tracks',[]),(updated.get('top_result') or {}).get('item') or {}]:
                            if _identity(track)==_identity(item):
                                track.update(album_id=details['id'],album=details.get('title'))
                self._publish(key,updated,expected_inventory=current['inventory_id'],expected_fetched_at=current['fetched_at'])
        except TimeoutError:
            pass
        for future,(kind,item) in futures.items():
            if not future.done():
                future.cancel()
                if kind=='artist': attempts[item['id']]=int(attempts.get(item['id'],0))+1
                else: updated['album_metadata_attempts']=int(updated.get('album_metadata_attempts',0))+1
        missing = [a for a in _unique([*updated.get('artists',[]), *updated.get('similar_artists',[])]) if not a.get('thumbnail')]
        relation_missing = bool(lead and not updated.get('relationships_checked'))
        album_missing = top.get('entity_type')=='track' and not updated.get('containing_album')
        unattempted = any(not attempts.get(a['id']) for a in missing) or relation_missing and not attempts.get(lead.get('id')) or album_missing and not updated.get('album_metadata_attempts')
        updated['metadata_state'] = 'pending' if unattempted else 'unavailable' if missing or relation_missing or album_missing else 'complete'
        # Visiting the next artist batch is not a retry of a failed lookup.
        updated['metadata_attempts'] = int(current.get('metadata_attempts') or 0)+(0 if unattempted else 1)
        updated['metadata_retry_after'] = time.time()+_RETRY_SECONDS
        published = self._publish(key,updated,expected_inventory=current['inventory_id'],expected_fetched_at=current['fetched_at'])
        if published and published.get('fetched_at') == current['fetched_at']:
            cache_search_payload(tracks=updated['tracks'],artists=updated['artists'],albums=updated['albums'])
        return published

    def _schedule(self, key, snapshot, *, refresh=False):
        with self._condition:
            if key in self._pending or not _BACKGROUND_ADMISSION.acquire(blocking=False):
                return False
            self._pending.add(key)
        def finish():
            with self._condition:
                self._pending.discard(key)
            _BACKGROUND_ADMISSION.release()
        def submit(current, passes, blocked):
            try:
                _SEARCH_BACKGROUND.submit(run,current,passes,False,blocked)
            except Exception:
                finish()
        def run(current, passes, do_refresh, blocked):
            deferred = False
            try:
                if do_refresh:
                    fetched = self._fetch(snapshot['query'],snapshot)
                    current=self._publish(key,fetched,expected_inventory=snapshot['inventory_id'],expected_fetched_at=snapshot['fetched_at'])
                    if not current or current['fetched_at'] != fetched['fetched_at']:
                        return
                if not current or current.get('metadata_version') != 3:
                    return
                inventory = current and current.get('inventory_id')
                fetched_at = current and current.get('fetched_at')
                while current and passes < _METADATA_MAX_PASSES:
                    with self._condition:
                        latest = deepcopy(self._snapshots.get(key))
                        active = time.time()-self._accessed.get(key,0) < _MEMORY_IDLE_SECONDS
                    if not active or not latest or latest.get('inventory_id') != inventory or latest.get('fetched_at') != fetched_at:
                        return
                    current = latest
                    if current.get('metadata_state') == 'unavailable':
                        if int(current.get('metadata_attempts') or 0) >= 3:
                            return
                        delay = max(0, current.get('metadata_retry_after',0)-time.time())
                        if delay:
                            timer = threading.Timer(delay,submit,args=(current,passes,blocked))
                            timer.daemon = True
                            timer.start()
                            deferred = True
                            return
                        current['metadata_state'] = 'pending'
                    if current.get('metadata_state') != 'pending':
                        return
                    before = (dict(current.get('artist_metadata_attempts') or {}),
                              int(current.get('album_metadata_attempts') or 0))
                    current = self._enrich_metadata(key,current)
                    passes += 1
                    if not current:
                        return
                    if (dict(current.get('artist_metadata_attempts') or {}),
                        int(current.get('album_metadata_attempts') or 0)) == before:
                        if current.get('metadata_state') == 'pending' and blocked < 2:
                            timer = threading.Timer(_RETRY_SECONDS,submit,args=(current,passes,blocked+1))
                            timer.daemon = True
                            timer.start()
                            deferred = True
                        return
                    blocked = 0
            except Exception as exc:
                print(f'[EBB:search] event=background_failed error={type(exc).__name__}',flush=True)
            finally:
                if not deferred:
                    finish()
        try:
            _SEARCH_BACKGROUND.submit(run,snapshot,0,refresh,0)
        except Exception:
            with self._condition:
                self._pending.discard(key)
            _BACKGROUND_ADMISSION.release()
            return False
        return True

    def _response(self, req, snapshot, *, hit, started):
        limit=max(1,min(int(getattr(req,'limit',24) or 24),100))
        offset=max(0,int(getattr(req,'offset',0) or 0))
        surface=str(getattr(req,'result_type','') or '')
        result={'status':'success','model_version':'provider_search_v1','query_intent':'mixed',
                'top_result':deepcopy(snapshot.get('top_result')),'lead_artist':deepcopy(snapshot.get('lead_artist')),
                'containing_album':deepcopy(snapshot.get('containing_album')),'similar_artists':deepcopy(snapshot.get('similar_artists') or []),
                'artist_tracks':[],'artist_albums':[],'related_albums':[],'similar_tracks':[],'pagination':{}}
        inventory=snapshot['inventory_id']
        for name in _SURFACES:
            rows=snapshot.get(name) or []
            start=offset if surface==name else 0
            result[name]=deepcopy(rows[start:start+limit])
            next_offset=min(start+limit,len(rows))
            result['pagination'][name]={'total':len(rows),'offset':start,'next_offset':next_offset,
                'has_more':next_offset<len(rows),'cursor':f'v2:{inventory}:{next_offset}:{name}'}
        result['results']=result['tracks']
        states=snapshot.get('provider_states') or {}
        failed=[name for name,state in states.items() if state!='complete']
        artwork_pending=any(artist_artwork_is_pending(self._server,a) for a in [*snapshot.get('artists',[]),*snapshot.get('similar_artists',[])]
                            if not str(a.get('thumbnail') or '').startswith('/artist_artwork/'))
        pending=(['tracks'] if any(name != 'artists' for name in failed) or _snapshot_stale(snapshot,time.time()) else [])+(['artists'] if 'artists' in failed or snapshot.get('metadata_state')=='pending' or artwork_pending else [])
        result['diagnostics']={'ranking_backend':'provider_search_v1','search_snapshot_hit':hit,
            'search_snapshot_revision':snapshot['revision'],'inventory_id':inventory,
            'search_pending_surfaces':pending,'search_exhausted_surfaces':[],
            'search_failed_sources':failed,'provider_states':states,
            'provider_counts':snapshot.get('latest_provider_counts',
                {k:len(v) for k,v in snapshot.get('provider_shelves',{}).items()}),
            'search_refresh_retained':snapshot.get('latest_refresh_empty') is True,
            'inventory_count':len(snapshot.get('tracks') or []),'ranking':snapshot.get('ranking'),
            'results_age_seconds':round(time.time()-snapshot['fetched_at'],2),
            'results_stale':_snapshot_stale(snapshot,time.time()),
            'metadata_state':snapshot.get('metadata_state'),'request_ms':round((time.perf_counter()-started)*1000),
            'search_outcome':'results' if any(snapshot.get(s) for s in _SURFACES) else 'provider_unavailable' if failed else 'empty'}
        return result

    def _search_inventory(self, req):
        started=time.perf_counter()
        query=str(req.query or '').strip()
        if not query:
            return {'status':'success','tracks':[],'results':[],'artists':[],'albums':[],'playlists':[],
                    'diagnostics':{'ranking_backend':'provider_search_v1','search_pending_surfaces':[]}}
        key=_key(query)
        snapshot=self._load(key)
        cursor=str(getattr(req,'cursor','') or '')
        if cursor:
            match=re.fullmatch(r'v2:([a-f0-9]{16}):(\d+):(tracks|artists|albums|playlists)',cursor)
            if not match or int(match[2])!=int(getattr(req,'offset',0) or 0) or match[3]!=getattr(req,'result_type',''):
                return {'status':'success','pagination_expired':True}
            if not snapshot or snapshot['inventory_id']!=match[1]:
                with self._condition:
                    snapshot=deepcopy(self._snapshots.get(key+'||'+match[1]))
            if snapshot is None:
                return {'status':'success','pagination_expired':True}
            return self._response(req,snapshot,hit=True,started=started)
        since=int(getattr(req,'revision',0) or 0)
        if since:
            if snapshot:
                self._maybe_schedule(key,snapshot)
            deadline=time.monotonic()+min(max(int(getattr(req,'revision_wait_ms',0) or 0),0),3000)/1000
            with self._condition:
                while snapshot and snapshot['revision']<=since and time.monotonic()<deadline:
                    self._condition.wait(timeout=max(0,deadline-time.monotonic()))
                    snapshot=deepcopy(self._snapshots.get(key))
            if not snapshot or snapshot['revision']<=since:
                return {'status':'success','not_modified':True,'diagnostics':{
                    'search_snapshot_revision':since,'revision_wait_timed_out':True,'ranking_backend':'provider_search_v1'}}
        hit=snapshot is not None and not getattr(req,'force_refresh',False)
        if not hit:
            with self._condition:
                flight=self._inflight.get(key)
                owner=flight is None
                if owner:
                    flight=Future();self._inflight[key]=flight
            if owner:
                try:
                    snapshot=self._publish(key,self._fetch(query,snapshot))
                    flight.set_result(snapshot)
                except Exception as exc:
                    flight.set_exception(exc)
                    raise
                finally:
                    with self._condition:
                        self._inflight.pop(key,None)
            else:
                snapshot=flight.result(timeout=_PROVIDER_SECONDS+1)
        self._maybe_schedule(key,snapshot)
        return self._response(req,snapshot,hit=hit,started=started)

    def _maybe_schedule(self, key, snapshot):
        old_inventory = snapshot.get('metadata_version') != 3
        now=time.time()
        failed=any(s!='complete' for s in snapshot.get('provider_states',{}).values())
        retry_after = snapshot.get('retry_after',0)
        if _empty_inventory(snapshot):
            # Older persisted negative snapshots may carry the longer retry TTL.
            retry_after = min(retry_after, snapshot['fetched_at']+_EMPTY_FRESH_SECONDS)
        if old_inventory and failed and now < retry_after:
            return
        refresh=(old_inventory and not failed) or ((_snapshot_stale(snapshot,now) or failed or old_inventory) and now>=retry_after)
        retry_metadata=(snapshot.get('metadata_state')=='unavailable'
                        and int(snapshot.get('metadata_attempts') or 0)<3
                        and now>=snapshot.get('metadata_retry_after',0))
        if refresh or snapshot.get('metadata_state')=='pending' or retry_metadata:
            if retry_metadata:
                snapshot={**snapshot,'metadata_state':'pending'}
            self._schedule(key,snapshot,refresh=refresh)

    def search_albums(self, req):
        return self.search(req)

    def search_artists(self, req):
        return self.search(req)

    def search(self, req):
        started=time.perf_counter()
        trace=None
        if hasattr(self._server,'_trace_start'):
            trace=self._server._trace_start('search',user_scope_id=req.user_scope_id or 'guest',
                surface=req.surface or 'search',query=req.query or '')
        try:
            result=self._search_inventory(req)
            result['request_id']=(trace or {}).get('request_id') or uuid4().hex
            result.setdefault('diagnostics',{})['request_ms']=round((time.perf_counter()-started)*1000)
            if trace is not None:
                trace['candidate_counts']={s:len(result.get(s) or []) for s in _SURFACES}
                trace.setdefault('stage_ms',{})['search.total']=result['diagnostics']['request_ms']
                trace.setdefault('ranking_meta',{}).update(result['diagnostics'].get('ranking') or {})
                self._server._trace_finalize(trace,status='success')
                self._server._trace_log_request(trace,request_type='search',
                    user_scope_id=req.user_scope_id or 'guest',model_version='provider_search_v1')
            return result
        except Exception as exc:
            if trace is not None:
                self._server._trace_finalize(trace,status='failed',error=type(exc).__name__)
                self._server._trace_log_request(trace,request_type='search',
                    user_scope_id=req.user_scope_id or 'guest',model_version='provider_search_v1')
            raise


    def playlist_details(self, playlist_id: str) -> Dict[str, Any]:
        normalized_id = str(playlist_id or "").strip()
        if not normalized_id:
            return {"status": "success", "playlist": {}, "tracks": []}
        payload = self._search_server().search_upstream_call_with_retry(
            lambda: self._server.ytmusic.get_playlist(
                playlistId=normalized_id,
                limit=100,
            ),
            default={},
        )
        payload = dict(payload or {})
        tracks: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw_track in payload.get("tracks") or []:
            track = self._search_server().normalize_track(raw_track)
            if track is None:
                continue
            track_id = str(track.get("id") or "").strip()
            if not track_id or track_id in seen:
                continue
            seen.add(track_id)
            tracks.append(track)
        playlist = {
            "id": normalized_id,
            "name": payload.get("title") or "Playlist",
            "title": payload.get("title") or "Playlist",
            "subtitle": payload.get("description") or payload.get("author") or "",
            "thumbnail": self._server.extract_thumbnail(payload),
            "author": self._server.extract_artist(payload),
            "track_count": len(tracks),
            "provider": "ytmusic",
            "tracks": tracks,
        }
        cache_search_payload(tracks=tracks, artists=[], albums=[])
        return {"status": "success", "playlist": playlist, "tracks": tracks}

    def resolve_artist(self, req):
        started_at = time.perf_counter()
        query = self._search_server().trim_text(req.query)
        requested_artist_id = self._search_server().trim_text(
            getattr(req, "anchor_artist_id", "")
        )
        try:
            artists = search_artists_direct_cached(
                query,
                max(1, min(req.limit or 4, 4)),
                server=self._server,
            )
            error = ""
        except Exception as exc:
            artists = []
            error = str(exc)
        normalized_query = self._search_server().normalize_text(query)
        resolved = next(
            (
                artist
                for artist in artists
                if requested_artist_id
                and str(
                    artist.get("provider_artist_id")
                    or artist.get("id")
                    or ""
                ).strip()
                == requested_artist_id
            ),
            None,
        )
        if resolved is None:
            exact_matches = [
                artist
                for artist in artists
                if self._search_server().normalize_text(artist.get("name"))
                == normalized_query
            ]
            resolved = max(
                exact_matches,
                key=lambda artist: (
                    normalized_popularity(artist),
                    bool(str(artist.get("thumbnail") or "").strip()),
                ),
                default=artists[0] if artists else None,
            )
        return {
            "status": "success",
            "artist": resolved,
            "artists": artists,
            "diagnostics": {
                "ranking_backend": "canonical_artist_resolver_v1",
                "request_ms": int((time.perf_counter() - started_at) * 1000),
                "candidate_count": len(artists),
                "resolved": resolved is not None,
                "error": error,
            },
        }

    def suggest(self, req):
        server = self._server
        try:
            suggestion_timings = {}
            suggestion_items = semantic_search_suggestion_items(
                req,
                server=server,
                diagnostics_out=suggestion_timings,
            )
            results = [
                item.get("text") if isinstance(item, dict) else str(item)
                for item in suggestion_items
                if (item.get("text") if isinstance(item, dict) else str(item))
            ]
            return {
                "status": "success",
                "results": results[: max(1, min(req.limit or 5, 8))],
                "suggestions": suggestion_items[: max(1, min(req.limit or 5, 8))],
                "diagnostics": {
                    "ranking_backend": "canonical_suggestions_v1",
                    "warmup_scheduled": False,
                    "stage_timings_ms": suggestion_timings,
                },
            }
        except Exception:
            return {
                "status": "success",
                "results": [],
                "error_message": "Suggestions are temporarily unavailable.",
                "diagnostics": {"ranking_backend": "canonical_suggestions_v1"},
            }
