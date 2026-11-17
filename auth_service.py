"""认证与安全管理服务：用户、会话、RBAC、审计、接入策略、数据隔离。

与既有 service.py 并列，由 server 控制器注入 conn/tx 后调用。
所有涉及身份的操作在这里统一留痕到 audit_log。
"""
from __future__ import annotations

import time

import security
from errors import AuthError, ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from repository import AgentRepo, AuditRepo, SessionRepo, UserRepo

# 角色与权限点
ROLES = {"admin", "operator", "viewer"}
PERMISSIONS = {
    "admin": {"task.create", "task.view", "agent.pair", "agent.view", "agent.revoke",
              "agent.manage", "user.manage", "audit.view", "selftest.run"},
    "operator": {"task.create", "task.view", "agent.pair", "agent.view", "agent.manage",
                 "selftest.run"},
    "viewer": {"task.view", "agent.view"},
}

MAX_FAILED = 5
LOCK_SECONDS = 300
SESSION_TTL = 8 * 3600


def _public_user(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "status": row["status"],
        "display_name": row["display_name"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


class SecurityService:
    def __init__(self, conn, tx, *, session_ttl: float = SESSION_TTL):
        self.conn = conn
        self.tx = tx
        self.session_ttl = session_ttl

    def _repos(self, c):
        return UserRepo(c), SessionRepo(c), AuditRepo(c), AgentRepo(c)

    # ---------- 审计 ----------
    def audit(self, actor_type: str, actor: str, action: str, *,
              target: str = "", ip: str = "", result: str = "ok",
              detail: str = "", request_id: str = "") -> None:
        with self.tx(immediate=True) as c:
            AuditRepo(c).insert(time.time(), actor_type, actor, action, target,
                                ip, result, detail, request_id)

    def list_audit(self, limit: int, actor: str | None, action: str | None,
                   request_id: str = "") -> list[dict]:
        limit = max(1, min(int(limit or 50), 500))
        with self.tx() as c:
            return AuditRepo(c).list(limit, actor, action)

    # ---------- 用户管理 ----------
    def bootstrap_admin(self) -> tuple[str, str] | None:
        """无用户时创建初始 admin，随机密码，返回 (用户名, 明文密码)。已有用户返回 None。"""
        with self.tx(immediate=True) as c:
            users, _, audit, _ = self._repos(c)
            if users.count() > 0:
                return None
            username = "admin"
            password = security.new_token(12)
            users.insert(username, security.hash_password(password), "admin", "系统管理员", time.time())
            audit.insert(time.time(), "system", "system", "user.bootstrap",
                         target=username, result="ok", detail="首次启动创建管理员")
            return username, password

    def create_user(self, username: str, password: str, role: str,
                    display_name: str = "", actor: str = "", ip: str = "") -> dict:
        username = (username or "").strip()
        if not username or not username.isalnum() or len(username) < 3:
            raise ValidationError("用户名需至少 3 位字母数字")
        if role not in ROLES:
            raise ValidationError("角色必须是 admin/operator/viewer")
        err = security.validate_password_strength(password)
        if err:
            raise ValidationError(err)
        with self.tx(immediate=True) as c:
            users, _, audit, _ = self._repos(c)
            if users.get_by_name(username):
                raise ConflictError("用户名已存在")
            row = users.insert(username, security.hash_password(password), role,
                               display_name.strip(), time.time())
            # 由调用方（Web API / CLI）统一记录 user.create，避免重复审计
            return _public_user(users.get(row["id"]))

    def list_users(self) -> list[dict]:
        with self.tx() as c:
            return [_public_user(r) for r in UserRepo(c).list_all()]

    def update_user(self, user_id: int, *, role: str | None = None,
                    status: str | None = None, display_name: str | None = None,
                    password: str | None = None, actor: str = "", ip: str = "") -> dict:
        with self.tx(immediate=True) as c:
            users, sessions, audit, _ = self._repos(c)
            row = users.get(user_id)
            if not row:
                raise NotFoundError("用户不存在")
            if role is not None:
                if role not in ROLES:
                    raise ValidationError("角色无效")
                users.set_role(user_id, role)
            if status is not None:
                if status not in ("active", "disabled"):
                    raise ValidationError("状态无效")
                users.set_status(user_id, status)
                if status == "disabled":
                    sessions.delete_by_user(user_id)  # 禁用即踢下线
            if display_name is not None:
                users.set_display_name(user_id, display_name.strip())
            if password is not None:
                err = security.validate_password_strength(password)
                if err:
                    raise ValidationError(err)
                users.set_password(user_id, security.hash_password(password))
                sessions.delete_by_user(user_id)  # 改密踢下线
            audit.insert(time.time(), "user", actor or "system", "user.update",
                         target=row["username"], ip=ip,
                         detail=",".join(k for k, v in (("role", role), ("status", status),
                                                        ("password", password), ("name", display_name)) if v is not None))
            return _public_user(users.get(user_id))

    def delete_user(self, user_id: int, actor: str = "", ip: str = "") -> None:
        with self.tx(immediate=True) as c:
            users, sessions, audit, agents = self._repos(c)
            row = users.get(user_id)
            if not row:
                raise NotFoundError("用户不存在")
            if row["role"] == "admin" and users.count() <= 1:
                raise ConflictError("不能删除最后一个管理员")
            # 该用户名下的 Agent 转为无主（管理员可接管）
            c.execute("UPDATE agents SET owner_user_id=NULL WHERE owner_user_id=?", (user_id,))
            sessions.delete_by_user(user_id)
            users.delete(user_id)
            audit.insert(time.time(), "user", actor or "system", "user.delete",
                         target=row["username"], ip=ip)

    # ---------- 登录 / 会话 ----------
    def login(self, username: str, password: str, ip: str = "") -> dict:
        username = (username or "").strip()
        outcome = None  # (exc, message) —— 失败计数需随事务提交，故异常放到事务外抛
        with self.tx(immediate=True) as c:
            users, sessions, audit, _ = self._repos(c)
            row = users.get_by_name(username)
            now = time.time()
            if not row:
                audit.insert(now, "user", username or "(empty)", "login", ip=ip, result="fail",
                             detail="用户不存在")
                outcome = (AuthError, "用户名或密码错误")
            elif row["status"] != "active":
                audit.insert(now, "user", username, "login", ip=ip, result="denied", detail="账号已禁用")
                outcome = (PermissionDeniedError, "账号已禁用")
            elif row["locked_until"] and row["locked_until"] > now:
                audit.insert(now, "user", username, "login", ip=ip, result="denied", detail="账号锁定中")
                outcome = (PermissionDeniedError, "账号已锁定，请稍后再试")
            elif not security.verify_password(password or "", row["password_hash"]):
                failed = row["failed_attempts"] + 1
                lock_until = now + LOCK_SECONDS if failed >= MAX_FAILED else None
                users.record_failed(row["id"], now, lock_until)
                audit.insert(now, "user", username, "login", ip=ip, result="fail",
                             detail="密码错误 %d/%d" % (failed, MAX_FAILED))
                outcome = (AuthError, "用户名或密码错误")
            else:
                users.clear_failed(row["id"], now)
                token = security.new_token(32)
                sessions.insert(security.token_hash(token), row["id"], now + self.session_ttl, now, ip)
                audit.insert(now, "user", username, "login", ip=ip, result="ok")
                result = {"token": token, "user": _public_user(users.get(row["id"])),
                          "expires_in": self.session_ttl}
        if outcome:
            raise outcome[0](outcome[1])
        return result

    def logout(self, token: str, actor: str = "", ip: str = "") -> None:
        with self.tx(immediate=True) as c:
            _, sessions, audit, _ = self._repos(c)
            sessions.delete(security.token_hash(token))
            audit.insert(time.time(), "user", actor or "(unknown)", "logout", ip=ip)

    def resolve_session(self, token: str) -> dict | None:
        """返回会话对应的用户（含 role），失效返回 None。"""
        if not token:
            return None
        now = time.time()
        with self.tx(immediate=True) as c:
            users, sessions, _, _ = self._repos(c)
            row = sessions.get(security.token_hash(token))
            if not row or row["expires_at"] <= now:
                return None
            u = users.get(row["user_id"])
            if not u or u["status"] != "active":
                return None
            out = _public_user(u)
            return out

    # ---------- 权限 ----------
    @staticmethod
    def has_permission(user: dict, perm: str) -> bool:
        return perm in PERMISSIONS.get(user.get("role", ""), set())

    @staticmethod
    def require(user: dict, perm: str) -> None:
        if not SecurityService.has_permission(user, perm):
            raise PermissionDeniedError("权限不足：需要 %s" % perm)

    @staticmethod
    def is_admin(user: dict) -> bool:
        return user.get("role") == "admin"

    # ---------- 数据隔离 ----------
    def owned_agent_names(self, user: dict) -> list[str] | None:
        """admin 返回 None 表示不限制；否则返回该用户拥有的 agent 名列表。"""
        if self.is_admin(user):
            return None
        with self.tx() as c:
            agents = AgentRepo(c)
            rows = agents.list_by_owner(user["id"])
            return [r["name"] for r in rows]

    def assign_agent_owner(self, agent_name: str, user: dict) -> None:
        if self.is_admin(user):
            return  # admin 配对不归属个人（管理员全局）
        with self.tx(immediate=True) as c:
            AgentRepo(c).set_owner(agent_name, user["id"])
