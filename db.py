"""数据层：schema、连接、幂等迁移。不持有任何全局连接或锁。

调用方（server）持有唯一的连接与事务锁，把 conn 注入进来。
schema_version:
  1 -> 2：tasks.assignee、agents 凭证字段
  2 -> 3：安全加固 —— users / sessions / audit_log，agents 归属与接入约束列
  3 -> 4：Agent 分类与归档 —— agents.archived / archived_at / category
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    assignee    TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    progress    INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    agent_id    TEXT,
    result      TEXT,
    error       TEXT,
    claimed_at  REAL,
    finished_at REAL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, id);
CREATE TABLE IF NOT EXISTS agents (
    name          TEXT PRIMARY KEY,
    token_hash    TEXT,
    paired_at     REAL,
    revoked       INTEGER NOT NULL DEFAULT 0,
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL,
    claimed_count INTEGER NOT NULL DEFAULT 0,
    archived      INTEGER NOT NULL DEFAULT 0,
    archived_at   REAL,
    category      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pairing_codes (
    code_hash   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    expires_at  REAL NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- v3 安全加固新增表
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'operator',
    status         TEXT NOT NULL DEFAULT 'active',
    display_name   TEXT NOT NULL DEFAULT '',
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until   REAL,
    created_at     REAL NOT NULL,
    last_login_at  REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    expires_at  REAL NOT NULL,
    created_at  REAL NOT NULL,
    ip          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    actor_type  TEXT NOT NULL,          -- user / agent / system
    actor       TEXT NOT NULL,          -- 用户名 / agent 名 / 'system'
    action      TEXT NOT NULL,          -- login / task.create / agent.revoke ...
    target      TEXT NOT NULL DEFAULT '',
    ip          TEXT NOT NULL DEFAULT '',
    result      TEXT NOT NULL DEFAULT 'ok',   -- ok / denied / fail
    detail      TEXT NOT NULL DEFAULT '',
    request_id  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor, ts);
"""


def _add_columns(conn, table: str, cols: tuple[tuple[str, str], ...]) -> None:
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
    for name, ddl in cols:
        if name not in existing:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))


def connect(db_path: Path | str) -> sqlite3.Connection:
    """建连接：WAL + 行字典 + 忙等待 + 外键。事务由调用方控制。"""
    path = Path(db_path)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表 + 幂等迁移。可安全重复调用；旧库按列检测补列，数据不丢。"""
    conn.executescript(SCHEMA)

    # --- v1->v2 历史迁移 ---
    _add_columns(conn, "tasks", (("assignee", "TEXT"),))
    conn.execute("UPDATE tasks SET assignee=COALESCE(agent_id, 'unassigned') WHERE assignee IS NULL OR assignee=''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee, status, id)")
    _add_columns(conn, "agents", (
        ("token_hash", "TEXT"),
        ("paired_at", "REAL"),
        ("revoked", "INTEGER NOT NULL DEFAULT 0"),
    ))

    # --- v2->v3 安全加固迁移 ---
    # agents：归属用户、接入审批、IP 白名单、token 时效、配对来源
    _add_columns(conn, "agents", (
        ("owner_user_id", "INTEGER"),
        ("approval", "TEXT NOT NULL DEFAULT 'approved'"),
        ("allowed_ips", "TEXT NOT NULL DEFAULT ''"),
        ("token_issued_at", "REAL"),
        ("token_expires_at", "REAL"),
        ("paired_ip", "TEXT NOT NULL DEFAULT ''"),
        ("paired_by", "TEXT NOT NULL DEFAULT ''"),
    ))
    # 旧 Agent 凭证保留：一律视为已审批、无 IP 限制、无时效
    conn.execute("UPDATE agents SET approval='approved' WHERE approval IS NULL OR approval=''")

    # pairing_codes：创建者、来源 IP
    _add_columns(conn, "pairing_codes", (
        ("created_by", "TEXT NOT NULL DEFAULT ''"),
        ("created_ip", "TEXT NOT NULL DEFAULT ''"),
    ))

    # tasks：下发者（用于用户维度隔离与审计）
    _add_columns(conn, "tasks", (("created_by", "TEXT NOT NULL DEFAULT ''"),))

    # --- v3->v4 Agent 分类与归档迁移 ---
    # 归档是"退役隐藏"：默认全部可见（archived=0）、无分类，现有数据不受影响
    _add_columns(conn, "agents", (
        ("archived", "INTEGER NOT NULL DEFAULT 0"),
        ("archived_at", "REAL"),
        ("category", "TEXT NOT NULL DEFAULT ''"),
    ))
    conn.execute("UPDATE agents SET archived=0 WHERE archived IS NULL")
    conn.execute("UPDATE agents SET category='' WHERE category IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_archived ON agents(archived)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_category ON agents(category)")

    conn.execute(
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),),
    )


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
    return int(row["value"]) if row else 0
