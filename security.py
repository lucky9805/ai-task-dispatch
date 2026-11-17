"""安全工具：密码哈希、会话令牌、限速器、IP 白名单、配对码生成。

设计原则：
- 密码哈希优先用成熟组件 bcrypt；缺失时回退标准库 scrypt（仍然安全，只是非 bcrypt）。
- 所有令牌/凭证只以哈希形式落库；比对一律常量时间。
- 零第三方依赖也能跑（bcrypt 可选），兼顾部署稳健。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import threading
import time

# ---- 密码哈希：bcrypt 优先，回退 scrypt ----
try:
    import bcrypt as _bcrypt
    _HAS_BCRYPT = True
except ImportError:  # pragma: no cover - 取决于部署环境
    _bcrypt = None
    _HAS_BCRYPT = False

_SCRYPT_N = 2 ** 14  # 16384，OWASP 推荐量级
_SCRYPT_R = 8
_SCRYPT_P = 1
# 三级回退：bcrypt -> scrypt -> pbkdf2_hmac。
# macOS 系统自带 Python(3.9) 既无 bcrypt 也无 hashlib.scrypt，
# 但 hashlib.pbkdf2_hmac 一定存在，用它兜底保证任何环境都能改密码。
_HAS_SCRYPT = hasattr(hashlib, "scrypt")
_PBKDF2_ITERS = 600_000  # OWASP 推荐量级


def hash_password(password: str) -> str:
    """返回带算法前缀的哈希串：bcrypt$/scrypt$/pbkdf2$。"""
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("密码至少 8 个字符")
    if _HAS_BCRYPT:
        raw = _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt())
        return "bcrypt$" + raw.decode("ascii")
    salt = secrets.token_bytes(16)
    if _HAS_SCRYPT:
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
        return "scrypt$%s$%s" % (base64.b64encode(salt).decode(),
                                 base64.b64encode(dk).decode())
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS)
    return "pbkdf2$%d$%s$%s" % (_PBKDF2_ITERS, base64.b64encode(salt).decode(),
                                 base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    if not stored or not isinstance(password, str):
        return False
    try:
        if stored.startswith("bcrypt$"):
            if not _HAS_BCRYPT:
                return False
            return _bcrypt.checkpw(password.encode("utf-8"),
                                   stored[len("bcrypt$"):].encode("ascii"))
        if stored.startswith("scrypt$"):
            if not _HAS_SCRYPT:
                return False
            _, salt_b64, hash_b64 = stored.split("$", 2)
            salt = base64.b64decode(salt_b64)
            dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
            return hmac.compare_digest(dk, base64.b64decode(hash_b64))
        if stored.startswith("pbkdf2$"):
            _, iters, salt_b64, hash_b64 = stored.split("$", 3)
            salt = base64.b64decode(salt_b64)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
            return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:
        return False
    return False


def password_backend() -> str:
    if _HAS_BCRYPT:
        return "bcrypt"
    if _HAS_SCRYPT:
        return "scrypt(stdlib)"
    return "pbkdf2-hmac(stdlib)"


def validate_password_strength(password: str) -> str | None:
    """返回 None 表示通过，否则返回错误说明。"""
    if not isinstance(password, str) or len(password) < 8:
        return "密码至少 8 个字符"
    if len(password) > 128:
        return "密码不能超过 128 个字符"
    kinds = sum(bool(_c) for _c in (
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    ))
    if kinds < 2:
        return "密码需至少包含两类字符（大小写字母/数字/符号）"
    return None


# ---- 令牌 ----
def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def safe_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a or "", b or "")


# ---- 配对码：默认 12 位，分段易读 ----
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_pair_code(groups: int = 3, group_len: int = 4) -> str:
    return "-".join(
        "".join(secrets.choice(PAIR_ALPHABET) for _ in range(group_len))
        for _ in range(groups)
    )


# ---- IP / CIDR 白名单 ----
def ip_allowed(ip: str, cidrs: list[str] | None) -> bool:
    """cidrs 为空/None 表示不限制；否则 IP 需命中任一 CIDR 或单 IP。"""
    if not cidrs:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in cidrs:
        item = (item or "").strip()
        if not item:
            continue
        try:
            if addr in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            continue
    return False


# ---- 限速器：滑动窗口，按 key 计数 ----
class RateLimiter:
    def __init__(self):
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.time()
        with self._lock:
            bucket = self._hits.setdefault(key, [])
            cutoff = now - window
            # 丢弃过期命中
            i = 0
            while i < len(bucket) and bucket[i] <= cutoff:
                i += 1
            if i:
                del bucket[:i]
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


# ---- 安全响应头 ----
def security_headers(scheme: str = "http") -> dict[str, str]:
    h = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }
    if scheme == "https":
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return h
