"""Lossless candidate/radio storage encoding; logical inventory schemas stay unchanged."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import zlib
from typing import Any

COMPACT_WRITES_ENV = "NEATIE_COMPACT_INVENTORY_WRITES"
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_LOGICAL_BYTES = 128 * 1024 * 1024
_MIN_JSON_BYTES = 16 * 1024
_MARKER = "_neatie_inventory_storage"
_VERSION = "zlib-json-v1"
_REF_VERSION = "zlib-json-refs-v2"
_POOL_VERSION = "zlib-json-pools-v3"
_FIELDS = {_MARKER, "kind", "json_bytes", "json_sha256", "data"}
_REF_FIELDS = _FIELDS | {"storage_json_bytes", "storage_json_sha256"}
_KINDS = {"candidate", "radio"}
_TRACKS_REF = "_neatie_canonical_tracks_from_tracks"
_CARDS_REF = "_neatie_selected_card_indices"


def _same_json(left: Any, right: Any) -> bool:
    """Strict JSON equality: bool/int and differently represented floats differ."""
    if type(left) is not type(right):
        return False
    if left is right:
        return True
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_json(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_json(a, b) for a, b in zip(left, right))
    return repr(left) == repr(right) if isinstance(left, float) else left == right


def _deduplicate_inventory(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    """Shallow-copy only containers that lose a proven identical field."""
    if kind == "candidate":
        pools = payload.get("pools")
        if not isinstance(pools, dict):
            return payload
        replacements = {}
        for pool, candidates in pools.items():
            if not isinstance(candidates, list):
                continue
            revised = []
            changed = False
            for candidate in candidates:
                item = candidate.get("item") if isinstance(candidate, dict) else None
                if isinstance(item, dict) and _TRACKS_REF in item:
                    return payload  # Existing literal marker never becomes a reference.
                if (isinstance(item, dict) and isinstance(item.get("tracks"), list)
                        and isinstance(item.get("canonical_tracks"), list)
                        and _same_json(item["tracks"], item["canonical_tracks"])):
                    reduced = {key: value for key, value in item.items() if key != "canonical_tracks"}
                    reduced[_TRACKS_REF] = True
                    candidate = {**candidate, "item": reduced}
                    changed = True
                revised.append(candidate)
            if changed:
                replacements[pool] = revised
        return {**payload, "pools": {**pools, **replacements}} if replacements else payload
    cards, reserve = payload.get("cards"), payload.get("reservoir_cards")
    if (_CARDS_REF in payload or not isinstance(cards, list) or not isinstance(reserve, list)
            or not all(isinstance(card, dict) for card in [*cards, *reserve])):
        return payload
    positions: dict[str, list[int]] = {}
    for index, card in enumerate(reserve):
        positions.setdefault(str(card.get("id") or ""), []).append(index)
    selected = []
    for card in cards:
        match = next((index for index in positions.get(str(card.get("id") or ""), [])
                      if _same_json(card, reserve[index])), None)
        if match is None:
            return payload
        selected.append(match)
    reduced = {key: value for key, value in payload.items() if key != "cards"}
    reduced[_CARDS_REF] = selected
    return reduced


def _json_fragments(value: Any):
    # C-encode individual list items instead of either materializing the whole
    # inventory string or using Python's per-leaf encoder for every track field.
    # Canonical punctuation/order stays byte-identical to json.dumps.
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def fragments(item):
        if isinstance(item, dict) and all(isinstance(key, str) for key in item):
            yield "{"
            for index, key in enumerate(sorted(item)):
                if index:
                    yield ","
                yield encoder.encode(key)
                yield ":"
                yield from fragments(item[key])
            yield "}"
        elif isinstance(item, list):
            yield "["
            for index, member in enumerate(item):
                if index:
                    yield ","
                yield encoder.encode(member)
            yield "]"
        else:
            yield encoder.encode(item)

    for fragment in fragments(value):
        yield fragment.encode('utf-8')


def _json_digest(value: Any, maximum: int) -> tuple[int, str]:
    size, digest = 0, hashlib.sha256()
    for data in _json_fragments(value):
        size += len(data)
        if size > maximum:
            raise InventoryStorageError("inventory_storage_expanded_size_exceeded")
        digest.update(data)
    return size, digest.hexdigest()


def _intern_pools(payload: dict[str, Any]) -> dict[str, Any]:
    """One exact item body; membership wrappers keep their scores/provenance.

    Identity alone is insufficient: editions, source proofs and historical
    metadata can differ for the same album. Only byte-equivalent bodies share.
    """
    bodies, positions, memberships, pools = [], {}, {}, {}
    for name, candidates in payload.get('pools', {}).items():
        if not isinstance(candidates, list):
            pools[name] = candidates
            continue
        revised, indices = [], []
        for candidate in candidates:
            item = candidate.get('item') if isinstance(candidate, dict) else None
            index = None
            if isinstance(item, dict):
                signature = _json_digest(item, MAX_LOGICAL_BYTES)
                index = positions.get(signature)
                if index is None:
                    index = len(bodies)
                    bodies.append(item)
                    positions[signature] = index
                elif not _same_json(item, bodies[index]):
                    raise InventoryStorageError('inventory_item_digest_collision')
                candidate = {key: value for key, value in candidate.items() if key != 'item'}
            revised.append(candidate)
            indices.append(index)
        pools[name], memberships[name] = revised, indices
    return {'inventory': {**payload, 'pools': pools}, 'items': bodies,
            'memberships': memberships}


def _restore_pools(wrapper: dict[str, Any], size: int, checksum: str, *, copy_references: bool) -> dict[str, Any]:
    if set(wrapper) != {'inventory', 'items', 'memberships', 'track_refs'}:
        raise InventoryStorageError('invalid_inventory_pool_reference')
    payload, bodies, memberships = wrapper['inventory'], wrapper['items'], wrapper['memberships']
    if (not isinstance(payload, dict) or _MARKER in payload
            or not isinstance(payload.get('pools'), dict) or not isinstance(bodies, list)
            or not all(isinstance(body, dict) for body in bodies)
            or not isinstance(memberships, dict) or type(wrapper['track_refs']) is not bool
            or set(memberships) != {k for k,v in payload['pools'].items() if isinstance(v,list)}):
        raise InventoryStorageError('invalid_inventory_pool_reference')
    # Bound amplification before serializing the referenced logical tree.
    lengths = [_json_digest(body, MAX_JSON_BYTES)[0] for body in bodies]
    projected = _json_digest(payload, MAX_JSON_BYTES)[0]
    references = []
    for name, indices in memberships.items():
        candidates = payload['pools'][name]
        if not isinstance(indices, list) or len(indices) != len(candidates):
            raise InventoryStorageError('invalid_inventory_pool_reference')
        for candidate, index in zip(candidates, indices):
            if index is None:
                continue
            if (type(index) is not int or not 0 <= index < len(bodies)
                    or not isinstance(candidate, dict) or 'item' in candidate):
                raise InventoryStorageError('invalid_inventory_pool_reference')
            projected += 7 + lengths[index] + bool(candidate)  # "item": plus comma
            if projected > size:
                raise InventoryStorageError('inventory_storage_expanded_size_exceeded')
            candidate['item'] = dict(bodies[index])
            references.append(candidate)
    if wrapper['track_refs']:
        payload = _expand_inventory(payload, 'candidate', size, checksum, projected,
                                    copy_references=copy_references)
    elif _json_digest(payload, size) != (size, checksum):
        raise InventoryStorageError('invalid_inventory_storage_logical_checksum')
    # Historical callers may mutate an occurrence: sharing is storage-only.
    if copy_references:
        for candidate in references:
            candidate['item'] = copy.deepcopy(candidate['item'])
    return payload


def _expand_inventory(payload: dict[str, Any], kind: str, size: int, checksum: str, storage_size: int,
                      *, copy_references: bool = True) -> dict[str, Any]:
    copies = []
    projected_size = storage_size
    def json_size(value: Any) -> int:
        return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if kind == "candidate":
        if not isinstance(payload.get("pools"), dict):
            raise InventoryStorageError("invalid_inventory_storage_reference")
        for candidates in payload["pools"].values():
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                item = candidate.get("item") if isinstance(candidate, dict) else None
                if not isinstance(item, dict) or _TRACKS_REF not in item:
                    continue
                if item[_TRACKS_REF] is not True or "canonical_tracks" in item or not isinstance(item.get("tracks"), list):
                    raise InventoryStorageError("invalid_inventory_storage_reference")
                projected_size += json_size("canonical_tracks") + json_size(item["tracks"]) - json_size(_TRACKS_REF) - 4
                if projected_size > size:
                    raise InventoryStorageError("inventory_storage_expanded_size_exceeded")
                item.pop(_TRACKS_REF)
                item["canonical_tracks"] = item["tracks"]
                copies.append((item, "canonical_tracks"))
    else:
        indices, reserve = payload.get(_CARDS_REF), payload.get("reservoir_cards")
        if ("cards" in payload or not isinstance(indices, list) or not isinstance(reserve, list)
                or any(type(index) is not int or not 0 <= index < len(reserve) for index in indices)):
            raise InventoryStorageError("invalid_inventory_storage_reference")
        lengths = {index: json_size(reserve[index]) for index in set(indices)}
        cards_size = 2 + sum(lengths[index] for index in indices) + max(len(indices) - 1, 0)
        projected_size += json_size("cards") + cards_size - json_size(_CARDS_REF) - json_size(indices)
        payload.pop(_CARDS_REF)
        payload["cards"] = [reserve[index] for index in indices]
        copies.append((payload, "cards"))
    # Exact projected byte size bounds expansion before fast canonical serialization
    # or copying. Repeated indices cannot amplify a small stored object unchecked.
    if projected_size != size:
        raise InventoryStorageError("inventory_storage_expanded_size_exceeded")
    logical_size, logical_checksum = _json_digest(payload, size)
    if logical_size != size or logical_checksum != checksum:
        raise InventoryStorageError("invalid_inventory_storage_logical_checksum")
    if copy_references:
        for container, key in copies:
            container[key] = ([copy.deepcopy(card) for card in container[key]]
                              if key == "cards" else copy.deepcopy(container[key]))
    return payload


class InventoryStorageError(ValueError):
    """Stored inventory encoding is invalid; never substitute partial inventory."""


def encode_inventory_payload(
    payload: dict[str, Any], kind: str, *, enabled: bool | None = None
) -> dict[str, Any]:
    """Encode future writes only when enabled and demonstrably smaller.

    Disabled, small and reserved-field payloads remain literal. Enabled writes
    exceeding the explicit bounds fail instead of silently writing huge JSON.
    Existing records are never converted by this function.
    """
    if kind not in _KINDS:
        raise ValueError("invalid_inventory_kind")
    if enabled is None:
        enabled = os.environ.get(COMPACT_WRITES_ENV, "").strip().lower() == "true"
    if not enabled or _MARKER in payload:
        return payload
    compact = _deduplicate_inventory(payload, kind)
    logical_stats = None
    pool_references = kind == 'candidate' and isinstance(payload.get('pools'), dict)
    if pool_references:
        logical_stats = _json_digest(payload, MAX_LOGICAL_BYTES)
        wrapper = _intern_pools(compact)
        wrapper['track_refs'] = compact is not payload
        compact = wrapper
    elif compact is not payload:
        logical_stats = _json_digest(payload, MAX_JSON_BYTES)
    compressor = zlib.compressobj(level=1)
    digest = hashlib.sha256()
    size = 0
    packed_parts: list[bytes] = []
    buffer = bytearray()
    for data in _json_fragments(compact):
        size += len(data)
        if size > MAX_JSON_BYTES:
            raise InventoryStorageError('inventory_storage_size_exceeded')
        digest.update(data)
        buffer.extend(data)
        if len(buffer) >= 65536:
            packed_parts.append(compressor.compress(buffer))
            buffer.clear()
    logical_size = logical_stats[0] if logical_stats else size
    if logical_size < _MIN_JSON_BYTES:
        return payload
    packed_parts.append(compressor.compress(buffer))
    packed_parts.append(compressor.flush())
    packed = b"".join(packed_parts)
    # Base64 plus bounded envelope overhead must save at least ten percent.
    if 4 * ((len(packed) + 2) // 3) + 512 > logical_size * 0.9:
        if logical_size > MAX_JSON_BYTES:
            raise InventoryStorageError('inventory_storage_size_exceeded')
        return payload
    stored = {
        _MARKER: _POOL_VERSION if pool_references else _REF_VERSION if logical_stats else _VERSION,
        "kind": kind,
        "json_bytes": logical_size,
        "json_sha256": logical_stats[1] if logical_stats else digest.hexdigest(),
        "data": base64.b64encode(packed).decode("ascii"),
    }
    if logical_stats:
        stored.update(storage_json_bytes=size, storage_json_sha256=digest.hexdigest())
    return stored


def decode_inventory_payload(payload: Any, expected_kind: str, *, copy_references: bool = True) -> Any:
    """Validate/expand envelopes; maintenance may borrow a shared read-only view.

    Normal callers retain independent mutable occurrences. Disabling copies
    never skips bounds, reference checks or either logical/storage checksum.
    """
    if expected_kind not in _KINDS:
        raise ValueError("invalid_inventory_kind")
    if not isinstance(payload, dict) or _MARKER not in payload:
        return payload
    # Legacy logical inventories may already contain a literal reserved field.
    # The encoder never wraps them, and their model version identifies them.
    if (
        "inventory_version" in payload and isinstance(payload.get("pools"), dict)
    ) or (
        "radio_inventory_version" in payload and isinstance(payload.get("cards"), list)
    ):
        return payload
    pool_references = payload.get(_MARKER) == _POOL_VERSION
    references = payload.get(_MARKER) in {_REF_VERSION, _POOL_VERSION}
    if set(payload) != (_REF_FIELDS if references else _FIELDS) or payload.get(_MARKER) not in {_VERSION, _REF_VERSION, _POOL_VERSION}:
        raise InventoryStorageError("invalid_inventory_storage_version_or_envelope")
    size = payload["json_bytes"]
    checksum = payload["json_sha256"]
    data = payload["data"]
    storage_size = payload.get("storage_json_bytes", size)
    storage_checksum = payload.get("storage_json_sha256", checksum)
    if (
        payload["kind"] != expected_kind
        or type(size) is not int
        or not 0 < size <= (MAX_LOGICAL_BYTES if pool_references else MAX_JSON_BYTES)
        or (pool_references and expected_kind != 'candidate')
        or not isinstance(checksum, str)
        or len(checksum) != 64
        or not isinstance(data, str)
        or len(data) > 4 * ((MAX_JSON_BYTES + 2) // 3)
        or type(storage_size) is not int
        or not 0 < storage_size <= MAX_JSON_BYTES
        or not isinstance(storage_checksum, str)
        or len(storage_checksum) != 64
    ):
        raise InventoryStorageError("invalid_inventory_storage_metadata")
    try:
        packed = base64.b64decode(data, validate=True)
        inflater = zlib.decompressobj()
        raw = inflater.decompress(packed, storage_size + 1)
    except (ValueError, TypeError, zlib.error):
        raise InventoryStorageError("invalid_inventory_storage_data") from None
    if (
        len(raw) != storage_size
        or not inflater.eof
        or inflater.unused_data
        or inflater.unconsumed_tail
        or hashlib.sha256(raw).hexdigest() != storage_checksum
    ):
        raise InventoryStorageError("invalid_inventory_storage_length_or_checksum")
    try:
        logical = json.loads(raw)
    except (ValueError, UnicodeError):
        raise InventoryStorageError("invalid_inventory_storage_json") from None
    if not isinstance(logical, dict) or _MARKER in logical:
        raise InventoryStorageError("invalid_inventory_storage_logical_payload")
    # The parsed tree owns its strings; decompressed bytes and inflater buffers
    # are no longer needed while references and their checksum are expanded.
    del raw, packed, inflater
    if pool_references:
        return _restore_pools(logical, size, checksum, copy_references=copy_references)
    return _expand_inventory(logical, expected_kind, size, checksum, storage_size,
                             copy_references=copy_references) if references else logical
