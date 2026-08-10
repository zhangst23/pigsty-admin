# Pigsty + PostgREST + Nginx 对外数据库 API 方案

> 目标：基于 Pigsty 搭建"类 Supabase 数据库中台"，让 AI 建站平台（类 Lovable）通过 **HTTPS + API Key** 自动连接并使用你的 PostgreSQL 数据库，**避免直接暴露 5432 与 pg_hba 的 IP 限制难题**。

---

## 0. 架构总览

```
AI 建站平台 (类 Lovable)
        │  HTTPS  +  Header: apikey: <API_KEY>
        ▼
┌──────────────────────────────────────────────┐
│  Nginx (SSL 终止 / 域名 / 限流 / 鉴权转发)      │  :443  →  api.yourdomain.com
│   - 校验 apikey 后反向代理到 PostgREST          │
└──────────────────────────────────────────────┘
        │  http://127.0.0.1:3000  (服务器内网)
        ▼
┌──────────────────────────────────────────────┐
│  PostgREST 容器                                │  表/视图 → REST API
│   docker image: postgrest/postgrest            │
└──────────────────────────────────────────────┘
        │  postgres://dbuser_app:***@127.0.0.1:6432/appdb  (走 PgBouncer)
        ▼
┌──────────────────────────────────────────────┐
│  Pigsty PG 集群 pg-meta (实例 pg-meta-1)        │
│   - appdb 数据库 + dbuser_app 专用账号          │
│   - PgBouncer :6432 / PG :5432                 │
│   - HA / 备份 / 监控 由 Pigsty 全权管理          │
└──────────────────────────────────────────────┘
```

**核心理念**：AI 平台**不直接连 PG**，而是连 PostgREST 暴露的 HTTP REST API。API 网关（PostgREST + Nginx）从**服务器本机/内网**连 PG，hba 看到的是 `127.0.0.1` / `intra` 网段，天然放行 —— 公网客户端 IP 限制问题彻底消失。

> 如果你想要"完整 Supabase 体验"（Auth/JWT、Realtime、Storage、Studio），直接跳到 **§6 用官方 supabase 模板**，底层 PG 仍用本方案的 Pigsty 集群。

---

## 1. 环境现状（基于当前 pigsty.yml）

| 项 | 值 | 说明 |
|----|----|----|
| 节点 | `217.69.2.217` | 单节点，infra/etcd/pg-meta 都在其上 |
| PG 集群 | `pg-meta` | 实例 `pg-meta-1` |
| PG 端口 | `5432` | 直连 PG |
| PgBouncer 端口 | `6432` | 连接池（推荐 PostgREST 走这里） |
| 已有账号 | `dbuser_meta`(admin) / `dbuser_view`(readonly) | 见 pigsty.yml:45-47 |
| hba 规则 | `intra` 内网放行 + pwd | pigsty.yml:261-262 |
| 防火墙 | 已放开 `22,80,443,5432` | pigsty.yml:353（**5432 公网放开仅为 demo，生产要收掉**） |
| Docker | `docker_enabled: true`（app 段） | pigsty.yml:312 |
| PostgREST 模板 | `app/postgrest/` 已存在 | docker-compose.yml + Makefile |
| 域名模板 | `infra_portal` 支持 domain | pigsty.yml:338-341 |



---

## 2. 步骤一：Pigsty 侧准备（声明专用库与账号）

不要复用 `dbuser_meta` / `dbuser_view`，为 AI 平台建**专用库 + 专用应用账号**。

编辑 `pigsty.yml`，在 `pg-meta` 集群的 `pg_users` / `pg_databases` 段追加：

```yaml
# pigsty.yml  →  pg-meta 集群 vars 段
pg_users:
  # ... 已有 dbuser_meta / dbuser_view ...
  - { name: dbuser_app, password: 'CHANGE_ME_STRONG_PASS', pgbouncer: true,
      roles: [dbrole_readwrite], comment: 'ai platform app user' }

pg_databases:
  # ... 已有 meta / pg1..pg100 ...
  - { name: appdb, owner: dbuser_app, comment: 'ai platform backend database' }
```

> 密码请用强随机串：`openssl rand -base64 18`。

应用变更（**危险操作，需你确认后执行**）：

```bash
cd /root/pigsty
ansible-playbook pgsql-user.yml -l pg-meta -e pg_user=dbuser_app   # 建用户
ansible-playbook pgsql-db.yml    -l pg-meta -e dbname=appdb         # 建库
# 或一次性： ansible-playbook pgsql.yml -l pg-meta --tags pg_user,pg_db
```

验证：

```bash
pig pg psql -d appdb -c '\dn'                       # 看 schema
pig pg psql -d appdb -c 'SELECT current_user;'      # 用 psql 手动验证（需配 .pgpass 或用 dbuser_app）
```

---

## 3. 步骤二：部署 PostgREST（表自动变 REST API）

Pigsty 已带 `app/postgrest/` 模板。我们基于它配置，让 PostgREST 连 `appdb`，并由 **Nginx 做鉴权前置**（见 §4），所以 PostgREST 本身只监听本机。

### 3.1 准备 .env

```bash
cd /root/pigsty/app/postgrest
cat > .env <<'EOF'
POSTGREST_DB_URI=postgres://dbuser_app:CHANGE_ME_STRONG_PASS@127.0.0.1:6432/appdb
POSTGREST_DB_SCHEMA=public
POSTGREST_DB_ANON_ROLE=dbuser_app
POSTGREST_SERVER_PORT=3000
POSTGREST_JWT_SECRET=CHANGE_ME_JWT_SECRET_AT_LEAST_32_CHARS
EOF
```

> - 用 `127.0.0.1:6432`（PgBouncer 连接池）而非 5432，抗压更好。
> - `PGRST_JWT_SECRET` 用于后续 JWT 鉴权（可选）。先用 anon role 直连即可跑通。

### 3.2 修改 docker-compose 监听本机

避免 PostgREST 直接暴露公网，把端口绑定改成本机：

```yaml
# app/postgrest/docker-compose.yml  ports 段改为：
ports:
  - "127.0.0.1:3000:3000"
```

### 3.3 启动

```bash
cd /root/pigsty/app/postgrest
make up            # docker compose up -d
make view          # 打印访问地址
docker logs -f postgrest
```

### 3.4 自测（服务器本地）

```bash
# 在 appdb 建一张测试表
pig pg psql -d appdb -c 'CREATE TABLE IF NOT EXISTS todos (id serial primary key, title text, done bool default false);'
pig pg psql -d appdb -c "INSERT INTO todos(title) VALUES ('hello from postgrest');"

# 通过 PostgREST 读取（本机）
curl -s "http://127.0.0.1:3000/todos" | jq
# => [{"id":1,"title":"hello from postgrest","done":false}]
```

> 现在 REST API 已能工作：`GET/POST/PATCH/DELETE /todos` 自动映射到表。这就是"AI 平台自动连库"的底层能力。

---

## 4. 步骤三：Nginx 前置（SSL + 域名 + API Key 鉴权）

这一步把 `http://127.0.0.1:3000` 变成 `https://api.yourdomain.com`，并**强制 API Key 校验**，对外只开 443。

### 4.1 域名解析

把 `api.yourdomain.com` 的 A 记录指向 `217.69.2.217`。

### 4.2 生成/复用 SSL 证书

Pigsty 自带 CA（`files/pki`），可签发服务端证书；或用自己的 Let's Encrypt 证书。简易自签（仅测试）：

```bash
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /etc/nginx/ssl/api.key -out /etc/nginx/ssl/api.crt \
  -days 365 -subj "/CN=api.yourdomain.com"
```

### 4.3 Nginx 配置

```nginx
# /etc/nginx/conf.d/api.yourdomain.com.conf
server {
    listen 443 ssl;
    server_name api.yourdomain.com;

    ssl_certificate     /etc/nginx/ssl/api.crt;
    ssl_certificate_key /etc/nginx/ssl/api.key;

    # 限流：每 IP 每秒 10 请求
    limit_req_zone $binary_remote_addr zone=apilimit:10m rate=10r/s;
    limit_req zone=apilimit burst=20 nodelay;

    location / {
        # 简单 API Key 鉴权（类 Supabase anon key）
        if ($http_apikey != "YOUR_PUBLIC_ANON_KEY") {
            return 401 '{"error":"unauthorized"}';
        }
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

> 更完整做法：用 `auth_request` 子请求 + JWT 校验（配合 §3 的 `PGRST_JWT_SECRET`），实现"用户级"权限。MVP 阶段用静态 `apikey` 即可。

### 4.4 重载 Nginx

```bash
nginx -t && systemctl reload nginx
```

### 4.5 外网验证

```bash
curl -s -H "apikey: YOUR_PUBLIC_ANON_KEY" \
  "https://api.yourdomain.com/todos" | jq
```

---

## 5. 步骤四：对接 AI 建站平台（类 Lovable）

在你的建站平台里配置"数据源 / 后端 API"，填入：

| 字段 | 值 |
|------|----|
| Base URL | `https://api.yourdomain.com` |
| API Key (Header) | `apikey: YOUR_PUBLIC_ANON_KEY` |
| 调用方式 | REST：`GET/POST/PATCH/DELETE /<table>` |
| 建表 | 由你在 Pigsty 侧预先建好 schema；或给平台一个"建表"专用接口（见 §7） |

**典型交互示例**（平台生成的代码）：

```js
const res = await fetch("https://api.yourdomain.com/todos?select=*", {
  headers: { "apikey": "YOUR_PUBLIC_ANON_KEY" }
});
const rows = await res.json();
```

平台只要会拼 REST URL，就能自动 CRUD —— 完全类比 Lovable 连 Supabase（Supabase 底层也是 PostgREST）。

> 可选：把 PostgREST 的 OpenAPI schema 喂给 AI 平台做"智能补全"：
> `curl https://api.yourdomain.com/ > openapi.json`，作为平台上下文。

---

## 6. 进阶：直接复用官方 Supabase 模板（推荐想要完整能力时）

如果你想要 Auth(JWT)、Realtime、Storage、Supabase Studio，Pigsty 已提供 `app/supabase/` 模板，底层 PG 直接复用 `pg-meta` 集群：

```bash
cd /root/pigsty
./docker.yml     # 确保 docker 就绪
./app.yml        # 用 pigsty.yml 的 app 段启动 supabase 无状态部分
```

配置要点（改 `pigsty.yml` 的 `app` 段 + supabase 的 `.env`）：
- `API_EXTERNAL_URL=https://supa.yourdomain.com/auth/v1`
- PostgREST schema 默认 `public,graphql_public`
- Storage 需要 MinIO/S3（取消 pigsty.yml 里 `minio` 段注释并部署）
- 通过 `infra_portal` 暴露：`supa: { domain: supa.yourdomain.com, endpoint: "${admin_ip}:8000", scheme: https }`

详见 `app/supabase/README.md` 与 https://pigsty.io/docs/app/supabase

---

## 7. 安全与运维清单

### 必须做（生产）
- [ ] **收掉公网 5432**：把 `node_firewall_public_port` 改回 `[22,80,443]`，AI 平台只走 443。
- [ ] 用强随机 `dbuser_app` 密码 + `PGRST_JWT_SECRET` + `YOUR_PUBLIC_ANON_KEY`。
- [ ] Nginx 开启 `limit_req` 限流，避免 API 被刷。
- [ ] 用真实 CA 证书（Let's Encrypt）替代自签。
- [ ] 数据库只给 `dbrole_readwrite`，不要用 admin 账号做应用连接。
- [ ] 定期 `pig pb info` 确认备份，pg-meta 已配每日全备（pigsty.yml:263-264）。

### 权限细化（可选）
- 在 PG 里建 `dbrole_api_ro` / `dbrole_api_rw`，用 `GRANT` 控制表级权限，PostgREST 的 anon role 对应不同权限。
- 用 JWT：`Authorization: Bearer <jwt>`，PostgREST 据 JWT 里的 `role` claim 切换权限。

### 监控
- Pigsty Grafana 已监控 PG；PostgREST/Nginx 可用 `pg_exporter` + node_exporter 覆盖，或加 Prometheus 黑盒探测 `/todos` 健康。

---

## 8. 与"直连 PG"方案对比

| 维度 | 直连 5432（之前失败的方式） | 本方案（PostgREST+Nginx） |
|------|---------------------------|--------------------------|
| pg_hba 限制 | 客户端公网 IP 需逐条放行 | 网关内网连 PG，天然放行 |
| 凭证暴露 | 直接暴露 DB 账号密码 | 仅暴露 API Key |
| SSL | 需 PG 端配证书 | Nginx 统一 SSL |
| AI 平台适配 | 需写 SQL/驱动 | 标准 REST，Lovable 友好 |
| 限流/审计 | 无 | Nginx 层可做 |
| 权限粒度 | DB 账号级 | 表级 + JWT 用户级 |

---

## 9. 落地顺序建议

1. **只读诊断**（无需改配置）：确认 Docker 可用、端口状态、app 模板就绪。
2. **§2** 建 `appdb` + `dbuser_app`（需你授权执行 playbook）。
3. **§3** 起 PostgREST（本机 3000）。
4. **§4** 配 Nginx + 域名 + SSL + API Key。
5. **§5** 在 AI 建站平台填入 Base URL + Key 联调。
6. 生产加固（§7）+ 可选 Supabase 模板（§6）。

---

> 文档配套：本方案所有 playbook / docker 操作均属**配置变更或危险操作**，按 Pigsty 安全策略需你明确授权后执行。建议先走"只读诊断 + §2/§3 本机联调"，确认可用再开放公网 443。
