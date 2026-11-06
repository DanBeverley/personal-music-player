"""Lossless AAC chunk indexing for the single-track files produced by FFmpeg.

AVFoundation reads an MP4 chunk as a unit. FFmpeg's audio-only remux can group
about 1 MiB (a minute of AAC) into one chunk. Change only stsc/stco, retaining
every sample, timestamp, edit and encoded byte. Unsupported layouts fail closed.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

_CONTAINERS = {b'moov', b'trak', b'mdia', b'minf', b'stbl'}
_MAX_INDEX = 8 * 1024 * 1024


def _atoms(data):
    offset = 0
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError('mp4_atom_truncated')
        size, kind = struct.unpack_from('>I4s', data, offset)
        header = 8
        if size == 1:
            if offset + 16 > len(data):
                raise ValueError('mp4_atom_truncated')
            size = struct.unpack_from('>Q', data, offset + 8)[0]
            header = 16
        if size < header or offset + size > len(data):
            raise ValueError('mp4_atom_bounds')
        yield kind, data[offset + header:offset + size]
        offset += size


def _box(kind, data):
    return struct.pack('>I4s', len(data) + 8, kind) + data


def aac_startup_extent(data: bytes, total: int, *, limit: int = 262144):
    """Return (required bytes, index complete) for two indexed AAC chunks.

    Only inspect a bounded prefix. An incomplete/unsupported/oversized index
    never proves readiness; callers may still serve the valid stored file.
    """
    offset = 0
    try:
        while offset + 8 <= len(data):
            size, kind = struct.unpack_from('>I4s', data, offset)
            header = 8
            if size == 1:
                if offset + 16 > len(data):
                    return min(offset + 16, limit, total), False
                size, header = struct.unpack_from('>Q', data, offset + 8)[0], 16
            if size < header or offset + size > total:
                return len(data), False
            if kind == b'moov':
                end = offset + size
                if end > limit:
                    return min(limit, total), False
                if end > len(data):
                    return end, False
                offsets = []
                def inspect(payload):
                    for box, body in _atoms(payload):
                        if box in _CONTAINERS:
                            inspect(body)
                        elif box in {b'stco', b'co64'}:
                            width, fmt = (4, 'I') if box == b'stco' else (8, 'Q')
                            if len(body) < 8:
                                raise ValueError('mp4_offsets_invalid')
                            count = struct.unpack_from('>I', body, 4)[0]
                            if not count or len(body) != 8 + count * width or offsets:
                                raise ValueError('mp4_offsets_invalid')
                            offsets.extend(struct.unpack_from('>' + fmt, body, 8 + i * width)[0]
                                           for i in range(min(count, 3)))
                            if count <= 2:
                                offsets.append(total)
                inspect(data[offset + header:end])
                if not offsets or offsets[0] < end or any(a >= b for a, b in zip(offsets, offsets[1:])):
                    return len(data), False
                required = offsets[min(2, len(offsets) - 1)]
                return min(required, limit, total), required <= limit and required <= total
            if kind not in {b'ftyp', b'free'}:
                return len(data), False
            offset += size
        return min(offset + 8, limit, total), False
    except (ValueError, struct.error, RecursionError):
        return len(data), False


def rechunk_aac(path: str) -> bool:
    """Reindex a fast-start, contiguous AAC file in place, outside live reads.

    Memory is bounded by the index, not the audio body. A false result denotes
    another audio codec; malformed/fragmented/encrypted layouts are rejected.
    The caller still performs its normal complete decode/duration validation.
    """
    size = os.path.getsize(path)
    top, moov, mdat = [], None, None
    with open(path, 'rb') as source:
        offset = 0
        while offset < size:
            header = source.read(8)
            if len(header) != 8:
                raise ValueError('mp4_header_truncated')
            length, kind = struct.unpack('>I4s', header)
            header_size = 8
            if length == 1:
                extended = source.read(8)
                if len(extended) != 8:
                    raise ValueError('mp4_header_truncated')
                length, header_size = struct.unpack('>Q', extended)[0], 16
            if length == 0:
                length = size - offset
            if length < header_size or offset + length > size:
                raise ValueError('mp4_header_bounds')
            if kind not in {b'ftyp', b'free', b'moov', b'mdat'}:
                raise ValueError('mp4_layout_unsupported')
            if kind == b'moov':
                if moov is not None or length > _MAX_INDEX:
                    raise ValueError('mp4_index_bounds')
                moov = source.read(length - header_size)
            if kind == b'mdat':
                if mdat is not None:
                    raise ValueError('mp4_multiple_media_bodies')
                mdat = (offset + header_size, length - header_size)
            top.append((offset, length, kind))
            offset += length
            source.seek(offset)
    if moov is None or mdat is None:
        raise ValueError('mp4_index_missing')
    tables, tracks = {}, 0

    def inspect(data):
        nonlocal tracks
        for kind, payload in _atoms(data):
            if kind == b'trak':
                tracks += 1
            if kind in {b'mvex', b'saio', b'senc', b'sinf'}:
                raise ValueError('mp4_layout_unsupported')
            if kind in {b'mdhd', b'hdlr', b'stsd', b'stsz', b'stts', b'stsc', b'stco', b'co64'}:
                if kind in tables:
                    raise ValueError('mp4_duplicate_table')
                tables[kind] = payload
            if kind in _CONTAINERS:
                inspect(payload)

    inspect(moov)
    if tracks != 1 or tables.get(b'hdlr', b'')[8:12] != b'soun':
        raise ValueError('mp4_single_audio_required')
    description = tables.get(b'stsd', b'')
    if len(description) < 16 or struct.unpack_from('>I', description, 4)[0] != 1:
        raise ValueError('mp4_description_invalid')
    if description[12:16] != b'mp4a':
        return False

    def table(kind, width):
        payload = tables.get(kind, b'')
        if len(payload) < 8 or payload[:4] != b'\0' * 4:
            raise ValueError('mp4_table_invalid')
        count = struct.unpack_from('>I', payload, 4)[0]
        if not count or len(payload) != 8 + count * width:
            raise ValueError('mp4_table_bounds')
        return payload, count

    mdhd = tables.get(b'mdhd', b'')
    position = 12 if mdhd[:1] == b'\0' else 20 if mdhd[:1] == b'\1' else -1
    if position < 0 or len(mdhd) < position + 4:
        raise ValueError('mp4_timescale_invalid')
    timescale = struct.unpack_from('>I', mdhd, position)[0]
    sizes = tables.get(b'stsz', b'')
    if len(sizes) < 12:
        raise ValueError('mp4_sample_sizes_invalid')
    constant, count = struct.unpack_from('>II', sizes, 4)
    if not 0 < count <= 500_000 or len(sizes) != 12 + (0 if constant else count * 4):
        raise ValueError('mp4_sample_sizes_invalid')
    sample_sizes = [constant] * count if constant else list(struct.unpack_from(f'>{count}I', sizes, 12))
    if not timescale or not all(sample_sizes) or sum(sample_sizes) != mdat[1]:
        raise ValueError('mp4_media_size_invalid')
    timing, timing_count = table(b'stts', 8)
    durations = []
    for i in range(timing_count):
        samples, delta = struct.unpack_from('>II', timing, 8 + i * 8)
        if not samples or not delta or len(durations) + samples > count:
            raise ValueError('mp4_sample_timing_invalid')
        durations.extend([delta] * samples)
    if len(durations) != count:
        raise ValueError('mp4_sample_timing_invalid')
    offset_kind = b'co64' if b'co64' in tables else b'stco'
    if b'co64' in tables and b'stco' in tables:
        raise ValueError('mp4_offset_tables_conflict')
    offsets_table, chunk_count = table(offset_kind, 8 if offset_kind == b'co64' else 4)
    fmt = 'Q' if offset_kind == b'co64' else 'I'
    offsets = struct.unpack_from(f'>{chunk_count}{fmt}', offsets_table, 8)
    mapping, mapping_count = table(b'stsc', 12)
    entries = [struct.unpack_from('>III', mapping, 8 + i * 12) for i in range(mapping_count)]
    if (entries[0][0] != 1 or any(not n or description != 1 or first > chunk_count
            for first, n, description in entries) or any(a[0] >= b[0] for a, b in zip(entries, entries[1:]))):
        raise ValueError('mp4_chunk_mapping_invalid')
    index, sample, byte_offset = 0, 0, mdat[0]
    for chunk, offset in enumerate(offsets, 1):
        if index + 1 < len(entries) and entries[index + 1][0] == chunk:
            index += 1
        n = entries[index][1]
        if offset != byte_offset or sample + n > count:
            raise ValueError('mp4_noncontiguous_chunks')
        byte_offset += sum(sample_sizes[sample:sample + n])
        sample += n
    if sample != count:
        raise ValueError('mp4_chunk_mapping_invalid')

    groups, starts, ticks, n, byte_offset = [], [], 0, 0, 0
    for sample_size, delta in zip(sample_sizes, durations):
        if n and ticks + delta > timescale:
            groups.append(n)
            ticks, n = 0, 0
        if not n:
            starts.append(byte_offset)
        ticks, n, byte_offset = ticks + delta, n + 1, byte_offset + sample_size
    groups.append(n)
    entries = [(i, n, 1) for i, n in enumerate(groups, 1) if i == 1 or n != groups[i - 2]]
    mapping = b'\0' * 4 + struct.pack('>I', len(entries)) + b''.join(struct.pack('>III', *e) for e in entries)

    def rewrite(data, shift):
        result = []
        for kind, payload in _atoms(data):
            if kind == b'stsc':
                payload = mapping
            elif kind == offset_kind:
                payload = b'\0' * 4 + struct.pack('>I', len(starts)) + b''.join(
                    struct.pack('>' + fmt, mdat[0] + shift + offset) for offset in starts)
            elif kind in _CONTAINERS:
                payload = rewrite(payload, shift)
            result.append(_box(kind, payload))
        return b''.join(result)

    moov_entry = next(item for item in top if item[2] == b'moov')
    if moov_entry[0] >= mdat[0]:
        raise ValueError('mp4_faststart_required')
    initial = _box(b'moov', rewrite(moov, 0))
    replacement = _box(b'moov', rewrite(moov, len(initial) - moov_entry[1]))
    if len(replacement) > _MAX_INDEX:
        raise ValueError('mp4_index_bounds')
    target = Path(path + '.rechunk')
    created = False
    try:
        with open(path, 'rb') as source, target.open('xb') as output:
            created = True
            for offset, length, kind in top:
                if kind == b'moov':
                    output.write(replacement)
                    continue
                source.seek(offset)
                remaining = length
                while remaining:
                    block = source.read(min(remaining, 65536))
                    if not block:
                        raise ValueError('mp4_body_truncated')
                    output.write(block)
                    remaining -= len(block)
        if target.stat().st_size != size + len(replacement) - moov_entry[1]:
            raise ValueError('mp4_output_size_invalid')
        os.replace(target, path)
        return True
    finally:
        if created:
            target.unlink(missing_ok=True)
