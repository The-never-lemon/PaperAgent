from __future__ import annotations

import copy
import json
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from posixpath import normpath
from typing import Any

from src.models.sessions import THREAD_MAX_EVENTS, SessionError, SessionRecord, utc_now
from src.utils import get_logger
from src.utils.readable_id import create_readable_id

from .base import JsonObject, SessionRepository


logger = get_logger(__name__)


class SessionStoreBackend:
    """基于 SQLite 与文件系统的会话存储后端。"""

    def __init__(self, storage_root: Path | str):
        """初始化持久化后端，并确保基础目录与表结构存在。"""

        self.storage_root = Path(storage_root)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.storage_root / "session_store.db"
        self.sessions_dir = self.storage_root / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def has_sessions(self) -> bool:
        """判断当前数据库中是否已经存在会话记录。"""

        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(1) AS count FROM session").fetchone()
        return bool(row and int(row["count"]) > 0)

    def create_session(
        self,
        session_id: str,
        title: str,
        created_at: str,
        updated_at: str,
        user_id: str,
        workspace_scope: JsonObject | None,
        metadata: JsonObject | None = None,
        status: str = "created",
    ) -> None:
        """创建一条新的会话主记录。"""

        payload = dict(metadata or {})
        payload.setdefault("schema_version", 1)
        payload["workspace_scope"] = workspace_scope
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO session (
                    id, user_id, title, status, created_at, updated_at, last_message_at, summary, metadata, run_started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    user_id,
                    title,
                    status,
                    created_at,
                    updated_at,
                    None,
                    "",
                    self._dump_json(payload),
                    None,
                ),
            )
        self.ensure_session_layout(session_id)

    def get_session(self, session_id: str) -> JsonObject | None:
        """读取单条会话主记录，并把 JSON 字段还原为普通对象。"""

        with self._connection() as connection:
            row = connection.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_session(row)

    def list_sessions(self) -> list[JsonObject]:
        """按更新时间倒序返回全部会话摘要原始数据。"""

        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM session ORDER BY updated_at DESC").fetchall()
        return [self._row_to_session(row) for row in rows]

    def delete_session(self, session_id: str) -> None:
        """删除指定会话的数据库记录和文件系统目录。"""

        with self._connection() as connection:
            connection.execute("DELETE FROM session WHERE id = ?", (session_id,))
        session_dir = self.sessions_dir / session_id
        if session_dir.exists():
            shutil.rmtree(session_dir)

    def update_workspace_scope(self, session_id: str, workspace_scope: JsonObject | None, updated_at: str) -> None:
        """更新会话工作区范围信息，并同步刷新更新时间。"""

        record = self.get_session(session_id)
        if record is None:
            return
        metadata = dict(record.get("metadata") or {})
        metadata["workspace_scope"] = workspace_scope
        with self._connection() as connection:
            connection.execute(
                "UPDATE session SET metadata = ?, updated_at = ? WHERE id = ?",
                (self._dump_json(metadata), updated_at, session_id),
            )

    def update_run_started_at(self, session_id: str, run_started_at: str | None, updated_at: str) -> None:
        """更新当前会话的运行中时间戳。"""

        with self._connection() as connection:
            connection.execute(
                "UPDATE session SET run_started_at = ?, updated_at = ? WHERE id = ?",
                (run_started_at, updated_at, session_id),
            )

    def update_status(self, session_id: str, status: str, updated_at: str) -> None:
        """更新会话状态，并把更新时间一并写回数据库。"""

        with self._connection() as connection:
            connection.execute(
                "UPDATE session SET status = ?, updated_at = ? WHERE id = ?",
                (status, updated_at, session_id),
            )

    def update_active_run_id(self, session_id: str, run_id: str | None, updated_at: str) -> None:
        """更新会话 metadata 里的 active_run_id 字段。

        中文说明：
        前端刷新页面时需要知道「当前这个会话有没有还在跑的 run」以及「去哪个
        地址接回实时流」。这个方法把 run_id 写进 session 的 metadata 列；
        run_id 传 None 表示把字段从 metadata 里删掉。
        """

        record = self.get_session(session_id)
        if record is None:
            return
        metadata = dict(record.get("metadata") or {})
        if run_id is None:
            metadata.pop("active_run_id", None)
        else:
            metadata["active_run_id"] = run_id
        with self._connection() as connection:
            connection.execute(
                "UPDATE session SET metadata = ?, updated_at = ? WHERE id = ?",
                (self._dump_json(metadata), updated_at, session_id),
            )

    def reset_stale_runs(self, now: str) -> tuple[int, int]:
        """启动时把所有「正在运行」状态的会话置成已中断。

        中文说明：
        后端进程被强杀后，数据库里可能有 status='running' 的残留记录，
        或者 run_started_at 还没被清空的记录。这个方法把两种情况统一修成
        interrupted，清空 run_started_at 和 metadata 里的 active_run_id，
        让用户可以继续对话。

        Returns:
            (修复的 running 会话数, 清理的孤儿时间戳数) 二元组。
        """

        with self._connection() as connection:
            # 第一步：把所有残留的 active_run_id 从 metadata 里清掉。
            # metadata 是 JSON 字符串，不能纯 SQL 更新，所以逐行读出来改完写回。
            all_sessions = connection.execute("SELECT id, metadata FROM session").fetchall()
            for row in all_sessions:
                meta = self._load_json(row["metadata"])
                if "active_run_id" in meta:
                    meta.pop("active_run_id", None)
                    connection.execute(
                        "UPDATE session SET metadata = ? WHERE id = ?",
                        (self._dump_json(meta), row["id"]),
                    )
            # 第二步：除了清 metadata，还要把 status 和 run_started_at 两个字段修好。
            cursor_running = connection.execute(
                "UPDATE session SET status = ?, run_started_at = NULL, updated_at = ? "
                "WHERE status = 'running'",
                ("interrupted", now),
            )
            running_count = cursor_running.rowcount
            cursor_orphan = connection.execute(
                "UPDATE session SET run_started_at = NULL, updated_at = ? "
                "WHERE run_started_at IS NOT NULL AND status != 'running'",
                (now,),
            )
            orphan_count = cursor_orphan.rowcount
        return running_count, orphan_count

    def purge_stream_events(self) -> int:
        """清掉历史上全部流式 token 事件（delta / reasoning_delta）。

        中文说明：
        改造前每一个 reasoning_delta / delta 都会往 session_event 表里插一条记录。
        改造后这些流式 token 不再落库，但历史数据里堆积的旧事件还在。
        启动时跑一次清理，把数据库瘦身。返回被删掉的记录条数。
        """

        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM session_event WHERE event_type IN ('delta', 'reasoning_delta')"
            )
            return cursor.rowcount

    def update_title(self, session_id: str, title: str, updated_at: str) -> None:
        """更新会话标题，通常用于首次用户输入后的自动命名。"""

        with self._connection() as connection:
            connection.execute(
                "UPDATE session SET title = ?, updated_at = ? WHERE id = ?",
                (title, updated_at, session_id),
            )

    def append_message(
        self,
        session_id: str,
        message_id: str,
        role: str,
        content: str,
        created_at: str,
        parent_id: str | None = None,
        metadata: JsonObject | None = None,
    ) -> None:
        """向消息表追加一条会话消息，并维护会话摘要字段。"""

        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO session_message (id, session_id, role, content, created_at, parent_id, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    role,
                    content,
                    created_at,
                    parent_id,
                    self._dump_json(metadata or {}),
                ),
            )
            connection.execute(
                """
                UPDATE session
                SET updated_at = ?, last_message_at = ?, summary = ?
                WHERE id = ?
                """,
                (created_at, created_at, content[:120], session_id),
            )

    def list_messages(self, session_id: str) -> list[JsonObject]:
        """按时间顺序读取某个会话下的全部消息。"""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, role, content, created_at, parent_id, metadata
                FROM session_message
                WHERE session_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (session_id,),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def append_event(
        self,
        session_id: str,
        event_type: str,
        content: str,
        created_at: str,
        metadata: JsonObject | None = None,
        event_id: str | None = None,
    ) -> JsonObject:
        """追加单条过程事件，并同步写入会话目录下的 `events.jsonl`。"""

        normalized_event_id = event_id or uuid.uuid4().hex
        payload = metadata or {}
        with self._connection() as connection:
            seq_row = connection.execute(
                "SELECT COALESCE(MAX(seq_no), 0) + 1 AS next_seq FROM session_event WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq_no = int(seq_row["next_seq"]) if seq_row else 1
            connection.execute(
                """
                INSERT INTO session_event (id, session_id, event_type, content, created_at, seq_no, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_event_id,
                    session_id,
                    event_type,
                    content,
                    created_at,
                    seq_no,
                    self._dump_json(payload),
                ),
            )
        event_record = {
            "id": normalized_event_id,
            "session_id": session_id,
            "event_type": event_type,
            "content": content,
            "created_at": created_at,
            "seq_no": seq_no,
            "metadata": payload,
        }
        self._append_event_jsonl(session_id, event_record)
        return event_record

    def list_events(self, session_id: str) -> list[JsonObject]:
        """按序号顺序读取指定会话的过程事件记录，最多只带最近 THREAD_MAX_EVENTS 条。

        中文说明：
        一次长调研会积累非常多的过程事件，而这个方法在每次打开会话、刷新页面
        恢复线程时都会被调用（thread 接口靠它重放历史）。不加限制的话，历史
        事件太多时一次查询就要搬几万条记录，长会话加载会卡死。所以这里只重放
        最近 THREAD_MAX_EVENTS（2000）条，更早的过程痕迹不影响阅读对话正文
        （正文来自消息表，不依赖过程事件）。
        """

        with self._connection() as connection:
            # 中文注释：内层子查询先按 seq_no 从大到小取"最近的 2000 条"，
            # 外层再把这批记录翻回按 seq_no 从小到大，保证调用方拿到的事件
            # 仍然是从旧到新的正确顺序。
            rows = connection.execute(
                """
                SELECT id, event_type, content, created_at, seq_no, metadata
                FROM (
                    SELECT id, event_type, content, created_at, seq_no, metadata
                    FROM session_event
                    WHERE session_id = ?
                    ORDER BY seq_no DESC
                    LIMIT ?
                )
                ORDER BY seq_no ASC
                """,
                (session_id, THREAD_MAX_EVENTS),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def append_artifact_record(
        self,
        session_id: str,
        artifact_type: str,
        name: str,
        path: str,
        size: int,
        created_at: str,
        metadata: JsonObject | None = None,
        artifact_id: str | None = None,
    ) -> JsonObject:
        """向产物表追加一条产物登记记录。"""

        normalized_artifact_id = artifact_id or uuid.uuid4().hex
        payload = metadata or {}
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO session_artifact (id, session_id, artifact_type, name, path, size, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_artifact_id,
                    session_id,
                    artifact_type,
                    name,
                    path,
                    size,
                    created_at,
                    self._dump_json(payload),
                ),
            )
        return {
            "id": normalized_artifact_id,
            "artifact_type": artifact_type,
            "name": name,
            "path": path,
            "size": size,
            "created_at": created_at,
            "metadata": payload,
        }

    def list_artifacts(self, session_id: str) -> list[JsonObject]:
        """读取会话关联的全部产物记录。"""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, artifact_type, name, path, size, created_at, metadata
                FROM session_artifact
                WHERE session_id = ?
                ORDER BY created_at ASC, rowid ASC
                """,
                (session_id,),
            ).fetchall()
        return [self._row_to_artifact(row) for row in rows]

    def ensure_session_layout(self, session_id: str) -> Path:
        """确保会话目录及其子目录存在，并返回会话根目录。"""

        session_dir = self.sessions_dir / session_id
        (session_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        (session_dir / "exports").mkdir(parents=True, exist_ok=True)
        (session_dir / "snapshots").mkdir(parents=True, exist_ok=True)
        events_file = session_dir / "events.jsonl"
        if not events_file.exists():
            events_file.touch()
        return session_dir

    def write_artifact_file(
        self,
        session_id: str,
        relative_path: str,
        content: str | bytes,
        *,
        encoding: str = "utf-8",
    ) -> Path:
        """把产物内容写入会话目录，并返回最终文件路径。"""

        session_dir = self.ensure_session_layout(session_id)
        normalized_relative_path = normpath(relative_path).replace("\\", "/").lstrip("/")
        if normalized_relative_path in {"", "."} or normalized_relative_path.startswith("../"):
            raise ValueError(f"invalid artifact path: {relative_path}")
        target_path = session_dir / Path(normalized_relative_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target_path.write_bytes(content)
        else:
            target_path.write_text(content, encoding=encoding)
        return target_path

    def _initialize_schema(self) -> None:
        """初始化 SQLite 表结构与必要索引。"""

        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS session (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_message_at TEXT,
                    summary TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    run_started_at TEXT
                );

                CREATE TABLE IF NOT EXISTS session_event (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    seq_no INTEGER NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(session_id, seq_no),
                    FOREIGN KEY(session_id) REFERENCES session(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS session_message (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    parent_id TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(session_id) REFERENCES session(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS session_artifact (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(session_id) REFERENCES session(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_session_updated_at ON session(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_session_user_id ON session(user_id);
                CREATE INDEX IF NOT EXISTS idx_session_event_session_seq ON session_event(session_id, seq_no);
                CREATE INDEX IF NOT EXISTS idx_session_message_session_created ON session_message(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_session_artifact_session_created ON session_artifact(session_id, created_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        """创建一条开启外键约束与行字典访问的 SQLite 连接。"""

        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        # 中文注释：把"等锁"的耐心从默认 5 秒放宽到 10 秒。多个后台 run 同时
        # 写库时偶尔要排队，默认 5 秒很容易等不及就抛"database is locked"错误，
        # 一次锁超时甚至能废掉整次调研。多等一会儿，绝大多数排队都能自然拿到锁。
        # 注意顺序：这条要放在下面切换 WAL 模式之前——数据库第一次切到 WAL
        # 需要短暂拿到独占锁，先把等锁耐心设好，切换这一步自己才不会等锁失败。
        connection.execute("PRAGMA busy_timeout = 10000")
        # 中文注释：开启 WAL 模式，解决"读和写互相挡路"的问题。SQLite 默认的
        # 日志模式下，一个写事务会把整个数据库锁住，这期间连读历史事件都得排队。
        # 改成 WAL 后，写的内容先记到旁边的 WAL 文件里，读和写可以同时进行；
        # 多个会话一起写时也会自动排队，不会互相报错。
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self):
        """提供会自动关闭的 SQLite 连接上下文。"""

        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _append_event_jsonl(self, session_id: str, event_record: JsonObject) -> None:
        """把结构化事件同步追加写入磁盘 JSONL 文件。"""

        session_dir = self.ensure_session_layout(session_id)
        events_file = session_dir / "events.jsonl"
        with events_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event_record, ensure_ascii=False) + "\n")

    def _row_to_session(self, row: sqlite3.Row) -> JsonObject:
        """把 `session` 表记录转换成更易消费的字典结构。"""

        metadata = self._load_json(row["metadata"])
        return {
            "key": row["id"],
            "user_id": row["user_id"],
            "title": row["title"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_message_at": row["last_message_at"],
            "summary": row["summary"],
            "metadata": metadata,
            "workspace_scope": metadata.get("workspace_scope"),
            "run_started_at": row["run_started_at"],
        }

    def _row_to_message(self, row: sqlite3.Row) -> JsonObject:
        """把消息表记录还原成线程视图兼容的消息对象。"""

        metadata = self._load_json(row["metadata"])
        return {
            "id": row["id"],
            "role": row["role"],
            "content": row["content"],
            "created_at": row["created_at"],
            "parent_id": row["parent_id"],
            **metadata,
        }

    def _row_to_event(self, row: sqlite3.Row) -> JsonObject:
        """把事件表记录转换成会话详情接口可直接返回的对象。"""

        return {
            "id": row["id"],
            "event_type": row["event_type"],
            "content": row["content"],
            "created_at": row["created_at"],
            "seq_no": row["seq_no"],
            "metadata": self._load_json(row["metadata"]),
        }

    def _row_to_artifact(self, row: sqlite3.Row) -> JsonObject:
        """把产物表记录转换成普通字典。"""

        return {
            "id": row["id"],
            "artifact_type": row["artifact_type"],
            "name": row["name"],
            "path": row["path"],
            "size": row["size"],
            "created_at": row["created_at"],
            "metadata": self._load_json(row["metadata"]),
        }

    def _dump_json(self, payload: JsonObject) -> str:
        """把字典安全序列化成 UTF-8 友好的 JSON 字符串。"""

        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _load_json(self, payload: str | None) -> JsonObject:
        """把 JSON 文本安全反序列化为空字典或普通字典。"""

        if not payload:
            return {}
        loaded = json.loads(payload)
        return loaded if isinstance(loaded, dict) else {}


class SQLiteSessionRepository(SessionRepository):
    """基于 SQLite 后端实现的会话仓储。"""

    def __init__(
        self,
        initial: list[JsonObject] | None = None,
        storage_root: Path | str | None = None,
        backend: SessionStoreBackend | None = None,
        default_user_id: str = "local-user",
    ):
        """初始化会话仓储，并在首次启动时可选导入初始数据。"""

        self.default_user_id = default_user_id
        self.backend = backend or SessionStoreBackend(storage_root or Path("data"))
        if initial and not self.backend.has_sessions():
            self._bootstrap_initial_sessions(initial)
        logger.info("会话仓储初始化完成", extra={"storage_root": str(self.backend.storage_root.resolve())})

    def create(self, title: str = "New chat", workspace_scope: JsonObject | None = None) -> SessionRecord:
        """创建新的会话记录。"""

        # 会话编号会出现在数据库、日志和本地目录中，因此保留创建时间，方便人工排查。
        key = create_readable_id()
        now = utc_now()
        self.backend.create_session(
            session_id=key,
            title=title,
            created_at=now,
            updated_at=now,
            user_id=self.default_user_id,
            workspace_scope=copy.deepcopy(workspace_scope),
            metadata={"schema_version": 1},
            status="created",
        )
        logger.info("创建新会话", extra={"session_key": key, "title": title})
        return self.get(key)

    def get(self, key: str) -> SessionRecord:
        """根据会话键获取会话记录。"""

        raw_session = self.backend.get_session(key)
        if raw_session is None:
            logger.warning("会话不存在", extra={"session_key": key})
            raise SessionError(f"session not found: {key}", 404)
        return self._hydrate_record(raw_session)

    def list(self) -> list[JsonObject]:
        """返回按更新时间倒序排列的会话摘要列表。"""

        return [self._hydrate_record(item).summary() for item in self.backend.list_sessions()]

    def delete(self, key: str) -> None:
        """删除指定会话。"""

        self.get(key)
        self.backend.delete_session(key)
        logger.info("删除会话", extra={"session_key": key})

    def append_message(self, key: str, role: str, content: str, **extra: Any) -> JsonObject:
        """向指定会话追加一条消息。"""

        record = self.get(key)
        created_at = utc_now()
        message_id = str(extra.pop("id", uuid.uuid4().hex))
        payload = {
            "id": message_id,
            "role": role,
            "content": content,
            "created_at": created_at,
            **extra,
        }
        metadata = {
            item_key: item_value
            for item_key, item_value in payload.items()
            if item_key not in {"id", "role", "content", "created_at"}
        }
        self.backend.append_message(
            session_id=key,
            message_id=message_id,
            role=role,
            content=content,
            created_at=created_at,
            parent_id=metadata.get("parent_id"),
            metadata=metadata,
        )
        if role == "user" and (record.title == "New chat" or not record.title.strip()):
            self._rename_if_default_title(key, content[:40] or "New chat")
        logger.debug(
            "追加会话消息",
            extra={"session_key": key, "role": role, "content_length": len(content), "message_id": message_id},
        )
        return copy.deepcopy(payload)

    def append_event(
        self,
        key: str,
        event_type: str,
        content: str = "",
        metadata: JsonObject | None = None,
        created_at: str | None = None,
    ) -> JsonObject:
        """向指定会话追加一条结构化过程事件。"""

        self.get(key)
        return self.backend.append_event(
            session_id=key,
            event_type=event_type,
            content=content,
            created_at=created_at or utc_now(),
            metadata=copy.deepcopy(metadata or {}),
        )

    def write_artifact(
        self,
        key: str,
        artifact_type: str,
        name: str,
        content: str | bytes,
        *,
        relative_path: str,
        metadata: JsonObject | None = None,
        created_at: str | None = None,
        encoding: str = "utf-8",
    ) -> JsonObject:
        """向指定会话写入产物文件，并在仓储中登记元数据。"""

        self.get(key)
        resolved_created_at = created_at or utc_now()
        target_path = self.backend.write_artifact_file(
            session_id=key,
            relative_path=relative_path,
            content=content,
            encoding=encoding,
        )
        artifact_record = self.backend.append_artifact_record(
            session_id=key,
            artifact_type=artifact_type,
            name=name,
            path=str(target_path),
            size=target_path.stat().st_size,
            created_at=resolved_created_at,
            metadata=copy.deepcopy(metadata or {}),
        )
        logger.info(
            "写入会话产物",
            extra={
                "session_key": key,
                "artifact_type": artifact_type,
                "artifact_name": name,
                "artifact_path": str(target_path),
            },
        )
        return artifact_record

    def read_artifact_path(self, key: str, artifact_id: str) -> Path | None:
        """根据产物编号返回安全的产物文件路径。

        中文说明：
        先按 id 找到产物记录，再把记录里保存的路径做一次安全校验：路径解析后
        必须仍然位于当前会话目录内，并且文件确实存在，否则返回 None。这样即使
        历史数据里存了越界路径或被删除的文件，下载接口也不会把它读出去。
        """

        session = self.get(key)
        artifact = next(
            (item for item in session.artifacts if str(item.get("id") or "") == artifact_id),
            None,
        )
        if artifact is None:
            return None
        raw_path = str(artifact.get("path") or "").strip()
        if not raw_path:
            return None
        session_dir = (self.backend.sessions_dir / key).resolve()
        resolved = Path(raw_path).resolve(strict=False)
        if not resolved.is_relative_to(session_dir) or not resolved.is_file():
            logger.warning(
                "产物文件路径校验未通过，拒绝提供下载",
                extra={"session_key": key, "artifact_id": artifact_id, "artifact_path": raw_path},
            )
            return None
        return resolved

    def set_workspace_scope(self, key: str, workspace_scope: JsonObject | None) -> JsonObject:
        """更新会话的工作区范围信息。"""

        self.get(key)
        updated_at = utc_now()
        self.backend.update_workspace_scope(key, copy.deepcopy(workspace_scope), updated_at)
        self.backend.append_event(
            session_id=key,
            event_type="workspace_scope_update",
            content="workspace scope updated",
            created_at=updated_at,
            metadata={"workspace_scope": copy.deepcopy(workspace_scope)},
        )
        logger.info("更新会话工作区范围", extra={"session_key": key, "has_workspace_scope": workspace_scope is not None})
        return self.get(key).summary()

    def set_run_started_at(self, key: str, started_at: str | None) -> JsonObject:
        """更新会话当前回合的运行状态时间戳。"""

        self.get(key)
        updated_at = utc_now()
        self.backend.update_run_started_at(key, started_at, updated_at)
        if started_at is not None:
            self.backend.append_event(
                session_id=key,
                event_type="status_change",
                content="running",
                created_at=updated_at,
                metadata={"run_started_at": started_at},
            )
        logger.debug(
            "更新会话运行状态",
            extra={"session_key": key, "run_started_at": started_at, "is_running": started_at is not None},
        )
        return self.get(key).summary()

    def set_status(self, key: str, status: str) -> JsonObject:
        """显式更新会话状态，并同步写入状态变更事件。"""

        self.get(key)
        updated_at = utc_now()
        self.backend.update_status(key, status, updated_at)
        self.backend.append_event(
            session_id=key,
            event_type="status_change",
            content=status,
            created_at=updated_at,
            metadata={"status": status},
        )
        logger.info("更新会话状态", extra={"session_key": key, "status": status})
        return self.get(key).summary()

    def set_active_run_id(self, key: str, run_id: str | None) -> JsonObject:
        """更新会话 metadata 里的 active_run_id 字段。

        中文说明：
        前端刷新页面时需要知道当前会话有没有还在跑的 run 以及去哪个地址接回实时流。
        这个方法把 run_id 写进 session 的 metadata 列，start_run 时写入、
        三个 finalizer 和 reset_stale_runs 清空。
        """

        self.get(key)
        now = utc_now()
        # 底层 SQL 在 SessionStoreBackend.update_active_run_id 中，
        # 仓储层只做业务包装（写 metadata → 由底层读旧值再写新值）。
        self.backend.update_active_run_id(key, run_id, now)
        logger.debug(
            "更新会话活跃 run 编号",
            extra={"session_key": key, "active_run_id": run_id},
        )
        return self.get(key).summary()

    def reset_stale_runs(self) -> int:
        """启动时把所有「正在运行」或「未清 run_started_at」的会话置成已中断。

        中文说明：
        后端进程被强杀后，数据库里可能有 status='running' 的残留记录，
        或者 run_started_at 还没被三个 finalizer 清空的记录。这个方法把两种
        情况统一修成 interrupted，清空 run_started_at 和 metadata 里的
        active_run_id，让用户可以继续对话。
        """

        now = utc_now()
        # 底层 SQL 在 SessionStoreBackend.reset_stale_runs 中，仓储层只做调用包装。
        running_count, orphan_count = self.backend.reset_stale_runs(now)
        total = running_count + orphan_count
        if total > 0:
            logger.info(
                "启动自愈：修复了残留的运行中会话",
                extra={"interrupted_sessions": running_count, "cleared_orphan_timestamps": orphan_count},
            )
        return total

    def purge_stream_events(self) -> int:
        """清掉历史上全部流式 token 事件（delta / reasoning_delta）。

        中文说明：
        改造前每一个 reasoning_delta / delta 都会往 session_event 表里插一条记录，
        一次长调研能产生上万条事件。改造后这些流式 token 不再落库，但历史数据里
        已经堆积的旧事件还在。启动时跑一次清理，把数据库瘦身。返回被删掉的记录条数。
        """

        # 底层 SQL 在 SessionStoreBackend.purge_stream_events 中，仓储层只做调用包装。
        deleted = self.backend.purge_stream_events()
        if deleted > 0:
            logger.info(
                "启动清理：删除了历史流式 token 事件",
                extra={"purged_events": deleted},
            )
        return deleted

    def _bootstrap_initial_sessions(self, initial: list[JsonObject]) -> None:
        """把初始会话列表导入持久化后端。"""

        for item in initial:
            key = str(item.get("key") or create_readable_id())
            created_at = str(item.get("created_at") or utc_now())
            updated_at = str(item.get("updated_at") or created_at)
            workspace_scope = copy.deepcopy(item.get("workspace_scope"))
            self.backend.create_session(
                session_id=key,
                title=str(item.get("title") or "New chat"),
                created_at=created_at,
                updated_at=updated_at,
                user_id=str(item.get("user_id") or self.default_user_id),
                workspace_scope=workspace_scope,
                metadata=copy.deepcopy(item.get("metadata") or {"schema_version": 1}),
                status=str(item.get("status") or "created"),
            )
            for message in item.get("messages") or []:
                self.backend.append_message(
                    session_id=key,
                    message_id=str(message.get("id") or uuid.uuid4().hex),
                    role=str(message.get("role") or "user"),
                    content=str(message.get("content") or ""),
                    created_at=str(message.get("created_at") or utc_now()),
                    parent_id=message.get("parent_id"),
                    metadata={
                        entry_key: copy.deepcopy(entry_value)
                        for entry_key, entry_value in message.items()
                        if entry_key not in {"id", "role", "content", "created_at", "parent_id"}
                    },
                )

    def _hydrate_record(self, payload: JsonObject) -> SessionRecord:
        """把底层后端读取出的原始字典组装成 `SessionRecord`。"""

        messages = self.backend.list_messages(payload["key"])
        events = self.backend.list_events(payload["key"])
        artifacts = self.backend.list_artifacts(payload["key"])
        return SessionRecord(
            key=payload["key"],
            title=str(payload.get("title") or "New chat"),
            created_at=str(payload.get("created_at") or utc_now()),
            updated_at=str(payload.get("updated_at") or utc_now()),
            status=str(payload.get("status") or "created"),
            summary_text=str(payload.get("summary") or ""),
            messages=messages,
            events=events,
            artifacts=artifacts,
            workspace_scope=copy.deepcopy(payload.get("workspace_scope")),
            run_started_at=payload.get("run_started_at"),
            user_id=str(payload.get("user_id") or self.default_user_id),
            last_message_at=payload.get("last_message_at"),
            metadata=copy.deepcopy(payload.get("metadata") or {}),
        )

    def _rename_if_default_title(self, key: str, title: str) -> None:
        """当新会话首次收到用户输入时，用输入摘要替换默认标题。"""

        record = self.get(key)
        if record.title != "New chat" and record.title.strip():
            return
        raw_session = self.backend.get_session(key)
        metadata = dict((raw_session or {}).get("metadata") or {})
        self.backend.update_title(key, title, utc_now())
        self.backend.append_event(
            session_id=key,
            event_type="summary_update",
            content=title,
            created_at=utc_now(),
            metadata={"title": title, **metadata},
        )
