from __future__ import annotations

from typing import Any, Dict, List, Optional
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid


def assistant_db_connection(server: Any):
    connection = sqlite3.connect(server.ASSISTANT_MEMORY_DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def assistant_pgvector_enabled(server: Any):
    return (
        server.ASSISTANT_VECTOR_BACKEND == "pgvector"
        and server.psycopg is not None
        and bool(server.ASSISTANT_PGVECTOR_DSN)
    )


def assistant_pgvector_connection(server: Any):
    if not assistant_pgvector_enabled(server):
        return None
    connection = server.psycopg.connect(server.ASSISTANT_PGVECTOR_DSN)
    connection.autocommit = True
    return connection


def assistant_vector_literal(_server: Any, embedding: Optional[List[float]]):
    if not embedding:
        return None
    return "[" + ",".join(f"{float(value):.8f}" for value in embedding) + "]"


def assistant_init_memory_db(server: Any):
    with server.assistant_memory_lock:
        if assistant_pgvector_enabled(server):
            connection = assistant_pgvector_connection(server)
            if connection is not None:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
                        cursor.execute(
                            f"""
                            CREATE TABLE IF NOT EXISTS assistant_memory (
                                id TEXT PRIMARY KEY,
                                scope_id TEXT NOT NULL,
                                kind TEXT NOT NULL,
                                content TEXT NOT NULL,
                                metadata_json JSONB,
                                embedding VECTOR({server.ASSISTANT_EMBED_DIM}),
                                created_at DOUBLE PRECISION NOT NULL
                            )
                            """
                        )
                        cursor.execute(
                            "CREATE INDEX IF NOT EXISTS idx_assistant_memory_scope_time "
                            "ON assistant_memory(scope_id, created_at DESC)"
                        )
                    return
                except Exception:
                    pass
                finally:
                    connection.close()

        connection = assistant_db_connection(server)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_memory (
                    id TEXT PRIMARY KEY,
                    scope_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT,
                    embedding_json TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_assistant_memory_scope_time "
                "ON assistant_memory(scope_id, created_at DESC)"
            )
            connection.commit()
        finally:
            connection.close()


def assistant_safe_scope_id(_server: Any, scope_id: Optional[str]):
    cleaned = (scope_id or "guest").strip()
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "_", cleaned)
    return cleaned or "guest"


def assistant_now_timestamp(_server: Any):
    return time.time()


def assistant_default_session_title(_server: Any, message: Optional[str]):
    text = re.sub(r"\s+", " ", (message or "").strip())
    if not text:
        return "New chat"
    return text[:72].rstrip(" .,!?:;") or "New chat"


def assistant_preview_text(_server: Any, text: Optional[str], limit: int = 180):
    preview = re.sub(r"\s+", " ", (text or "").strip())
    if len(preview) <= limit:
        return preview
    return preview[: limit - 1].rstrip() + "..."


def assistant_init_session_db(server: Any):
    connection = assistant_db_connection(server)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS assistant_sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_message_preview TEXT,
                last_mode TEXT,
                archived_at REAL,
                pinned_at REAL
            )
            """
        )
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(assistant_sessions)").fetchall()
        }
        if "archived_at" not in existing_columns:
            connection.execute("ALTER TABLE assistant_sessions ADD COLUMN archived_at REAL")
        if "pinned_at" not in existing_columns:
            connection.execute("ALTER TABLE assistant_sessions ADD COLUMN pinned_at REAL")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_assistant_sessions_user_updated "
            "ON assistant_sessions(user_id, updated_at DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS assistant_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                payload_json TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_assistant_messages_session_time "
            "ON assistant_messages(session_id, created_at ASC)"
        )
        connection.commit()
    finally:
        connection.close()


def assistant_session_summary_from_row(_server: Any, row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "title": row["title"] or "New chat",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_message_preview": row["last_message_preview"] or "",
        "last_mode": row["last_mode"] or "",
        "archived_at": row["archived_at"],
        "pinned_at": row["pinned_at"],
    }


def assistant_list_sessions(server: Any, user_scope_id: str, include_archived: bool = False):
    scope_id = assistant_safe_scope_id(server, user_scope_id)
    connection = assistant_db_connection(server)
    try:
        query = (
            "SELECT id, user_id, title, created_at, updated_at, "
            "last_message_preview, last_mode, archived_at, pinned_at "
            "FROM assistant_sessions WHERE user_id = ?"
        )
        params: List[Any] = [scope_id]
        if not include_archived:
            query += " AND archived_at IS NULL"
        query += " ORDER BY pinned_at IS NULL ASC, pinned_at DESC, updated_at DESC"
        rows = connection.execute(query, params).fetchall()
        return [assistant_session_summary_from_row(server, row) for row in rows]
    finally:
        connection.close()


def assistant_get_session(server: Any, session_id: str, user_scope_id: str):
    scope_id = assistant_safe_scope_id(server, user_scope_id)
    connection = assistant_db_connection(server)
    try:
        row = connection.execute(
            """
            SELECT id, user_id, title, created_at, updated_at,
                   last_message_preview, last_mode, archived_at, pinned_at
            FROM assistant_sessions
            WHERE id = ? AND user_id = ?
            """,
            [session_id, scope_id],
        ).fetchone()
        return assistant_session_summary_from_row(server, row)
    finally:
        connection.close()


def assistant_create_session(
    server: Any,
    user_scope_id: str,
    *,
    title: Optional[str] = None,
    seed_message: Optional[str] = None,
):
    scope_id = assistant_safe_scope_id(server, user_scope_id)
    session_id = str(uuid.uuid4())
    now = assistant_now_timestamp(server)
    resolved_title = (title or "").strip() or assistant_default_session_title(server, seed_message)
    connection = assistant_db_connection(server)
    try:
        connection.execute(
            """
            INSERT INTO assistant_sessions (
                id, user_id, title, created_at, updated_at,
                last_message_preview, last_mode, archived_at, pinned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            [
                session_id,
                scope_id,
                resolved_title,
                now,
                now,
                assistant_preview_text(server, seed_message),
                "",
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return assistant_get_session(server, session_id, scope_id)


def assistant_touch_session(
    server: Any,
    session_id: str,
    user_scope_id: str,
    *,
    title: Optional[str] = None,
    last_message_preview: Optional[str] = None,
    last_mode: Optional[str] = None,
):
    scope_id = assistant_safe_scope_id(server, user_scope_id)
    updates = ["updated_at = ?"]
    params: List[Any] = [assistant_now_timestamp(server)]
    if title is not None:
        updates.append("title = ?")
        params.append(title.strip() or "New chat")
    if last_message_preview is not None:
        updates.append("last_message_preview = ?")
        params.append(assistant_preview_text(server, last_message_preview))
    if last_mode is not None:
        updates.append("last_mode = ?")
        params.append((last_mode or "").strip())
    params.extend([session_id, scope_id])

    connection = assistant_db_connection(server)
    try:
        connection.execute(
            f"""
            UPDATE assistant_sessions
            SET {", ".join(updates)}
            WHERE id = ? AND user_id = ?
            """,
            params,
        )
        connection.commit()
    finally:
        connection.close()


def assistant_store_session_message(
    server: Any,
    session_id: str,
    user_scope_id: str,
    *,
    role: str,
    content: str,
    payload: Optional[Dict[str, Any]] = None,
):
    scope_id = assistant_safe_scope_id(server, user_scope_id)
    connection = assistant_db_connection(server)
    try:
        connection.execute(
            """
            INSERT INTO assistant_messages (
                id, session_id, user_id, role, content, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                str(uuid.uuid4()),
                session_id,
                scope_id,
                role,
                (content or "").strip(),
                json.dumps(payload or {}, ensure_ascii=False),
                assistant_now_timestamp(server),
            ],
        )
        connection.commit()
    finally:
        connection.close()


def assistant_get_session_messages(server: Any, session_id: str, user_scope_id: str):
    scope_id = assistant_safe_scope_id(server, user_scope_id)
    connection = assistant_db_connection(server)
    try:
        rows = connection.execute(
            """
            SELECT id, session_id, user_id, role, content, payload_json, created_at
            FROM assistant_messages
            WHERE session_id = ? AND user_id = ?
            ORDER BY created_at ASC
            """,
            [session_id, scope_id],
        ).fetchall()
    finally:
        connection.close()

    messages = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        messages.append(
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "role": row["role"],
                "content": row["content"],
                "payload": payload,
                "created_at": row["created_at"],
            }
        )
    return messages


def assistant_get_session_detail(server: Any, session_id: str, user_scope_id: str):
    session = assistant_get_session(server, session_id, user_scope_id)
    if session is None:
        return None
    return {
        "session": session,
        "messages": assistant_get_session_messages(server, session_id, user_scope_id),
    }


def assistant_update_session(
    server: Any,
    session_id: str,
    user_scope_id: str,
    *,
    title: Optional[str] = None,
    archived: Optional[bool] = None,
    pinned: Optional[bool] = None,
):
    scope_id = assistant_safe_scope_id(server, user_scope_id)
    updates = ["updated_at = ?"]
    params: List[Any] = [assistant_now_timestamp(server)]
    if title is not None:
        updates.append("title = ?")
        params.append(title.strip() or "New chat")
    if archived is not None:
        updates.append("archived_at = ?")
        params.append(assistant_now_timestamp(server) if archived else None)
    if pinned is not None:
        updates.append("pinned_at = ?")
        params.append(assistant_now_timestamp(server) if pinned else None)
    params.extend([session_id, scope_id])

    connection = assistant_db_connection(server)
    try:
        connection.execute(
            f"""
            UPDATE assistant_sessions
            SET {", ".join(updates)}
            WHERE id = ? AND user_id = ?
            """,
            params,
        )
        connection.commit()
    finally:
        connection.close()
    return assistant_get_session(server, session_id, scope_id)


def assistant_delete_session(server: Any, session_id: str, user_scope_id: str):
    scope_id = assistant_safe_scope_id(server, user_scope_id)
    connection = assistant_db_connection(server)
    try:
        connection.execute(
            "DELETE FROM assistant_messages WHERE session_id = ? AND user_id = ?",
            [session_id, scope_id],
        )
        cursor = connection.execute(
            "DELETE FROM assistant_sessions WHERE id = ? AND user_id = ?",
            [session_id, scope_id],
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def assistant_embed_texts(server: Any, texts: List[str]):
    payload_texts = [text.strip() for text in texts if text and text.strip()]
    if not payload_texts:
        return []

    def local_embed(value: str):
        vector = [0.0] * server.ASSISTANT_EMBED_DIM
        tokens = server._query_tokens(value)
        if not tokens:
            tokens = [
                token
                for token in re.split(r"[^a-z0-9]+", server._normalize_text(value))
                if token
            ]
        if not tokens:
            return vector
        for index, token in enumerate(tokens):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            slot = int.from_bytes(digest[:4], "big") % server.ASSISTANT_EMBED_DIM
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            weight = 1.15 if index < 6 else 1.0
            vector[slot] += sign * weight
        norm = math.sqrt(sum(item * item for item in vector))
        if norm > 0:
            vector = [item / norm for item in vector]
        return vector

    if server.ASSISTANT_EMBED_BACKEND == "ollama":
        now = time.time()
        with server.assistant_embed_ollama_backoff_lock:
            backoff_until = server.assistant_embed_ollama_backoff_until
        if now >= backoff_until:
            try:
                response = server.ollama_http.post(
                    f"{server.OLLAMA_BASE_URL}/embed",
                    headers=server._ollama_headers(),
                    json={
                        "model": server.OLLAMA_EMBED_MODEL,
                        "input": payload_texts,
                    },
                    timeout=(
                        min(
                            server.OLLAMA_CONNECT_TIMEOUT_SECONDS,
                            server.ASSISTANT_EMBED_OLLAMA_TIMEOUT_SECONDS,
                        ),
                        server.ASSISTANT_EMBED_OLLAMA_TIMEOUT_SECONDS,
                    ),
                )
                response.raise_for_status()
                data = response.json()
                embeddings = data.get("embeddings")
                if isinstance(embeddings, list) and embeddings:
                    if len(embeddings) >= len(payload_texts):
                        with server.assistant_embed_ollama_backoff_lock:
                            server.assistant_embed_ollama_backoff_until = 0.0
                        return embeddings[: len(payload_texts)]
                embedding = data.get("embedding")
                if isinstance(embedding, list) and embedding and len(payload_texts) == 1:
                    with server.assistant_embed_ollama_backoff_lock:
                        server.assistant_embed_ollama_backoff_until = 0.0
                    return [embedding]
            except Exception:
                pass
            with server.assistant_embed_ollama_backoff_lock:
                server.assistant_embed_ollama_backoff_until = (
                    time.time() + server.ASSISTANT_EMBED_OLLAMA_COOLDOWN_SECONDS
                )
    return [local_embed(text) for text in payload_texts]


def assistant_cosine_similarity(_server: Any, a: List[float], b: List[float]):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for left, right in zip(a, b):
        dot += left * right
        norm_a += left * left
        norm_b += right * right
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


def assistant_store_memory(
    server: Any,
    scope_id: str,
    kind: str,
    content: str,
    metadata: Optional[Dict[str, Any]] = None,
):
    text = (content or "").strip()
    if not text:
        return

    embeddings = assistant_embed_texts(server, [text])
    embedding = embeddings[0] if embeddings else None
    if assistant_pgvector_enabled(server):
        connection = assistant_pgvector_connection(server)
        if connection is not None:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO assistant_memory(id, scope_id, kind, content, metadata_json, embedding, created_at)
                        VALUES (%s, %s, %s, %s, %s::jsonb, %s::vector, %s)
                        """,
                        (
                            str(uuid.uuid4()),
                            assistant_safe_scope_id(server, scope_id),
                            kind,
                            text,
                            json.dumps(metadata or {}, ensure_ascii=False),
                            assistant_vector_literal(server, embedding),
                            time.time(),
                        ),
                    )
                return
            except Exception:
                pass
            finally:
                connection.close()

    connection = assistant_db_connection(server)
    try:
        connection.execute(
            """
            INSERT INTO assistant_memory(id, scope_id, kind, content, metadata_json, embedding_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                assistant_safe_scope_id(server, scope_id),
                kind,
                text,
                json.dumps(metadata or {}, ensure_ascii=False),
                json.dumps(embedding) if embedding is not None else None,
                time.time(),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def assistant_query_memory(server: Any, scope_id: str, queries: List[str], limit: int = 6):
    cleaned_queries = [query.strip() for query in queries if query and query.strip()]
    if not cleaned_queries:
        return []

    query_embeddings = assistant_embed_texts(server, cleaned_queries[:3])
    if not query_embeddings:
        return []

    if assistant_pgvector_enabled(server):
        connection = assistant_pgvector_connection(server)
        if connection is not None:
            try:
                rows = []
                with connection.cursor() as cursor:
                    for query_embedding in query_embeddings[:3]:
                        cursor.execute(
                            """
                            SELECT id, kind, content, metadata_json::text, created_at,
                                   (1 - (embedding <=> %s::vector)) AS score
                            FROM assistant_memory
                            WHERE scope_id = %s
                            ORDER BY embedding <=> %s::vector ASC, created_at DESC
                            LIMIT %s
                            """,
                            (
                                assistant_vector_literal(server, query_embedding),
                                assistant_safe_scope_id(server, scope_id),
                                assistant_vector_literal(server, query_embedding),
                                max(limit * 3, 12),
                            ),
                        )
                        rows.extend(cursor.fetchall())
                deduped = {}
                for row in rows:
                    row_id = row[0]
                    current = deduped.get(row_id)
                    candidate = {
                        "id": row[0],
                        "kind": row[1],
                        "content": row[2],
                        "metadata": json.loads(row[3] or "{}"),
                        "score": float(row[5] or 0),
                        "created_at": row[4],
                    }
                    if current is None or candidate["score"] > current["score"]:
                        deduped[row_id] = candidate
                ranked = sorted(
                    deduped.values(),
                    key=lambda item: (item["score"], item["created_at"]),
                    reverse=True,
                )
                return ranked[:limit]
            except Exception:
                pass
            finally:
                connection.close()

    connection = assistant_db_connection(server)
    try:
        rows = connection.execute(
            """
            SELECT id, kind, content, metadata_json, embedding_json, created_at
            FROM assistant_memory
            WHERE scope_id = ?
            ORDER BY created_at DESC
            LIMIT 200
            """,
            (assistant_safe_scope_id(server, scope_id),),
        ).fetchall()
    finally:
        connection.close()

    scored = []
    for row in rows:
        raw_embedding = row["embedding_json"]
        if not raw_embedding:
            continue
        try:
            embedding = json.loads(raw_embedding)
        except Exception:
            continue
        best_score = 0.0
        for query_embedding in query_embeddings:
            best_score = max(
                best_score,
                assistant_cosine_similarity(server, query_embedding, embedding),
            )
        scored.append(
            {
                "id": row["id"],
                "kind": row["kind"],
                "content": row["content"],
                "metadata": json.loads(row["metadata_json"] or "{}"),
                "score": best_score,
                "created_at": row["created_at"],
            }
        )

    scored.sort(key=lambda item: (item["score"], item["created_at"]), reverse=True)
    return scored[:limit]
