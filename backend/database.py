# -*- coding: utf-8 -*-
"""
SQLite 持久化：存储任务元数据、识别文本与摘要结果。
"""
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional, List, Dict, Any

from config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    filename        TEXT NOT NULL,
    status          TEXT NOT NULL,           -- pending / processing / done / failed
    duration        REAL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    error           TEXT,
    transcript      TEXT,                    -- 完整转写文本（JSON 分段）
    language        TEXT,
    summary         TEXT,                    -- 总体摘要
    key_points      TEXT,                    -- 关键要点（JSON 数组）
    chapters        TEXT,                    -- 章节切分（JSON 数组）
    insight         TEXT,                    -- 深度总结（观后感式段落）
    lines_text      TEXT,                    -- 台词稿（简体中文、带标点）
    word_count      INTEGER DEFAULT 0,
    source_url      TEXT                      -- 来源链接（链接任务时有值）
);
"""


@contextmanager
def get_conn():
    """每次操作使用独立连接，退出时自动提交并关闭，避免文件句柄泄漏。"""
    conn = sqlite3.connect(str(settings.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    _migrate(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """为已存在的旧数据库补充新增列。"""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if "source_url" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN source_url TEXT")
        conn.commit()
    if "lines_text" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN lines_text TEXT")
        conn.commit()
    if "insight" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN insight TEXT")
        conn.commit()


def create_task(task_id: str, filename: str, source_url: str = "") -> None:
    now = time.time()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tasks (id, filename, status, created_at, updated_at, source_url) "
            "VALUES (?,?,?,?,?,?)",
            (task_id, filename, "pending", now, now, source_url or None),
        )


def update_task(task_id: str, **fields) -> None:
    fields["updated_at"] = time.time()
    # 将 list/dict 类型字段序列化为 JSON 字符串，适配 SQLite TEXT 列
    _JSON_FIELDS = {"transcript", "key_points", "chapters"}
    for k in list(fields.keys()):
        if k in _JSON_FIELDS and isinstance(fields[k], (list, dict)):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [task_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE tasks SET {cols} WHERE id = ?", vals)


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return None
    return _row_to_dict(row)


def list_tasks(limit: int = 50) -> List[Dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, filename, status, duration, created_at, updated_at, word_count, source_url "
            "FROM tasks ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def delete_task(task_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    for k in ("transcript", "key_points", "chapters"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (json.JSONDecodeError, TypeError):
                pass
    return d
