"""Authoritative conversation storage and cross-session retrieval assembly.

Source messages live in the business database and backups. Full-text/vector indexes
and sync ledgers live only in ``derived.db`` and can be rebuilt. Local FTS5 remains
available without cloud embeddings; authorized hybrid retrieval uses a separate index.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, replace

from agentmaker import ConversationSearch, Scope, SqliteSessionStore
from agentmaker.retrieval import SqliteBookkeeping, SyncIndexSync
from agentmaker.retrieval.backends import Fts5KeywordIndex, build_sqlite_hybrid

from ...platform.ai.retrieval import build_openai_embedder
from ...platform.database import derived_db_path

_APP_SCOPE = "careerdesk"
_KEYWORD_TABLE = "conversation_keyword_items"
_HYBRID_KEYWORD_TABLE = "conversation_hybrid_keyword_items"
_HYBRID_VECTOR_TABLE = "conversation_hybrid_vector_items"
_KEYWORD_BOOKKEEPING = "conversation_keyword_bookkeeping"
_HYBRID_BOOKKEEPING = "conversation_hybrid_bookkeeping"
_INDEX_METADATA_COLUMNS = ("role", "created_at")


@dataclass(frozen=True)
class _IndexItem:
    id: str
    content: str
    metadata: dict


def _source_scopes(db_path: str, *, user_id: str) -> list[Scope]:
    """Enumerate one user's exact conversation footprint, or empty before setup."""
    with sqlite3.connect(db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_messages'",
        ).fetchone()
        if exists is None:
            return []
        rows = conn.execute(
            "SELECT DISTINCT base, sc_user, sc_agent, sc_session, sc_app "
            "FROM session_messages WHERE sc_user = ? AND sc_app = ?",
            (user_id, _APP_SCOPE),
        ).fetchall()
    return [
        Scope(
            base=base or None,
            user=user or None,
            agent=agent or None,
            session=session or None,
            app=app or None,
        )
        for base, user, agent, session, app in rows
    ]


def _index_items(store: SqliteSessionStore, scope: Scope) -> list[_IndexItem]:
    items: list[_IndexItem] = []
    for message in store.load(scope=scope):
        message_id = message.metadata.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            continue
        content = message.content
        if isinstance(content, list):
            # AgentMaker converts multimodal content into safe text placeholders. Retrieval
            # needs text context only and never returns historical image bytes or local paths.
            from agentmaker.runtime.sessions import content_text

            content = content_text(content)
        items.append(_IndexItem(
            id=message_id,
            content=f"{message.role}: {content}",
            metadata={
                "role": message.role,
                "created_at": message.timestamp.isoformat(),
            },
        ))
    return items


def _reconcile_user_index(
    store: SqliteSessionStore,
    sync: SyncIndexSync,
    *,
    db_path: str,
    user_id: str,
) -> None:
    """Rebuild only when source/index sets drift to avoid repeated embedding calls."""
    source_scopes = _source_scopes(db_path, user_id=user_id)
    source_index_scopes: set[Scope] = set()
    for scope in source_scopes:
        index_scope = replace(scope, base="conversation")
        source_index_scopes.add(index_scope)
        items = _index_items(store, scope)
        source_ids = {item.id for item in items}
        if (
            sync.tracked_ids(scope=index_scope) != source_ids
            or sync.pending(scope=index_scope)
        ):
            sync.reconcile(items, scope=index_scope)

    owned = Scope(base="conversation", user=user_id, app=_APP_SCOPE)
    for stale_scope in sync.exact_scopes(scope=owned) - source_index_scopes:
        sync.reconcile([], scope=stale_scope)


def _table_exists(db_path: str, table: str) -> bool:
    if not os.path.exists(db_path):
        return False
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?",
            (table,),
        ).fetchone() is not None


def _vector_dimension(db_path: str, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?",
            (table,),
        ).fetchone()
    match = re.search(r"embedding float\[(\d+)\]", row[0] if row and row[0] else "")
    if match is None:
        raise RuntimeError("conversation vector index has an unreadable dimension")
    return int(match.group(1))


def _purge_derived_user_rows(index_path: str, *, user_id: str, ids: list[str]) -> None:
    """Clear both retrieval modes without creating absent derived tables."""
    if not ids or not os.path.exists(index_path):
        return
    scope = Scope(base="conversation", user=user_id, app=_APP_SCOPE)
    for table in (_KEYWORD_TABLE, _HYBRID_KEYWORD_TABLE):
        if not _table_exists(index_path, table):
            continue
        index = Fts5KeywordIndex(
            index_path,
            table=table,
            metadata_columns=_INDEX_METADATA_COLUMNS,
        )
        try:
            index.delete(ids, scope=scope)
        finally:
            index.close()

    if _table_exists(index_path, _HYBRID_VECTOR_TABLE):
        from agentmaker.retrieval.backends.sqlite import SqliteVecStore

        vectors = SqliteVecStore(
            dim=_vector_dimension(index_path, _HYBRID_VECTOR_TABLE),
            db_path=index_path,
            table=_HYBRID_VECTOR_TABLE,
            metadata_columns=_INDEX_METADATA_COLUMNS,
        )
        try:
            vectors.delete(ids, scope=scope)
        finally:
            vectors.close()

    with sqlite3.connect(index_path) as conn:
        for table in (_KEYWORD_BOOKKEEPING, _HYBRID_BOOKKEEPING):
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (table,),
            ).fetchone() is not None:
                conn.execute(
                    f"DELETE FROM {table} WHERE base = ? AND sc_user = ? AND sc_app = ?",
                    ("conversation", user_id, _APP_SCOPE),
                )
        conn.commit()


def clear_conversation_history(db_path: str, *, user_id: str) -> int:
    """Delete the user's source messages and search indexes; return message count."""
    with sqlite3.connect(db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_messages'",
        ).fetchone()
        if exists is None:
            return 0
        rows = conn.execute(
            "SELECT metadata FROM session_messages WHERE sc_user = ? AND sc_app = ?",
            (user_id, _APP_SCOPE),
        ).fetchall()
    ids: list[str] = []
    for (metadata_text,) in rows:
        try:
            message_id = json.loads(metadata_text).get("message_id")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(message_id, str) and message_id:
            ids.append(message_id)

    _purge_derived_user_rows(derived_db_path(db_path), user_id=user_id, ids=ids)
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM session_messages WHERE sc_user = ? AND sc_app = ?",
            (user_id, _APP_SCOPE),
        )
        conn.commit()
        return cursor.rowcount


def build_conversation_memory(
    db_path: str,
    *,
    embedding_enabled: bool,
    user_id: str | None = None,
    resource_closers: list | None = None,
):
    """Build durable conversation storage and always-available cross-session retrieval.

    ``embedding_enabled`` is explicit network authorization; an API key alone is not
    consent. FTS5 is used when unauthorized, offline, or keyless. With ``user_id``, a
    cheap set comparison rebuilds from authoritative messages only when indexes drift.
    """
    store = SqliteSessionStore(db_path)
    index_path = derived_db_path(db_path)
    semantic = embedding_enabled and bool(os.environ.get("OPENAI_API_KEY"))
    retriever = None
    bookkeeping = None
    try:
        if semantic:
            retriever = build_sqlite_hybrid(
                build_openai_embedder(),
                db_path=index_path,
                vec_table=_HYBRID_VECTOR_TABLE,
                kw_table=_HYBRID_KEYWORD_TABLE,
                metadata_columns=_INDEX_METADATA_COLUMNS,
            )
            bookkeeping = SqliteBookkeeping(index_path, table=_HYBRID_BOOKKEEPING)
        else:
            retriever = Fts5KeywordIndex(
                index_path,
                table=_KEYWORD_TABLE,
                metadata_columns=_INDEX_METADATA_COLUMNS,
            )
            bookkeeping = SqliteBookkeeping(index_path, table=_KEYWORD_BOOKKEEPING)
        sync = SyncIndexSync(retriever, bookkeeping=bookkeeping)
        conversation = ConversationSearch(store, retriever, index_sync=sync)
        if user_id is not None:
            _reconcile_user_index(store, sync, db_path=db_path, user_id=user_id)
    except Exception:
        store.close()
        if bookkeeping is not None:
            bookkeeping.close()
        if retriever is not None:
            retriever.close()
        raise
    if resource_closers is not None:
        def close() -> None:
            try:
                conversation.close()
            finally:
                retriever.close()

        resource_closers.append(close)
    return conversation, conversation
