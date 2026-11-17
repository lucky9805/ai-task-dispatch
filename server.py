"""任务下发中心 · HTTP 控制器层（薄）。

职责仅：路由 / 参数解析 / 鉴权 / 调用 service / 格式化响应。
业务规则在 service.py 与 auth_service.py，数据访问在 repository.py，配置在 config.py。

鉴权三路：
  1) X-Dispatch-Token == TOKEN  -> service token，视为 admin（兼容 ctl.py / 旧测试 / 脚本）
  2) Session cookie / Authorization: Bearer -> 用户会话，走 RBAC + 数据隔离
  3) 匿名 -> 仅 ALLOW_ANON（测试 / --allow-anon）放行；生产 main() 强制关闭并 bootstrap admin

Agent 面（pair / claim / progress / heartbeat / complete / fail）继续用 X-Agent-Token。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config
import db as db_mod
import logging_util as log
import security
import selftest as selftest_mod
from auth_service import SecurityService
from errors import AuthError, DispatchError, ValidationError, to_http
from service import Services

BASE_DIR = Path(__file__).resolve().parent

# ---- 模块级配置（可被测试覆盖，保持向后兼容）----
DB_PATH = Path(os.environ.get("DISPATCH_DB", str(BASE_DIR / "dispatch.db")))
TOKEN = os.environ.get("DISPATCH_TOKEN", "")
LEASE_SECONDS = float(os.environ.get("DISPATCH_LEASE", "120"))
ONLINE_WINDOW = float(os.environ.get("DISPATCH_ONLINE_WINDOW", "30"))
PAIR_TTL = float(os.environ.get("DISPATCH_PAIR_TTL", "600"))
ALLOW_ANON = True            # main() 在生产会置 False；测试直接起 Handler 时保持 True
ALLOWED_CIDRS: list[str] = []  # 空 = 不限制
SESSION_TTL = 8 * 3600
MAX_BODY = 1024 * 1024
SCHEME = "http"

_lock = threading.RLock()
_conn = None
_rate = security.RateLimiter()

# 兼容旧代码里 server.HOST / server.PORT 的引用
HOST = os.environ.get("DISPATCH_HOST", "127.0.0.1")
PORT = int(os.environ.get("DISPATCH_PORT", "8765"))


def db():
    """唯一的连接持有者；service / repository 通过注入拿它。"""
    global _conn
    if _conn is None:
        _conn = db_mod.connect(DB_PATH)
    return _conn


@contextmanager
def tx(immediate: bool = False):
    """串行事务。读改写（领取/回传/回收）必须 immediate=True。"""
    with _lock:
        c = db()
        if immediate:
            c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            if immediate:
                c.execute("COMMIT")
        except Exception:
            if immediate:
                c.execute("ROLLBACK")
            raise


def _services() -> Services:
    return Services(
        db(), tx,
        lease_seconds=LEASE_SECONDS,
        online_window=ONLINE_WINDOW,
        pair_ttl=PAIR_TTL,
    )


def _security() -> SecurityService:
    return SecurityService(db(), tx, session_ttl=SESSION_TTL)


# ---- 兼容转发层：ctl.py 与既有测试直接调用这些符号 ----

def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db_mod.init_db(db())


def create_pairing_code(name: str, ttl: float | None = None) -> str:
    return _services().pairing.create_code(name, ttl)


def redeem_pairing_code(name: str, code: str) -> str:
    return _services().pairing.redeem(name, code)


def revoke_agent(name: str) -> bool:
    return _services().pairing.revoke(name)


def list_agents() -> list[dict]:
    return _services().agents.list()


def archive_agent(name: str, archived: bool = True) -> dict:
    return _services().agents.set_archived(name, archived)


def set_agent_category(name: str, category: str) -> dict:
    return _services().agents.set_category(name, category)


def create_task(title: str, assignee: str, payload: dict) -> dict:
    return _services().tasks.create(title, assignee, payload)


def list_tasks(status=None, assignee=None) -> list[dict]:
    return _services().tasks.list(status, assignee)


def get_task(task_id: int) -> dict | None:
    return _services().tasks.get(task_id)


# ---- HTTP ----

AGENT_ACTIONS = {"progress", "heartbeat", "complete", "fail"}
SESSION_COOKIE = "dispatch_session"


def _is_agent_endpoint(path: str) -> bool:
    """Agent 面用配对凭证鉴权，不要求管理面令牌。"""
    if path in ("/api/pair", "/api/tasks/claim"):
        return True
    parts = path.strip("/").split("/")
    return (
        len(parts) == 4 and parts[0] == "api" and parts[1] == "tasks"
        and parts[3] in AGENT_ACTIONS
    )


class DispatchServer(ThreadingHTTPServer):
    request_queue_size = 128  # 默认 5，多个 Agent 同时轮询会被拒连
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "TaskDispatch"   # 不暴露具体版本号
    sys_version = ""                  # 不暴露 Python 版本

    def log_message(self, fmt, *args):
        log.info("http_access", getattr(self, "_rid", ""), message=fmt % args)

    def _send(self, status: int, obj, ctype: str = "application/json; charset=utf-8",
              extra_headers: dict | None = None):
        body = obj if isinstance(obj, bytes) else json.dumps(
            obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 收紧 CORS：同源即可，不再 Access-Control-Allow-Origin: *
        self.send_header("Vary", "Origin")
        if SCHEME == "https":
            self.send_header("Strict-Transport-Security",
                             "max-age=31536000; includeSubDomains")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # 管理面板是单文件内联脚本（<script>...</script>），script-src 必须允许
        # 'unsafe-inline'，否则浏览器拒绝执行全部 JS —— 登录框点了没反应。
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValidationError("请求体过大（上限 %d 字节）" % MAX_BODY)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValidationError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    # ---------- 认证上下文 ----------
    def _client_ip(self) -> str:
        try:
            return self.client_address[0]
        except Exception:
            return ""

    def _session_token(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                return v
        return ""

    def _auth_context(self):
        """返回 (user_dict, is_service) 或 None。不抛异常，由调用方决定 401。"""
        svc_token = self.headers.get("X-Dispatch-Token", "")
        if svc_token:
            if TOKEN and security.safe_equal(svc_token, TOKEN):
                return {"id": 0, "username": "service", "role": "admin"}, True
            if not TOKEN and ALLOW_ANON:
                return {"id": 0, "username": "anon", "role": "admin"}, True
        sess = self._session_token()
        if sess:
            user = _security().resolve_session(sess)
            if user:
                return user, False
        if ALLOW_ANON and not TOKEN:
            return {"id": 0, "username": "anon", "role": "admin"}, True
        return None

    def _require_user(self, perm: str | None = None):
        ctx = self._auth_context()
        if not ctx:
            raise AuthError("需要登录或提供有效令牌")
        user, is_service = ctx
        if perm and not is_service:
            SecurityService.require(user, perm)
        self._user = user
        self._is_service = is_service
        return user

    def _gate(self) -> bool:
        """IP 白名单 + 全局限速。返回 False 表示已发响应。"""
        ip = self._client_ip()
        if not security.ip_allowed(ip, ALLOWED_CIDRS):
            self._send(403, {"error": "来源 IP 不在白名单", "code": "forbidden"})
            return False
        if not _rate.allow("global:" + ip, 600, 60):
            self._send(429, {"error": "请求过于频繁", "code": "rate_limited"})
            return False
        return True

    def _audit(self, action: str, *, target: str = "", result: str = "ok",
               detail: str = "", actor: str | None = None):
        try:
            u = getattr(self, "_user", None)
            _security().audit("user" if u else "system",
                              actor or (u.get("username") if u else "system"),
                              action, target=target, ip=self._client_ip(),
                              result=result, detail=detail,
                              request_id=getattr(self, "_rid", ""))
        except Exception:
            pass  # 审计失败不影响主流程

    def _scope_names(self) -> list[str] | None:
        """当前用户可见的 agent 名；admin/service 返回 None 表示不限制。"""
        u = getattr(self, "_user", {})
        if getattr(self, "_is_service", False) or SecurityService.is_admin(u):
            return None
        return _security().owned_agent_names(u)

    def _can_agent(self, name: str) -> bool:
        names = self._scope_names()
        return names is None or name in names

    # ---------- 路由 ----------
    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        self._rid = log.new_request_id()
        if not self._gate():
            return
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            try:
                return self._send(200, (BASE_DIR / "index.html").read_bytes(),
                                  "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(404, {"error": "index.html 不存在"})
        if path == "/api/health":
            return self._send(200, {"ok": True, "lease_seconds": LEASE_SECONDS,
                                    "pair_ttl": PAIR_TTL,
                                    "auth_required": not ALLOW_ANON})
        try:
            self._require_user()
            if path == "/api/me":
                return self._send(200, {"user": self._user,
                                        "is_service": self._is_service})
            if path == "/api/selftest":
                SecurityService.require(self._user, "selftest.run") if not self._is_service else None
                return self._send(200, selftest_mod.run_all())
            if path == "/api/agents":
                agents = list_agents()
                scoped = self._scope_names()
                if scoped is not None:
                    agents = [a for a in agents if a["name"] in scoped]
                return self._send(200, {"agents": agents,
                                        "online_window": ONLINE_WINDOW})
            if path == "/api/tasks":
                status = (qs.get("status") or [None])[0]
                assignee = (qs.get("assignee") or [None])[0]
                tasks = list_tasks(status, assignee)
                scoped = self._scope_names()
                if scoped is not None:
                    tasks = [t for t in tasks if t.get("assignee") in scoped]
                return self._send(200, {"tasks": tasks})
            if path.startswith("/api/tasks/"):
                try:
                    task = get_task(int(path.rsplit("/", 1)[1]))
                except ValueError:
                    return self._send(404, {"error": "任务不存在", "code": "not_found"})
                if not task:
                    return self._send(404, {"error": "任务不存在", "code": "not_found"})
                if not self._can_agent(task.get("assignee", "")):
                    raise __import__("errors").PermissionDeniedError("无权查看该任务")
                return self._send(200, {"task": task})
            if path == "/api/users":
                SecurityService.require(self._user, "user.manage") if not self._is_service else None
                return self._send(200, {"users": _security().list_users()})
            if path == "/api/audit":
                SecurityService.require(self._user, "audit.view") if not self._is_service else None
                limit = int((qs.get("limit") or [50])[0])
                return self._send(200, {"events": _security().list_audit(
                    limit, (qs.get("actor") or [None])[0], (qs.get("action") or [None])[0])})
            return self._send(404, {"error": "未知路径", "code": "not_found"})
        except DispatchError as e:
            return self._dispatch_error(e)
        except Exception as e:
            return self._dispatch_error(e)

    def do_POST(self):
        self._rid = log.new_request_id()
        if not self._gate():
            return
        path = urlparse(self.path).path
        try:
            body = self._body()
        except DispatchError as e:
            return self._dispatch_error(e)

        # 登录：唯一不需要管理面凭证的管理端点
        if path == "/api/login":
            if not _rate.allow("login:" + self._client_ip(), 10, 60):
                return self._send(429, {"error": "登录尝试过于频繁", "code": "rate_limited"})
            try:
                res = _security().login(body.get("username", ""), body.get("password", ""),
                                        ip=self._client_ip())
                cookie = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict%s" % (
                    SESSION_COOKIE, res["token"], res["expires_in"],
                    "; Secure" if SCHEME == "https" else "")
                return self._send(200, {"user": res["user"], "expires_in": res["expires_in"]},
                                  extra_headers={"Set-Cookie": cookie})
            except DispatchError as e:
                return self._dispatch_error(e)

        if not _is_agent_endpoint(path):
            try:
                self._require_user()
            except DispatchError as e:
                return self._dispatch_error(e)

        svc = _services()
        agent_token = self.headers.get("X-Agent-Token", "")
        try:
            # ---- Agent 面 ----
            if path == "/api/pair":
                ip = self._client_ip()
                if not _rate.allow("pair:" + ip, 100, 60):
                    return self._send(429, {"error": "配对过于频繁", "code": "rate_limited"})
                token = svc.pairing.redeem(body.get("name", ""), body.get("code", ""),
                                           ip=ip)
                self._audit("agent.pair.redeem", target=str(body.get("name", "")).strip(),
                            actor=str(body.get("name", "")).strip())
                return self._send(200, {"agent_id": str(body.get("name", "")).strip(),
                                        "token": token})

            if path == "/api/tasks/claim":
                task = svc.tasks.claim(body.get("agent_id", ""), agent_token,
                                       ip=self._client_ip())
                return self._send(200, {"task": task})

            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "tasks":
                try:
                    tid = int(parts[2])
                except ValueError:
                    return self._send(404, {"error": "任务不存在", "code": "not_found"})
                action, agent_id = parts[3], body.get("agent_id", "")
                if action == "progress":
                    ok = svc.tasks.progress(tid, agent_id, agent_token,
                                            body.get("progress", 0), body.get("message", ""))
                elif action == "heartbeat":
                    ok = svc.tasks.heartbeat(tid, agent_id, agent_token)
                elif action == "complete":
                    ok = svc.tasks.finish(tid, agent_id, agent_token, True,
                                          str(body.get("result", "")))
                elif action == "fail":
                    ok = svc.tasks.finish(tid, agent_id, agent_token, False,
                                          str(body.get("error", "")))
                else:
                    return self._send(404, {"error": "未知操作", "code": "not_found"})
                return self._send(200, {"ok": True}) if ok else self._send(
                    409, {"error": "任务不属于该 Agent 或凭证无效", "code": "conflict"})

            # ---- 管理面 ----
            if path == "/api/logout":
                sess = self._session_token()
                actor = self._user.get("username") if getattr(self, "_user", None) else ""
                _security().logout(sess, actor=actor, ip=self._client_ip())
                return self._send(200, {"ok": True}, extra_headers={
                    "Set-Cookie": "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" % SESSION_COOKIE})

            if path == "/api/agents/pair":
                SecurityService.require(self._user, "agent.pair") if not self._is_service else None
                name = str(body.get("name", "")).strip()
                if not self._can_agent(name) and not self._is_service and not SecurityService.is_admin(self._user):
                    raise __import__("errors").PermissionDeniedError("无权为该 Agent 配对")
                code = svc.pairing.create_code(name, body.get("ttl"),
                                               created_by=(self._user.get("username") if getattr(self, "_user", None) else ""),
                                               created_ip=self._client_ip())
                if not self._is_service:
                    _security().assign_agent_owner(name, self._user)
                self._audit("agent.pair.create", target=name)
                return self._send(201, {"agent_id": name, "code": code,
                                        "ttl": int(body.get("ttl") if body.get("ttl") is not None else PAIR_TTL)})

            if path == "/api/agents/revoke":
                SecurityService.require(self._user, "agent.revoke") if not self._is_service else None
                name = str(body.get("name", "")).strip()
                if not self._can_agent(name) and not self._is_service and not SecurityService.is_admin(self._user):
                    raise __import__("errors").PermissionDeniedError("无权撤销该 Agent")
                if not svc.pairing.revoke(name):
                    return self._send(404, {"error": "Agent「%s」不存在" % name, "code": "not_found"})
                self._audit("agent.revoke", target=name)
                return self._send(200, {"ok": True, "agent_id": name})

            if path == "/api/agents/archive":
                SecurityService.require(self._user, "agent.manage") if not self._is_service else None
                name = str(body.get("name", "")).strip()
                if not self._can_agent(name) and not self._is_service and not SecurityService.is_admin(self._user):
                    raise __import__("errors").PermissionDeniedError("无权管理该 Agent")
                archived = bool(body.get("archived", True))
                try:
                    result = svc.agents.set_archived(name, archived)
                except ValidationError as e:
                    return self._send(404, {"error": str(e), "code": "not_found"})
                self._audit("agent.archive" if archived else "agent.unarchive", target=name)
                return self._send(200, {"ok": True, **result})

            if path == "/api/agents/category":
                SecurityService.require(self._user, "agent.manage") if not self._is_service else None
                name = str(body.get("name", "")).strip()
                if not self._can_agent(name) and not self._is_service and not SecurityService.is_admin(self._user):
                    raise __import__("errors").PermissionDeniedError("无权管理该 Agent")
                try:
                    result = svc.agents.set_category(name, str(body.get("category", "")))
                except ValidationError as e:
                    return self._send(404, {"error": str(e), "code": "not_found"})
                self._audit("agent.category", target=name, detail="category=" + result["category"])
                return self._send(200, {"ok": True, **result})

            if path == "/api/tasks":
                SecurityService.require(self._user, "task.create") if not self._is_service else None
                assignee = str(body.get("assignee", "")).strip()
                if not self._can_agent(assignee):
                    raise __import__("errors").PermissionDeniedError("无权向该 Agent 下发任务")
                created_by = self._user.get("username") if getattr(self, "_user", None) else ""
                task = svc.tasks.create(body.get("title", ""), assignee,
                                        body.get("payload") or {}, created_by=created_by)
                self._audit("task.create", target=assignee, detail="#%s" % task["id"])
                return self._send(201, {"task": task})

            # 用户管理
            if path == "/api/users":
                SecurityService.require(self._user, "user.manage") if not self._is_service else None
                u = _security().create_user(
                    body.get("username", ""), body.get("password", ""),
                    body.get("role", "operator"), body.get("display_name", ""),
                    actor=self._user.get("username", ""), ip=self._client_ip())
                self._audit("user.create", target=u["username"])
                return self._send(201, {"user": u})

            if path.startswith("/api/users/"):
                SecurityService.require(self._user, "user.manage") if not self._is_service else None
                try:
                    uid = int(path.rsplit("/", 1)[1])
                except ValueError:
                    return self._send(404, {"error": "用户不存在", "code": "not_found"})
                action = body.get("op", "update")
                if action == "delete":
                    _security().delete_user(uid, actor=self._user.get("username", ""), ip=self._client_ip())
                    self._audit("user.delete", target=str(uid))
                    return self._send(200, {"ok": True})
                u = _security().update_user(
                    uid, role=body.get("role"), status=body.get("status"),
                    display_name=body.get("display_name"), password=body.get("password"),
                    actor=self._user.get("username", ""), ip=self._client_ip())
                self._audit("user.update", target=u["username"])
                return self._send(200, {"user": u})

            return self._send(404, {"error": "未知路径", "code": "not_found"})
        except DispatchError as e:
            if e.http_status >= 400 and getattr(e, "code", "") in ("forbidden", "unauthorized"):
                self._audit("access.denied", result="denied", detail=e.message)
            return self._dispatch_error(e)
        except Exception as e:
            return self._dispatch_error(e)

    def _dispatch_error(self, exc: Exception):
        status, body = to_http(exc)
        if status >= 500:
            log.error("unhandled_error", getattr(self, "_rid", ""), message=str(exc))
        self._send(status, body)


def local_ips():
    """尽力探测本机可被其他机器访问的 IPv4（纯标准库，失败返回空列表）。"""
    found = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            found.add(ip)
    except OSError:
        pass
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                found.add(ip)
    except OSError:
        pass
    return sorted(found)


def print_access_hint(host: str, port: int) -> None:
    """启动时打印接入提示：地址、鉴权状态、初始管理员密码。"""
    global SCHEME
    loopback = host in ("127.0.0.1", "localhost", "::1", "")
    ips = local_ips()
    scheme = "https" if SCHEME == "https" else "http"
    print("=" * 68, flush=True)
    if loopback:
        print("[中心] 监听 %s:%d —— 只有本机可访问。" % (host or "127.0.0.1", port), flush=True)
        print("[中心] 跨机器接入：改用 --host 0.0.0.0，并放行防火墙端口。", flush=True)
        if ips:
            print("[中心] Agent 配对时 --server 填：%s://%s:%d" % (scheme, ips[0], port), flush=True)
        else:
            print("[中心] Agent 配对时 --server 填 %s://<本机IP>:%d" % (scheme, port), flush=True)
    else:
        shown = host if host != "0.0.0.0" else ((ips or ["<本机IP>"])[0])
        print("[中心] 监听 %s:%d，接入地址：%s://%s:%d" % (host, port, scheme, shown, port), flush=True)
    if ALLOW_ANON:
        print("[安全][警告] 当前为匿名模式（--allow-anon），管理 API 无需登录！", flush=True)
        print("[安全][警告] 生产环境务必去掉 --allow-anon 并设置 DISPATCH_TOKEN。", flush=True)
    else:
        print("[安全] 管理面已强制鉴权：用浏览器登录，或设置 DISPATCH_TOKEN 供脚本调用。", flush=True)
    print("[安全] 密码哈希后端：%s；接入白名单：%s" % (
        security.password_backend(), ALLOWED_CIDRS or "不限制"), flush=True)
    print("=" * 68, flush=True)


def main():
    global ALLOW_ANON, ALLOWED_CIDRS, SCHEME, HOST, PORT
    ap = argparse.ArgumentParser(description="任务下发中心（安全加固版）")
    ap.add_argument("--host", default=os.environ.get("DISPATCH_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("DISPATCH_PORT", "8765")))
    ap.add_argument("--allow-anon", action="store_true",
                    help="允许管理 API 匿名访问（仅开发/测试；生产禁用）")
    ap.add_argument("--allow-cidr", action="append", default=[],
                    help="来源 IP 白名单（CIDR，可多次；默认不限制）")
    ap.add_argument("--cert", default=os.environ.get("DISPATCH_TLS_CERT", ""),
                    help="TLS 证书 PEM；与 --key 同时提供则启用 HTTPS")
    ap.add_argument("--key", default=os.environ.get("DISPATCH_TLS_KEY", ""),
                    help="TLS 私钥 PEM")
    args = ap.parse_args()

    cfg = config.load({
        **os.environ,
        "DISPATCH_HOST": args.host,
        "DISPATCH_PORT": str(args.port),
    })
    HOST, PORT = cfg.host, cfg.port
    ALLOW_ANON = args.allow_anon
    ALLOWED_CIDRS = args.allow_cidr
    init_db()

    # 非匿名模式下，只要没有任何用户就创建初始管理员。
    # 注意：service token 只服务脚本调用，Web 登录页仍需真实用户账号，
    # 因此不能因为设置了 TOKEN 就跳过 bootstrap，否则没人能登录管理界面。
    if not ALLOW_ANON:
        boot = _security().bootstrap_admin()
        if boot:
            u, p = boot
            print("=" * 68, flush=True)
            print("[安全] 已创建初始管理员，请立即登录 Web 修改密码：", flush=True)
            print("       用户名：%s" % u, flush=True)
            print("       密码  ：%s   （仅显示这一次）" % p, flush=True)
            print("=" * 68, flush=True)

    httpd = DispatchServer((args.host, args.port), Handler)
    if args.cert and args.key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.cert, args.key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        SCHEME = "https"
    log.info("server_started", host=args.host, port=args.port,
             db=str(DB_PATH), token_enabled=bool(TOKEN), anon_allowed=ALLOW_ANON,
             tls=(SCHEME == "https"), password_backend=security.password_backend())
    print_access_hint(args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("server_stopping")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
