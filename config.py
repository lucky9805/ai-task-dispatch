"""集中配置：全部来自环境变量，启动时校验、快速失败。

零第三方依赖。所有模块从 config.load() 拿配置，不各自读 os.environ。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Config:
    db_path: Path
    token: str                      # 管理面令牌，空 = 本地免鉴权
    lease_seconds: float            # 租约超时后任务回收重派
    online_window: float            # 多久内有活动算在线
    pair_ttl: float                 # 配对码有效期
    host: str
    port: int
    retries: int = 3                # 客户端 HTTP 重试次数（4xx 不重试）

    def validate(self) -> "Config":
        errors = []
        if self.lease_seconds <= 0:
            errors.append("DISPATCH_LEASE 必须 > 0")
        if self.online_window <= 0:
            errors.append("DISPATCH_ONLINE_WINDOW 必须 > 0")
        if self.pair_ttl <= 0:
            errors.append("DISPATCH_PAIR_TTL 必须 > 0")
        if not (1 <= self.port <= 65535):
            errors.append("DISPATCH_PORT 必须在 1-65535")
        if self.retries < 0:
            errors.append("DISPATCH_RETRIES 不能为负")
        if errors:
            raise ValueError("配置校验失败: " + "; ".join(errors))
        return self


def load(environ: dict | None = None) -> Config:
    """从环境变量构造配置。environ 参数便于测试注入，不影响真实 os.environ。"""
    env = os.environ if environ is None else environ
    db_raw = env.get("DISPATCH_DB", str(BASE_DIR / "dispatch.db"))
    cfg = Config(
        db_path=Path(db_raw),
        token=env.get("DISPATCH_TOKEN", ""),
        lease_seconds=_float_env(env, "DISPATCH_LEASE", "120"),
        online_window=_float_env(env, "DISPATCH_ONLINE_WINDOW", "30"),
        pair_ttl=_float_env(env, "DISPATCH_PAIR_TTL", "600"),
        host=env.get("DISPATCH_HOST", "127.0.0.1"),
        port=_int_env(env, "DISPATCH_PORT", "8765"),
        retries=_int_env(env, "DISPATCH_RETRIES", "3"),
    )
    return cfg.validate()


def _float_env(env, name, default):
    raw = env.get(name, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError("配置 %s 必须是数字，当前=%r" % (name, raw))


def _int_env(env, name, default):
    raw = env.get(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError("配置 %s 必须是整数，当前=%r" % (name, raw))
