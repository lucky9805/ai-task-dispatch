"""仓储层：只做数据访问，不含业务规则。所有方法接受 conn，由调用方控制事务。

仓储：AgentRepo / TaskRepo / PairingRepo / UserRepo / SessionRepo / AuditRepo。
"""
from __future__ import annotations

import json
import sqlite3
import time


def _task_dict(row) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["payload"] = json.loads(d["payload"] or "{}")
    return d


class BaseRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn


class AgentRepo(BaseRepo):
    def get(self, name: str):
        return self.conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()

    def touch(self, name: str, now: float) -> None:
        """活动打卡：存在则更新 last_seen，不存在则登记。"""
        cur = self.conn.execute("UPDATE agents SET last_seen=? WHERE name=?", (now, name))
        if cur.rowcount == 0:
            self.conn.execute(
                "INSERT INTO agents (name, first_seen, last_seen) VALUES (?,?,?)",
                (name, now, now),
            )

    def register(self, name: str, now: float) -> None:
        """登记并清撤销标记（生成配对码时用）。"""
        cur = self.conn.execute(
            "UPDATE agents SET revoked=0, last_seen=? WHERE name=?", (now, name)
        )
        if cur.rowcount == 0:
            self.conn.execute(
                "INSERT INTO agents (name, first_seen, last_seen, revoked) VALUES (?,?,?,0)",
                (name, now, now),
            )

    def set_token(self, name: str, token_hash: str, now: float, *,
                  ip: str = "", expires_at: float | None = None) -> None:
        cur = self.conn.execute(
            "UPDATE agents SET token_hash=?, paired_at=?, revoked=0, last_seen=?, "
            "token_issued_at=?, token_expires_at=?, paired_ip=? WHERE name=?",
            (token_hash, now, now, now, expires_at, ip, name),
        )
        if cur.rowcount == 0:
            self.conn.execute(
                "INSERT INTO agents (name, token_hash, paired_at, revoked, first_seen, "
                "last_seen, claimed_count, token_issued_at, token_expires_at, paired_ip) "
                "VALUES (?,?,?,0,?,?,0,?,?,?)",
                (name, token_hash, now, now, now, now, expires_at, ip),
            )

    def revoke(self, name: str) -> bool:
        cur = self.conn.execute(
            "UPDATE agents SET revoked=1, token_hash=NULL WHERE name=?", (name,)
        )
        return cur.rowcount > 0

    def list_all(self) -> list:
        return self.conn.execute("SELECT * FROM agents ORDER BY name").fetchall()

    def inc_claimed(self, name: str, now: float) -> None:
        self.conn.execute(
            "UPDATE agents SET last_seen=?, claimed_count=claimed_count+1 WHERE name=?",
            (now, name),
        )

    def set_owner(self, name: str, owner_id: int) -> None:
        self.conn.execute("UPDATE agents SET owner_user_id=? WHERE name=?", (owner_id, name))

    def set_access(self, name: str, *, allowed_ips: str, approval: str,
                   max_age: float | None, now: float) -> None:
        self.conn.execute(
            "UPDATE agents SET allowed_ips=?, approval=?, token_issued_at=?, token_expires_at=? "
            "WHERE name=?",
            (allowed_ips, approval, now,
             (now + max_age) if max_age else None, name),
        )

    def list_by_owner(self, owner_id: int) -> list:
        return self.conn.execute(
            "SELECT * FROM agents WHERE owner_user_id=? ORDER BY name", (owner_id,)
        ).fetchall()

    def approve(self, name: str, approval: str) -> bool:
        cur = self.conn.execute("UPDATE agents SET approval=? WHERE name=?", (approval, name))
        return cur.rowcount > 0

    def set_archived(self, name: str, archived: bool, now: float) -> bool:
        """归档（虚拟删除）：archived=1 隐藏且停活；archived_at 记录时间，取消归档清空。"""
        at = now if archived else None
        cur = self.conn.execute(
            "UPDATE agents SET archived=?, archived_at=? WHERE name=?",
            (1 if archived else 0, at, name),
        )
        return cur.rowcount > 0

    def set_category(self, name: str, category: str) -> bool:
        cur = self.conn.execute("UPDATE agents SET category=? WHERE name=?",
                                (category, name))
        return cur.rowcount > 0

    def list_categories(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT category FROM agents WHERE category!='' ORDER BY category"
        )]


class PairingRepo(BaseRepo):
    def get_by_hash(self, code_hash: str):
        return self.conn.execute(
            "SELECT * FROM pairing_codes WHERE code_hash=?", (code_hash,)
        ).fetchone()

    def insert(self, code_hash: str, name: str, expires_at: float, now: float,
               created_by: str = "", created_ip: str = "") -> None:
        self.conn.execute(
            "INSERT INTO pairing_codes (code_hash, name, expires_at, used, created_at, "
            "created_by, created_ip) VALUES (?,?,?,0,?,?,?)",
            (code_hash, name, expires_at, now, created_by, created_ip),
        )

    def mark_used(self, code_hash: str) -> None:
        self.conn.execute(
            "UPDATE pairing_codes SET used=1 WHERE code_hash=?", (code_hash,)
        )

    def count_recent_by_ip(self, ip: str, since: float) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM pairing_codes WHERE created_ip=? AND created_at>=?",
            (ip, since),
        ).fetchone()
        return row["n"]


class TaskRepo(BaseRepo):
    def insert(self, title: str, assignee: str, payload: dict, now: float,
               created_by: str = "") -> dict:
        cur = self.conn.execute(
            "INSERT INTO tasks (title, assignee, payload, status, created_at, updated_at, created_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (title, assignee, json.dumps(payload, ensure_ascii=False), "pending", now, now, created_by),
        )
        return self.get(cur.lastrowid)

    def get(self, task_id: int) -> dict | None:
        return _task_dict(
            self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        )

    def list(self, status: str | None, assignee: str | None) -> list[dict]:
        clauses, args = [], []
        if status:
            clauses.append("status=?")
            args.append(status)
        if assignee:
            clauses.append("assignee=?")
            args.append(assignee)
        sql = "SELECT * FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return [_task_dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]

    def claim_candidate(self, name: str):
        """领取候选：指派给自己且待领取的最早任务。"""
        return self.conn.execute(
            "SELECT * FROM tasks WHERE status='pending' AND assignee=? ORDER BY id LIMIT 1",
            (name,),
        ).fetchone()

    def mark_running(self, task_id: int, name: str, now: float) -> dict:
        self.conn.execute(
            "UPDATE tasks SET status='running', agent_id=?, claimed_at=?, progress=10, "
            "message='已领取，开始执行', updated_at=? WHERE id=?",
            (name, now, now, task_id),
        )
        return self.get(task_id)

    def set_progress(self, task_id: int, progress: int, message: str, now: float) -> None:
        self.conn.execute(
            "UPDATE tasks SET progress=?, message=?, updated_at=? WHERE id=?",
            (progress, message, now, task_id),
        )

    def heartbeat(self, task_id: int, now: float) -> None:
        self.conn.execute(
            "UPDATE tasks SET claimed_at=?, updated_at=? WHERE id=?", (now, now, task_id)
        )

    def finish(self, task_id: int, succeeded: bool, text: str, now: float) -> None:
        self.conn.execute(
            "UPDATE tasks SET status=?, result=?, error=?, progress=?, message=?, "
            "finished_at=?, updated_at=? WHERE id=?",
            (
                "succeeded" if succeeded else "failed",
                text if succeeded else None,
                None if succeeded else text,
                100,
                "执行完成" if succeeded else "执行失败",
                now, now, task_id,
            ),
        )

    def reclaim_expired(self, now: float, lease_seconds: float) -> int:
        cur = self.conn.execute(
            "UPDATE tasks SET status='pending', agent_id=NULL, claimed_at=NULL, progress=0, "
            "message='租约超时，已回收', updated_at=? "
            "WHERE status='running' AND claimed_at < ?",
            (now, now - lease_seconds),
        )
        return cur.rowcount

    def owned(self, task_id: int, agent_id: str) -> bool:
        return self.conn.execute(
            "SELECT id FROM tasks WHERE id=? AND status='running' AND agent_id=? AND assignee=?",
            (task_id, agent_id, agent_id),
        ).fetchone() is not None

    def list_by_assignees(self, assignees: list[str], status: str | None) -> list[dict]:
        """按 assignee 白名单列出任务；assignees 为空返回空列表。"""
        if not assignees:
            return []
        marks = ",".join("?" for _ in assignees)
        clauses = ["assignee IN (%s)" % marks]
        args: list = list(assignees)
        if status:
            clauses.append("status=?")
            args.append(status)
        sql = "SELECT * FROM tasks WHERE " + " AND ".join(clauses) + " ORDER BY id"
        return [_task_dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]


class UserRepo(BaseRepo):
    def get(self, user_id: int):
        return self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def get_by_name(self, username: str):
        return self.conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()

    def list_all(self) -> list:
        return self.conn.execute(
            "SELECT id, username, role, status, display_name, failed_attempts, "
            "locked_until, created_at, last_login_at FROM users ORDER BY id"
        ).fetchall()

    def insert(self, username: str, password_hash: str, role: str,
               display_name: str, now: float) -> dict:
        cur = self.conn.execute(
            "INSERT INTO users (username, password_hash, role, status, display_name, created_at) "
            "VALUES (?,?,?, 'active', ?, ?)",
            (username, password_hash, role, display_name, now),
        )
        return dict(self.conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone())

    def set_password(self, user_id: int, password_hash: str) -> None:
        self.conn.execute("UPDATE users SET password_hash=?, failed_attempts=0, locked_until=NULL WHERE id=?",
                          (password_hash, user_id))

    def set_role(self, user_id: int, role: str) -> None:
        self.conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))

    def set_status(self, user_id: int, status: str) -> None:
        self.conn.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))

    def set_display_name(self, user_id: int, display_name: str) -> None:
        self.conn.execute("UPDATE users SET display_name=? WHERE id=?", (display_name, user_id))

    def record_failed(self, user_id: int, now: float, lock_until: float | None) -> None:
        if lock_until is not None:
            self.conn.execute(
                "UPDATE users SET failed_attempts=failed_attempts+1, locked_until=? WHERE id=?",
                (lock_until, user_id),
            )
        else:
            self.conn.execute(
                "UPDATE users SET failed_attempts=failed_attempts+1 WHERE id=?", (user_id,)
            )

    def clear_failed(self, user_id: int, now: float) -> None:
        self.conn.execute(
            "UPDATE users SET failed_attempts=0, locked_until=NULL, last_login_at=? WHERE id=?",
            (now, user_id),
        )

    def delete(self, user_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        return cur.rowcount > 0

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


class SessionRepo(BaseRepo):
    def insert(self, token_hash: str, user_id: int, expires_at: float,
               now: float, ip: str = "") -> None:
        self.conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at, created_at, ip) "
            "VALUES (?,?,?,?,?)",
            (token_hash, user_id, expires_at, now, ip),
        )

    def get(self, token_hash: str):
        return self.conn.execute(
            "SELECT * FROM sessions WHERE token_hash=?", (token_hash,)
        ).fetchone()

    def delete(self, token_hash: str) -> None:
        self.conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def delete_by_user(self, user_id: int) -> None:
        self.conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def purge_expired(self, now: float) -> None:
        self.conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))


class AuditRepo(BaseRepo):
    def insert(self, ts: float, actor_type: str, actor: str, action: str,
               target: str = "", ip: str = "", result: str = "ok",
               detail: str = "", request_id: str = "") -> None:
        self.conn.execute(
            "INSERT INTO audit_log (ts, actor_type, actor, action, target, ip, result, detail, request_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, actor_type, actor, action, target, ip, result, detail[:2000], request_id),
        )

    def list(self, limit: int, actor: str | None = None,
             action: str | None = None) -> list:
        clauses, args = [], []
        if actor:
            clauses.append("actor=?")
            args.append(actor)
        if action:
            clauses.append("action LIKE ?")
            args.append(action + "%")
        sql = "SELECT * FROM audit_log"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self.conn.execute(sql, tuple(args)).fetchall()]
