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

---

## 10. 本次实际落地配置（2026-08-10 执行记录）

### 10.1 环境适配修正（与原方案不同处）
- **PostgREST 端口**：原方案用 `3000`，但该端口被 Grafana 占用，实际改用 **`3001`**。
- **DB_URI 不能用 `127.0.0.1`**：Docker 容器内 `127.0.0.1` 是容器自己，连不上宿主 PgBouncer。实际 URI 用节点 IP `217.69.2.217:6432`。
- **防火墙实际管理者是 ufw（非 firewalld）**：该机器为云镜像环境（含 Sanguo Panel/OpenLiteSpeed 等），Pigsty 的 `node_firewall` 任务因 firewalld inactive 而 skip。收口公网端口需直接操作 `ufw`，且 **5432 公网放行最终在云安全组层**（用户决定保持开放，不关闭）。
- **`pig pg psql` 不支持 `-d`**，连指定库用 `PGDATABASE=xxx pig pg psql -c ...` 或直接 `psql "postgres://user:pass@host:port/db"`。

### 10.2 凭据清单（请另行备份到密码管理器）
| 项 | 值 |
|----|----|
| 对外 API 域名 | `https://pigstyapi.yunyingx.com` |
| API Key (anon) | `bc1608bff236f578f166f8d3515f16f2` |
| 应用库名 | `appdb` |
| 应用账号 | `dbuser_app` |
| 应用账号密码 | `F1LKkyTDilEyJTDE7hjTGBPu` |
| PostgREST JWT Secret | `0ZMG3j7g5y5evEsq1k8kdQ9J5MVCPTmbkvBVAR1N6eB` |
| PostgREST 端口(容器内/本机) | `3001` |
| PgBouncer 连接地址 | `217.69.2.217:6432` |
| Let's Encrypt 证书 | `/etc/letsencrypt/live/pigstyapi.yunyingx.com/`（2026-11-08 到期，自动续期） |

### 10.3 关键文件路径
- Nginx 配置：`/etc/nginx/conf.d/pigstyapi.conf`
- PostgREST 配置：`/root/pigsty/app/postgrest/.env`、`docker-compose.yml`
- OpenAPI schema 存档：`/root/pigsty/docs/pigstyapi-openapi.json`

### 10.4 运维命令
```bash
# 重启 PostgREST（新建表后必须，以刷新 schema 缓存）
cd /root/pigsty/app/postgrest && docker compose restart

# 查看 PostgREST 日志
docker logs -f postgrest

# 测试 API
curl -s -H "apikey: bc1608bff236f578f166f8d3515f16f2" "https://pigstyapi.yunyingx.com/todos"

# 续期证书（certbot 已配自动任务，手动触发：）
certbot renew
```

---

## 11. 给 AI 建站平台的最终接入说明

把以下信息填进你的 AI 建站平台（类 Lovable）的"后端 / 数据源"配置：

### 11.1 连接配置
| 字段 | 值 |
|------|----|
| Base URL | `https://pigstyapi.yunyingx.com` |
| 认证方式 | API Key（Header） |
| Header 名 | `apikey` （或 `Authorization: Bearer <key>`） |
| Header 值 | `bc1608bff236f578f166f8d3515f16f2` |
| API 风格 | REST（PostgREST），完全兼容 Supabase 客户端 |

### 11.2 调用示例
```js
// 读取 todos 表全部数据
const res = await fetch("https://pigstyapi.yunyingx.com/todos?select=*", {
  headers: { "apikey": "bc1608bff236f578f166f8d3515f16f2" }
});
const rows = await res.json();

// 插入
await fetch("https://pigstyapi.yunyingx.com/todos", {
  method: "POST",
  headers: {
    "apikey": "bc1608bff236f578f166f8d3515f16f2",
    "Content-Type": "application/json"
  },
  body: JSON.stringify({ title: "new task", done: false })
});
```

### 11.3 让 AI 平台"认识"你的库结构（强烈推荐）
把 OpenAPI schema 作为上下文喂给平台，它就能自动知道有哪些表、字段、类型：
```bash
# 文件已存档：/root/pigsty/docs/pigstyapi-openapi.json
# 直接把这个文件内容贴给 AI 平台，或托管后给它 URL
```
PostgREST 会自动把 `appdb.public` 下的所有表/视图暴露为 REST 端点，例如：
- `GET /todos` → 查
- `POST /todos` → 增
- `PATCH /todos?id=eq.1` → 改
- `DELETE /todos?id=eq.1` → 删
- `GET /todos?select=id,title&done=eq.false&order=id.desc&limit=10` → 过滤/排序/分页

### 11.4 在 Pigsty 侧给平台加新表
平台生成的"建表"动作需由你在数据库侧执行（PostgREST 不会自动建表）。两种方式：
1. 在 `pigsty.yml` 的 `pg_databases` / 预置 SQL 里定义，或
2. 直接 `psql` 连 `appdb` 执行 `CREATE TABLE`，然后 `docker compose restart` 刷新 PostgREST schema 缓存。

> 注意：当前 API 账号 `dbuser_app` 是 `dbrole_readwrite`，可建表/读写。若需更细权限（只读/只写某些表），参见 §7 权限细化。

---

## 12. 已知限制与后续优化
- **新建表后 schema 缓存自动刷新**（已实现，见 §13）：通过 `NOTIFY pgrst,'reload config'` + PostgREST 直连 5432（**不能走 PgBouncer 6432 事务池**，否则 LISTEN 随事务断开）。无需再 restart 容器。
- **API Key 为静态 anon key**：所有人共用同一权限。如需"每用户隔离"，启用 JWT（`PGRST_JWT_SECRET` 已配），由 Supabase Auth 或自建鉴权签发带 `role` 的 JWT。
- **公网 5432 仍开放**（按用户决策保留），建议仅作为运维备用，业务流量一律走 443 API。
- 可选升级为完整 Supabase（§6），获得 Auth/Realtime/Storage。

---

## 13. 自动刷新 schema 缓存（db-channel + NOTIFY）

### 13.1 原理
PostgREST 监听 Postgres 的 `LISTEN/NOTIFY` 通道（默认 `pgrst`）。在数据库内执行 `NOTIFY pgrst, 'reload config'` 即可触发其**重载 schema 缓存**，无需重启进程。

### 13.2 关键坑：必须直连 PG，不能经 PgBouncer
- ❌ **失败配置**：`POSTGREST_DB_URI` 走 `:6432`（PgBouncer 事务池模式）。事务池下 LISTEN 在事务结束即断开，PostgREST 收不到通知，自动刷新失效。
- ✅ **正确配置**：`POSTGREST_DB_URI` 直连 `:5432`（PG 原生连接，LISTEN 持久）。PostgREST 自带连接池，抗压没问题。
- 当前 `.env` 已用 `217.69.2.217:5432/appdb`（见 §10.2）。

### 13.3 触发命令
```sql
-- 任意有 NOTIFY 权限的会话执行：
NOTIFY pgrst, 'reload config';
```
> 注意：通知字符串是 `reload config`（不是 `reload schema`），前者会同时重载配置与 schema cache。

### 13.4 验证
建/改表后 3~5 秒内，新表即出现在 REST 端点，无需 `docker compose restart`。

---

## 14. 自助建表接口（RPC）

为让 AI 建站平台**自助建表**（PostgREST 本身不提供 DDL，需经存储过程），在 `appdb` 建了 RPC 函数 `create_table`。

### 14.1 函数定义（已部署于 appdb.public）
```sql
CREATE OR REPLACE FUNCTION create_table(table_name text, columns text)
RETURNS jsonb LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE sql text; result jsonb;
BEGIN
  IF table_name !~ '^[a-zA-Z_][a-zA-Z0-9_]*$' THEN
    RETURN jsonb_build_object('ok', false, 'error', 'invalid table_name');
  END IF;
  sql := format('CREATE TABLE IF NOT EXISTS %I (%s)', table_name, columns);
  EXECUTE sql;
  PERFORM pg_notify('pgrst', 'reload config');   -- 触发 §13 自动刷新
  RETURN jsonb_build_object('ok', true, 'table', table_name);
END; $$;
```

### 14.2 AI 平台调用方式
```js
// POST /rpc/create_table
const res = await fetch("https://pigstyapi.yunyingx.com/rpc/create_table", {
  method: "POST",
  headers: {
    "apikey": "bc1608bff236f578f166f8d3515f16f2",
    "Content-Type": "application/json"
  },
  body: JSON.stringify({
    table_name: "tasks",
    columns: "id serial primary key, title text, done boolean default false"
  })
});
// => {"ok":true,"table":"tasks"}
// 3~5 秒后该表即可通过 REST 直接读写，无需人工干预
```

### 14.3 安全说明
- 函数做了 `table_name` 白名单校验（仅字母数字下划线），`columns` 仍需调用方保证安全（建议 AI 平台只生成标准列定义）。
- 执行身份为 `dbuser_app`（`dbrole_readwrite`），可建表/读写。
- 若需限制 AI 平台只能建表不能删库，可把建表逻辑收紧或改用专用低权限角色。

### 14.4 完整端到端验证（已执行通过）
```
POST /rpc/create_table → {"ok":true,"table":"tasks"}
sleep 4
GET  /tasks?select=*   → []            (自动刷新生效，新表可见)
POST /tasks            → 插入成功
GET  /tasks?select=*   → [{"id":1,...}] (数据可读)
```

---

## 15. 当前平台能力清单（截至 2026-08-10）
- ✅ REST CRUD：任意 `appdb.public` 下表，通过 `https://pigstyapi.yunyingx.com/<table>`
- ✅ 自助建表：`POST /rpc/create_table`（建后自动刷新，无需重启）
- ✅ 鉴权：API Key（header `apikey` 或 `Authorization: Bearer`）
- ✅ SSL：Let's Encrypt 证书，自动续期
- ✅ 限流 + CORS：Nginx 层
- ✅ OpenAPI 自动同步：建表后 `/openapi.json` 自动包含新表结构（见 §16）
- ⏳ 可选：JWT 用户级权限、Supabase 完整套件（§6）

---

## 16. 建表后自动更新 OpenAPI（让 AI 平台认知新字段）

### 16.1 原理
PostgREST 的 `GET /` 本身**实时动态生成** OpenAPI（基于当前 schema 缓存）。建表 RPC（§14）触发 schema 刷新后，再次请求 `GET /` 立即包含新表。**因此 AI 平台最规范的做法是：建表后重新拉取 `GET /`**，无需任何文件维护。

为兼容"只读静态文件"的平台，额外提供**定时落盘 + 固定 URL** 兜底。

### 16.2 同步脚本（已部署）
文件：`/usr/local/bin/pigstyapi-openapi-sync.sh`
```bash
#!/usr/bin/env bash
set -euo pipefail
API_URL="https://pigstyapi.yunyingx.com/"
API_KEY="bc1608bff236f578f166f8d3515f16f2"
OUT="/var/www/html/pigstyapi-openapi.json"
TMP="$(mktemp)"
curl -fsS -H "apikey: ${API_KEY}" "${API_URL}" -o "${TMP}"
mv "${TMP}" "${OUT}"
chmod 644 "${OUT}"     # 必须 644，否则 nginx worker 无权限读 → 403
```
> 注意：`chmod 644` 关键，否则 Nginx 返回 403（踩过的坑）。

### 16.3 定时任务（已配置）
```cron
*/2 * * * * /usr/local/bin/pigstyapi-openapi-sync.sh >> /var/log/pigstyapi-openapi.log 2>&1
```
每 2 分钟刷新一次（建表低频，2 分钟足够；平台也可建表后立即 `GET /` 拿实时版）。

### 16.4 公开端点
Nginx 暴露 `https://pigstyapi.yunyingx.com/openapi.json`（公开可读，仅含表/列结构，无数据无密码）。

### 16.5 AI 平台接入方式（二选一）
- **方式 A（推荐）**：建表后直接 `GET https://pigstyapi.yunyingx.com/`（实时，零延迟）→ 解析 schema。
- **方式 B（静态兜底）**：建表后等待 ≤2 分钟，拉取 `https://pigstyapi.yunyingx.com/openapi.json`。

拿到 schema 后，平台即可知道新表的字段类型，自动生成对应的 CRUD 调用。

### 16.6 验证记录
```
POST /rpc/create_table {table_name:"notes",...}  → {"ok":true,...}
GET  /openapi.json  → 含 /notes, /tasks, /todos   ✅
```

### 16.7 已知小瑕疵（非阻塞）
- OpenAPI 内 `host` 字段仍显示 `0.0.0.0:3001`（PostgREST 容器内地址），因 `PGRST_SERVER_PROXY_URI` 未完全覆盖。不影响平台使用（平台用我们给的 Base URL），仅 swagger ui 展示用。如需修正可在 `.env` 调整 `server-host` 相关参数。

---

## 17. 多库 API：pg1~pg100 全部暴露（每项目独立 database）

> 采用 GPT 方案一的精简版（API Gateway + PostgREST Worker Pool）：
> **每个项目库一个 PostgREST 实例**，由 Nginx 按路径 `/pgN/` 路由。
> 对外是"一个域名按路径选库"，对 AI 平台而言就是"动态选数据库"。

### 17.1 架构

```
AI 建站平台
   │  HTTPS + apikey
   ▼
Nginx (pigstyapi.yunyingx.com)
   │  /pg1/* → 127.0.0.1:3101 (PostgREST → pg1)
   │  /pg2/* → 127.0.0.1:3102 (PostgREST → pg2)
   │  ...
   │  /pg100/* → 127.0.0.1:3200 (PostgREST → pg100)
   │  / (root) → 127.0.0.1:3001 (PostgREST → appdb, 管理库)
   ▼
PostgREST 容器 ×100（docker-compose.multi.yml）
   │  直连 217.69.2.217:5432（容器内源 IP 在 172.16.0.0/12，命中 pg_hba 放行规则）
   ▼
Pigsty pg-meta: pg1, pg2, ... pg100（独立 database）
```

### 17.2 端口规划
- `appdb` PostgREST：**3001**（根路径 `/`，向后兼容）
- `pgN` PostgREST：**3100 + N** → pg1=3101, pg2=3102, ... pg100=3200

### 17.3 权限（方案 b：不动库归属，直接 GRANT）
由 superuser(`postgres`) 在每个 `pgN` 库执行（已执行）：
```sql
GRANT CONNECT ON DATABASE pgN TO dbuser_app;
GRANT ALL ON SCHEMA public TO dbuser_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES    TO dbuser_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO dbuser_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO dbuser_app;
```
库的 owner 保持原样（`postgres`/`dbuser_meta`），仅把读写权限赋给 `dbuser_app`。

### 17.10 安全加固：方案 X — 用 `api` schema 隔离扩展危险函数
**问题**：PostgREST 原暴露 `public` schema，其 OpenAPI 会列出 PostgreSQL 扩展自带的函数/处理器
（`postgres_fdw_*`、`file_fdw_handler`、`show_trgm`、`show_limit` 等），构成不必要的攻击面。

**方案 X（已落地）**：PostgREST 改为**只暴露 `api` schema**，彻底隔离 `public` 中的扩展对象。
由 superuser(`postgres`) 在每个库（appdb + pg1~pg100）执行（脚本 `app/postgrest/multi/setup_api_schema.sh`）：
```sql
CREATE SCHEMA IF NOT EXISTS api;
-- 将 public 下的用户基表整体迁移到 api（数据不动，权限跟随）
ALTER TABLE public.<t> SET SCHEMA api;
-- 授权
GRANT CREATE, USAGE ON SCHEMA api TO dbuser_app;
GRANT ALL ON ALL TABLES IN SCHEMA api TO dbuser_app;
GRANT ALL ON ALL SEQUENCES IN SCHEMA api TO dbuser_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA api GRANT ALL ON TABLES    TO dbuser_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA api GRANT ALL ON SEQUENCES TO dbuser_app;
-- 仅暴露安全 RPC：create_table（建表落在 api schema）
CREATE OR REPLACE FUNCTION api.create_table(...) ...
```
`gen_multi.sh` 同步改为 `PGRST_DB_SCHEMA: api`，重新生成并 `docker compose up -d` 重建 100 容器。

**效果验证**：
```text
GET /pg1/  →  paths: ['/', '/rpc/create_table', '/todos', '/user']
            （postgres_fdw_* / file_fdw_handler / show_trgm / show_limit 全部消失）✅
POST /pg1/rpc/create_table {"table_name":"x_test","columns":"id int primary key, v text"} → {"ok":true,"schema":"api"} ✅
POST /pg1/x_test {"id":1,"v":"hi"} → [{"id":1,"v":"hi"}] ✅
```
> 注：Nginx 的 `/pgN/openapi.json` 为静态落盘文件，需重跑
> `/usr/local/bin/pigstyapi-openapi-sync-all.sh` 刷新（已刷新至最新 api-only 内容）。
> 业务表现统一位于 `api` schema，新建表通过 `create_table` RPC 自动落在 `api`。

### 17.4 部署文件
| 文件 | 作用 |
|------|------|
| `app/postgrest/docker-compose.multi.yml` | 100 个 PostgREST service（pg1..pg100），由 `gen_multi.sh` 生成 |
| `app/postgrest/gen_multi.sh` | 生成脚本（改端口/凭据后重跑即可） |
| `/etc/nginx/conf.d/pigstyapi-multi.conf` | 100 个 `/pgN/` location + 每库 OpenAPI 端点 |
| `/etc/nginx/conf.d/pigstyapi.conf` | 仅保留 80→443 重定向（443 块已迁至 multi.conf） |
| `/usr/local/bin/pigstyapi-openapi-sync-all.sh` | 批量同步 appdb + pg1..pg100 的 OpenAPI.json |

启动：
```bash
cd app/postgrest && docker compose -f docker-compose.multi.yml up -d
nginx -s reload
```

### 17.5 自助建表 RPC（每库 `api` schema 已部署 `create_table`）
所有库（含 pg1~pg100、appdb）的 `api` schema 均已部署（方案 X 后迁移至此）：
```sql
CREATE OR REPLACE FUNCTION api.create_table(table_name text, columns text) RETURNS jsonb ...
  -- 内部 CREATE TABLE 落在 api schema；建表后 PERFORM pg_notify('pgrst', 'reload schema');
```
调用（以 pg4 为例）：
```http
POST /pg4/rpc/create_table
{ "table_name": "rpc_test", "columns": "id serial primary key, name text" }
```
**关键修正**：PostgREST 16 中 schema 刷新指令是 `NOTIFY pgrst, 'reload schema'`（旧文档写的 `reload config` 只重载配置、不刷新 schema cache，已纠正）。

### 17.6 自动刷新验证
- 在 pg2 建表 + `SELECT pg_notify('pgrst','reload schema')` → 2~3 秒后 `/pg2/新表` 立即可用 ✅
- 经 RPC 建表自动触发 notify → 无需重启容器 ✅

### 17.7 OpenAPI 自动同步
脚本 `pigstyapi-openapi-sync-all.sh` 把每个库的 OpenAPI 落盘：
- `appdb` → `/var/www/html/pigstyapi-openapi.json`（公开端点 `/openapi.json`）
- `pgN`  → `/var/www/html/pigstyapi-pgN-openapi.json`（公开端点 `/pgN/openapi.json`）

AI 平台可访问 `https://pigstyapi.yunyingx.com/pgN/openapi.json` 获取该库表结构。
> 注：crontab 定时同步**未启用**（用户拒绝）。需手动运行
> `/usr/local/bin/pigstyapi-openapi-sync-all.sh` 刷新，或建表后直接 `GET /pgN/` 拿实时 schema。

### 17.8 AI 平台接入示例
```text
Base URL:  https://pigstyapi.yunyingx.com
Header:    apikey: bc1608bff236f578f166f8d3515f16f2

操作 pg1 库里的 users 表：
  GET    /pg1/users
  POST   /pg1/users        {"name":"alice"}
  PATCH  /pg1/users?id=eq.1 {"name":"bob"}
  DELETE /pg1/users?id=eq.1

获取 pg1 库结构：
  GET /pg1/openapi.json
```
路径前缀 `/pgN/` 即"选择哪个数据库"，其余 PostgREST 语法与官方完全一致。

### 17.9 验证记录
```text
GET /pg1/                         → 返回 pg1 的 OpenAPI（路由到 3101）✅
GET /pg2/ 无 apikey               → 401 ✅
POST /pg4/rpc/create_table        → {"ok":true,"table":"rpc_test"} ✅
GET  /pg4/rpc_test                → [{"id":1,"name":"via rpc"}] ✅（自动刷新生效）
GET  /pg1/openapi.json            → 200，含表结构 ✅
docker ps | grep postgrest_pg     → 100 个容器 running ✅
```

