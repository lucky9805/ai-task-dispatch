# task-dispatch 安全部署指南（混合环境）

> 配套《服务端安全加固规划.md》。本文只讲**怎么把加固后的服务安全地跑起来**。
> 两种启用 HTTPS 的方式：**A. 反向代理终止 TLS（推荐，生产首选）**；**B. 服务端内置 TLS**。

---

## 0. 生产启动 checklist（先记住）

1. **不要用 `--allow-anon`**（那是开发/测试专用，管理 API 会完全裸奔）。
2. **设置强健的 `DISPATCH_TOKEN`**（供 ctl.py / submit.py 脚本调用）或依赖首次启动生成的管理员账号。
3. 跨机器时 `--host 0.0.0.0`，但**必须**叠加 IP 白名单 `--allow-cidr` 或放在反代后面。
4. 管理面走 HTTPS；Agent 的 `--server` 填 `https://域名`（配对时写一次，存进凭证）。

---

## 1. 方式 A：反向代理终止 TLS（推荐）

中心服务只监听本机，TLS 由 Caddy / Nginx 处理。这样证书自动续期、端口只暴露 443。

### 1.1 启动中心（只监听本机）

```bash
export DISPATCH_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
export DISPATCH_DB=/var/lib/task-dispatch/dispatch.db
python3 server.py --host 127.0.0.1 --port 8765
# 首次启动会打印初始管理员账号密码（若 users 表为空），记下来登录 Web 改密
```

### 1.2 Caddy（最省事，自动申请 Let's Encrypt 证书）

`Caddyfile`：

```
dispatch.example.com {
    reverse_proxy 127.0.0.1:8765
    header {
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        Referrer-Policy "no-referrer"
    }
}
```

```bash
caddy run --config Caddyfile
```

### 1.3 Nginx

```nginx
server {
    listen 443 ssl http2;
    server_name dispatch.example.com;
    ssl_certificate     /etc/letsencrypt/live/dispatch.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/dispatch.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
server { listen 80; server_name dispatch.example.com; return 301 https://$host$request_uri; }
```

### 1.4 Agent 端（跨机器）

```bash
python3 setup.py --server https://dispatch.example.com \
                 --name tmtpc --code <一次性配对码> --executor workbuddy
```

> 注意：反代模式下 `X-Real-IP` 是代理 IP。如需按来源 IP 白名单，请让代理把真实 IP 透传，
> 或在服务端与代理之间用内网 + CIDR 白名单约束。

---

## 2. 方式 B：服务端内置 TLS（无反代时）

适合内网 / 没有反代的场景。需要一份证书（自签或内网 CA）。

### 2.1 生成自签证书（测试用；生产请用正式 CA）

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout server.key -out server.crt \
  -subj "/CN=dispatch.example.com"
```

### 2.2 启动

```bash
python3 server.py --host 0.0.0.0 --port 8765 \
  --cert ./server.crt --key ./server.key \
  --allow-cidr 192.168.8.0/24
```

启动日志会显示 `tls=true`。Web 访问 `https://<IP>:8765`，浏览器会提示自签证书不受信任（生产换成正式证书即可）。

### 2.3 Agent 端信任自签证书

`agent.py` 用标准库 urllib，默认校验证书。自签证书有两种处理：

- **推荐**：把自签 CA / 证书导入系统信任链（或改用正式证书）；
- 临时方案：设置环境变量让客户端跳过校验（**仅限内网测试**，生产禁用）：

```bash
export DISPATCH_INSECURE_TLS=1   # 若客户端支持；否则用正式证书
python3 agent.py pair --server https://192.168.8.192:8765 --name tmtpc --code <码>
```

---

## 3. 网络与访问控制

| 措施 | 位置 | 说明 |
|---|---|---|
| IP 白名单（全局） | `server.py --allow-cidr`（可多次） | 非白名单来源直接 403 |
| IP 白名单（单 Agent） | Web「Agent 管理中心」/ API | 该 Agent 只能从指定 IP 领取/回传 |
| 全局限速 | 内置 | 600 次/分钟/IP |
| 登录限速 | 内置 | 10 次/分钟/IP |
| 配对限速 | 内置 | 100 次/分钟/IP |
| 请求体上限 | 内置 | 1 MB |
| 失败锁定 | 内置 | 连续 5 次失败锁 5 分钟 |
| token 时效 | 内置 | 可在 Agent 上设置有效期，到期需重新配对 |

---

## 4. 账号与权限运维

```bash
# 首次启动后：用控制台打印的初始 admin 密码登录 Web，立即改密
# 命令行管理用户
python3 ctl.py user list
python3 ctl.py user add alice 'StrongPass1!' --role operator --display 爱丽丝
python3 ctl.py user role alice viewer
python3 ctl.py user status alice disabled      # 禁用 = 立即踢下线
python3 ctl.py user password alice 'NewPass2!'
python3 ctl.py user delete alice

# 审计
python3 ctl.py audit --limit 100
python3 ctl.py audit --actor alice
python3 ctl.py audit --action task
```

**角色**：admin（全局 + 用户管理 + 审计）、operator（自己的 Agent/任务/配对）、viewer（只读）。

**数据隔离**：非 admin 账号只能看到并操作**自己名下**的 Agent 及其任务；配对时自动归属当前账号。

---

## 5. 验证加固是否生效

```bash
# 1) 匿名访问管理 API 应被拒（未登录、无 token）
curl -i http://<中心>:8765/api/tasks        # 期望 401（未设 --allow-anon 时）

# 2) 错误 token 被拒
curl -i -H 'X-Dispatch-Token: wrong' http://<中心>:8765/api/tasks   # 401

# 3) 正确 service token 可访问
curl -i -H "X-Dispatch-Token: $DISPATCH_TOKEN" http://<中心>:8765/api/tasks  # 200

# 4) 登录 → 拿 Cookie → 访问
curl -c /tmp/cj -X POST -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"..."}' http://<中心>:8765/api/login
curl -b /tmp/cj http://<中心>:8765/api/me     # 200，显示当前用户

# 5) 审计有记录
python3 ctl.py audit --limit 10

# 6) 全量回归
python3 run_full_test.py
```

---

## 6. 现有 Agent 凭证兼容

加固**不影响**已配对的 Agent：`tmtpc`、`workbuddy` 等的 `X-Agent-Token` 继续有效。
迁移时旧 Agent 自动标记为 `approved`、无 IP 限制、无时效。只有主动设置了约束的 Agent 才会被新策略限制。
