"""业务层：配对 / 任务调度 / Agent 列表。纯业务，不依赖 HTTP。

事务通过构造时注入的 tx(immediate=...) 上下文管理；服务层不持有全局连接或锁。
业务错误抛 errors.py 的类型化异常，由控制器统一映射成 HTTP 响应。
"""
from __future__ import annotations

import hashlib
import secrets
import time

from errors import AuthError, ValidationError
from repository import AgentRepo, PairingRepo, TaskRepo

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混字符
VALID_STATUS = {"pending", "running", "succeeded", "failed"}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _BaseService:
    def __init__(self, conn, tx, *, lease_seconds: float, online_window: float,
                 pair_ttl: float, access_max_age: float | None = None):
        self.conn = conn
        self.tx = tx
        self.lease_seconds = lease_seconds
        self.online_window = online_window
        self.pair_ttl = pair_ttl
        self._access_max_age = access_max_age

    def _repos(self, c):
        return AgentRepo(c), TaskRepo(c), PairingRepo(c)


class PairingService(_BaseService):
    def create_code(self, name: str, ttl: float | None = None, *,
                    created_by: str = "", created_ip: str = "") -> str:
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("Agent 名不能为空")
        name = name.strip()
        # 优先用安全模块生成的 12 位配对码；缺失时回退旧的 8 位算法
        try:
            import security as _sec
            code = _sec.new_pair_code()
        except Exception:
            code = "-".join(
                "".join(secrets.choice(CODE_ALPHABET) for _ in range(4)) for _ in range(2)
            )
        now = time.time()
        with self.tx(immediate=True) as c:
            agents, _, pairing = self._repos(c)
            pairing.insert(_hash(code), name, now + (ttl if ttl is not None else self.pair_ttl),
                           now, created_by, created_ip)
            agents.register(name, now)
        return code

    def redeem(self, name: str, code: str, *, ip: str = "") -> str:
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("Agent 名不能为空")
        if not isinstance(code, str) or not code.strip():
            raise ValidationError("配对码不能为空")
        name, code = name.strip(), code.strip().upper()
        now = time.time()
        with self.tx(immediate=True) as c:
            agents, _, pairing = self._repos(c)
            row = pairing.get_by_hash(_hash(code))
            if row is None:
                raise ValidationError("配对码无效")
            if row["used"]:
                raise ValidationError("配对码已被使用")
            if row["expires_at"] < now:
                raise ValidationError("配对码已过期，请在服务端重新生成")
            if row["name"] != name:
                raise ValidationError("配对码与 Agent 名不匹配")
            agent = agents.get(name)
            if agent is not None and agent["approval"] == "pending":
                raise ValidationError("该 Agent 尚未通过管理员审批")
            # 来源 IP 白名单（allowed_ips 为空表示不限制）
            if ip and agent is not None and agent["allowed_ips"]:
                import security as _sec
                if not _sec.ip_allowed(ip, [x for x in agent["allowed_ips"].split(",") if x]):
                    raise AuthError("来源 IP 不在该 Agent 的白名单内")
            token = secrets.token_urlsafe(32)
            pairing.mark_used(_hash(code))
            agents.set_token(name, _hash(token), now, ip=ip,
                             expires_at=(now + self._access_max_age) if self._access_max_age else None)
            return token

    def revoke(self, name: str) -> bool:
        with self.tx(immediate=True) as c:
            agents, _, _ = self._repos(c)
            return agents.revoke(name.strip())

    def check_token(self, name: str, token: str) -> bool:
        if not name or not token:
            return False
        with self.tx() as c:
            agents, _, _ = self._repos(c)
            row = agents.get(name)
            return bool(
                row and not row["revoked"] and row["token_hash"]
                and secrets.compare_digest(row["token_hash"], _hash(token))
            )


class TaskService(_BaseService):
    def create(self, title: str, assignee: str, payload: dict, *,
               created_by: str = "") -> dict:
        if not isinstance(title, str) or not title.strip():
            raise ValidationError("title 不能为空")
        if not isinstance(assignee, str) or not assignee.strip():
            raise ValidationError("必须指定 assignee：这个任务下发给哪个 Agent")
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是 JSON 对象")
        who = assignee.strip()
        with self.tx(immediate=True) as c:
            agents, tasks, _ = self._repos(c)
            if agents.get(who) is None:
                raise ValidationError(
                    "Agent「%s」未在服务端登记，请先执行 ctl.py pair %s" % (who, who))
            return tasks.insert(title.strip(), who, payload, time.time(), created_by)

    def list(self, status: str | None, assignee: str | None) -> list[dict]:
        if status and status not in VALID_STATUS:
            raise ValidationError("status 无效，可选 pending/running/succeeded/failed")
        with self.tx(immediate=True) as c:
            _, tasks, _ = self._repos(c)
            tasks.reclaim_expired(time.time(), self.lease_seconds)
            return tasks.list(status, assignee)

    def get(self, task_id: int) -> dict | None:
        with self.tx() as c:
            _, tasks, _ = self._repos(c)
            return tasks.get(task_id)

    def claim(self, agent_id: str, token: str, *, ip: str = "") -> dict | None:
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValidationError("agent_id 不能为空")
        name = agent_id.strip()
        now = time.time()
        with self.tx(immediate=True) as c:
            agents, tasks, _ = self._repos(c)
            row = agents.get(name)
            if not (row and not row["revoked"] and row["token_hash"]
                    and secrets.compare_digest(row["token_hash"], _hash(token))):
                raise AuthError("Agent 未配对或凭证无效，请先在客户端执行 pair")
            if row["archived"]:
                raise AuthError("Agent 已归档，无法领取任务；请先取消归档")
            if row["token_expires_at"] and row["token_expires_at"] <= now:
                raise AuthError("Agent 凭证已过期，请重新配对")
            if ip and row["allowed_ips"]:
                import security as _sec
                if not _sec.ip_allowed(ip, [x for x in row["allowed_ips"].split(",") if x]):
                    raise AuthError("来源 IP 不在该 Agent 的白名单内")
            tasks.reclaim_expired(now, self.lease_seconds)
            candidate = tasks.claim_candidate(name)
            if candidate is None:
                agents.touch(name, now)
                return None
            task = tasks.mark_running(candidate["id"], name, now)
            agents.inc_claimed(name, now)
            return task

    def _owned(self, c, task_id: int, agent_id: str, token: str) -> bool:
        agents, tasks, _ = self._repos(c)
        row = agents.get(agent_id)
        token_ok = bool(
            row and not row["revoked"] and row["token_hash"]
            and secrets.compare_digest(row["token_hash"], _hash(token))
        )
        return token_ok and tasks.owned(task_id, agent_id)

    def progress(self, task_id, agent_id, token, value, message) -> bool:
        with self.tx(immediate=True) as c:
            if not self._owned(c, task_id, agent_id, token):
                return False
            _, tasks, _ = self._repos(c)
            p = max(0, min(100, int(value)))
            tasks.set_progress(task_id, p, str(message)[:1000], time.time())
            return True

    def heartbeat(self, task_id, agent_id, token) -> bool:
        with self.tx(immediate=True) as c:
            if not self._owned(c, task_id, agent_id, token):
                return False
            _, tasks, _ = self._repos(c)
            tasks.heartbeat(task_id, time.time())
            return True

    def finish(self, task_id, agent_id, token, succeeded: bool, text: str) -> bool:
        with self.tx(immediate=True) as c:
            if not self._owned(c, task_id, agent_id, token):
                return False
            _, tasks, _ = self._repos(c)
            tasks.finish(task_id, succeeded, text, time.time())
            return True


class AgentService(_BaseService):
    def list(self) -> list[dict]:
        now = time.time()
        with self.tx() as c:
            agents, _, _ = self._repos(c)
            out = []
            for r in agents.list_all():
                revoked = bool(r["revoked"])
                archived = bool(r["archived"])
                paired = bool(r["token_hash"]) and not revoked
                out.append({
                    "name": r["name"],
                    "paired": paired,
                    "revoked": revoked,
                    "archived": archived,
                    "archived_at": r["archived_at"],
                    "category": r["category"] or "",
                    "paired_at": r["paired_at"],
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "claimed_count": r["claimed_count"],
                    "online": paired and not archived
                    and (now - r["last_seen"]) < self.online_window,
                })
            return out

    def set_archived(self, name: str, archived: bool) -> dict:
        """归档/取消归档。归档后停止接活（online=false），但凭证保留、可恢复。"""
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("Agent 名不能为空")
        name = name.strip()
        with self.tx(immediate=True) as c:
            agents, _, _ = self._repos(c)
            if agents.get(name) is None:
                raise ValidationError("Agent「%s」不存在" % name)
            agents.set_archived(name, archived, time.time())
            row = agents.get(name)
            return {"name": name, "archived": bool(row["archived"]),
                    "archived_at": row["archived_at"]}

    def set_category(self, name: str, category: str) -> dict:
        """设置 Agent 分类标签（空字符串表示清除分类）。"""
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("Agent 名不能为空")
        if not isinstance(category, str):
            raise ValidationError("分类必须是文本")
        name, category = name.strip(), category.strip()
        if len(category) > 32:
            raise ValidationError("分类标签不能超过 32 个字符")
        with self.tx(immediate=True) as c:
            agents, _, _ = self._repos(c)
            if agents.get(name) is None:
                raise ValidationError("Agent「%s」不存在" % name)
            agents.set_category(name, category)
            return {"name": name, "category": category}

    def list_categories(self) -> list[str]:
        with self.tx() as c:
            agents, _, _ = self._repos(c)
            return agents.list_categories()


class Services:
    """聚合入口：一次构造，拿到三个服务。"""

    def __init__(self, conn, tx, *, lease_seconds: float, online_window: float,
                 pair_ttl: float, access_max_age: float | None = None):
        common = dict(conn=conn, tx=tx, lease_seconds=lease_seconds,
                      online_window=online_window, pair_ttl=pair_ttl,
                      access_max_age=access_max_age)
        self.pairing = PairingService(**common)
        self.tasks = TaskService(**common)
        self.agents = AgentService(**common)
