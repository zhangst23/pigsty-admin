# Pigsty 云数据库 API 接入指南

> 基于 Pigsty + PostgREST + Nginx 搭建的「类 Supabase 数据库中台」对外 API 文档。
> 让 AI 建站平台（类 Lovable）通过 **HTTPS + API Key** 自动连接并使用 PostgreSQL，**无需直连 5432、无需处理 pg_hba 的 IP 限制**。

---

## 1. 架构总览

```
AI 建站平台
   │  HTTPS  +  Header: apikey: <API_KEY>
   ▼
Nginx (pigstyapi.yunyingx.com)        ← SSL 终止 / API Key 鉴权 / 限流 / CORS / 路径路由
   │  /pg1/*   → 127.0.0.1:3101 (PostgREST → pg1)
   │  /pg2/*   → 127.0.0.1:3102 (PostgREST → pg2)
   │  ...
   │  /pg100/* → 127.0.0.1:3200 (PostgREST → pg100)
   │  / (root) → 127.0.0.1:3001 (PostgREST → appdb 管理库)
   ▼
PostgREST 容器 ×100（docker-compose.multi.yml）
   │  直连 217.69.2.217:5432（容器内源 IP 在 172.16.0.0/12，命中 pg_hba 放行）
   ▼
Pigsty pg-meta: appdb, pg1, pg2, ... pg100（独立 database）
```

**核心**：路径前缀 `/pgN/` 即「选择哪个数据库」，其余 PostgREST 语法与官方完全一致。

---

## 2. 接入信息（Credentials）

| 项 | 值 |
|----|----|
| Base URL | `https://pigstyapi.yunyingx.com` |
| API Key | `bc1608bff236f578f166f8d3515f16f2` |
| 鉴权方式 | 请求头 `apikey: <key>` 或 `Authorization: Bearer <key>` |
| 管理库 | `/`（appdb，含 `create_database` / `create_table` RPC） |
| 项目库 | `/pgN/`（pg1~pg100 全部已暴露） |

> ⚠️ 以上 Key / 密码为生产凭据，仅限你本人使用，请勿外泄或提交到公开仓库。

---

## 3. 快速开始

### 3.1 列出某库所有可访问表（读 OpenAPI）
```bash
curl -H "apikey: bc1608bff236f578f166f8d3515f16f2" \
     https://pigstyapi.yunyingx.com/pg1/openapi.json
```

### 3.2 查询数据
```bash
# 查 pg1 库里的 users 表（全部）
curl -H "apikey: bc1608bff236f578f166f8d3515f16f2" \
     https://pigstyapi.yunyingx.com/pg1/users

# 带过滤
curl -H "apikey: ..." "https://pigstyapi.yunyingx.com/pg1/users?age=gte.18&order=id.desc"

# 只取某些字段
curl -H "apikey: ..." "https://pigstyapi.yunyingx.com/pg1/users?select=id,name"
```

### 3.3 插入数据
```bash
curl -X POST -H "apikey: ..." -H "Content-Type: application/json" \
     -d '{"name":"alice","age":20}' \
     https://pigstyapi.yunyingx.com/pg1/users
```

### 3.4 更新 / 删除
```bash
# 更新 id=1
curl -X PATCH -H "apikey: ..." -H "Content-Type: application/json" \
     -d '{"name":"bob"}' \
     "https://pigstyapi.yunyingx.com/pg1/users?id=eq.1"

# 删除 id=1
curl -X DELETE -H "apikey: ..." \
     "https://pigstyapi.yunyingx.com/pg1/users?id=eq.1"
```

---

## 4. PostgREST 标准语法速查

| 操作 | 方法 + 路径 | 说明 |
|------|------------|------|
| 查询 | `GET /pgN/table` | 列表；支持 `?select=`, `?col=op.val`, `?order=`, `?limit=` |
| 单行 | `GET /pgN/table?id=eq.1` | 按主键/条件取一行 |
| 插入 | `POST /pgN/table` | body 为 JSON 对象或数组 |
| 更新 | `PATCH /pgN/table?col=op.val` | 按条件更新 |
| 删除 | `DELETE /pgN/table?col=op.val` | 按条件删除 |
| 调用 RPC | `POST /pgN/rpc/func_name` | body 为函数参数 JSON |
| 表结构 | `GET /pgN/openapi.json` | 该库 OpenAPI 描述（供 AI 平台发现字段） |

**常用过滤操作符**：`eq` `neq` `gt` `gte` `lt` `lte` `like` `ilike` `in.(a,b)` `is.null` `fts`(全文检索)

示例：`?age=gte.18&status=eq.active&name=ilike.*john*`

---

## 5. 自助建表（RPC）

每个库（含 appdb、pg1~pg100）均已部署 `create_table`，建表后自动刷新 schema，**无需重启容器**。

```bash
curl -X POST -H "apikey: ..." -H "Content-Type: application/json" \
     -d '{"table_name":"rpc_test","columns":"id serial primary key, name text, created_at timestamptz default now()"}' \
     https://pigstyapi.yunyingx.com/pg4/rpc/create_table
```

返回：
```json
{"ok":true,"table":"rpc_test"}
```

建表后约 2~3 秒，该表即可通过 `GET /pgN/rpc_test` 直接访问（PostgREST 经 `NOTIFY pgrst,'reload schema'` 自动重载）。

> 注意：`table_name` 仅允许 `^[a-zA-Z_][a-zA-Z0-9_]*$`，防止注入。列定义字符串由调用方负责合法性。

---

## 6. AI 平台集成建议

1. **发现表结构**：对每个项目库先 `GET /pgN/openapi.json`，拿到表/字段清单再生成调用代码。
2. **选库即选路径**：项目 A 用 `/pg1/`，项目 B 用 `/pg2/`，互不干扰（独立 database）。
3. **统一 Header**：所有请求带 `apikey`。
4. **错误码**：
   - `401` 缺/错 API Key
   - `429` 限流（单 IP 10 r/s，burst 20）
   - `404` 表/路径不存在
   - `400` 请求体/参数错误（PostgREST 返回 `PGRST` 错误码）

---

## 7. 部署与运维

### 7.1 文件清单
| 文件 | 作用 |
|------|------|
| `app/postgrest/docker-compose.multi.yml` | 100 个 PostgREST service（pg1→3101 … pg100→3200），由 `gen_multi.sh` 生成 |
| `app/postgrest/gen_multi.sh` | 生成脚本（改端口/凭据/连接池后重跑即可） |
| `/etc/nginx/conf.d/pigstyapi-multi.conf` | `/pgN/` 路径路由 + 每库 OpenAPI 端点 + API Key 鉴权 |
| `/etc/nginx/conf.d/pigstyapi.conf` | 仅保留 80→443 重定向 |
| `/usr/local/bin/pigstyapi-openapi-sync-all.sh` | 批量同步 appdb + pg1~pg100 的 OpenAPI.json 到 `/var/www/html/` |

### 7.2 启停
```bash
# 启动 100 个 PostgREST 实例
cd app/postgrest && docker compose -f docker-compose.multi.yml up -d
nginx -s reload
```

### 7.3 连接池调优
每个 PostgREST 连接池设为 `PGRST_DB_POOL: 2`（见 `gen_multi.sh`），100 实例共 ~200 个 PG 常驻连接，避免打满 `max_connections`。

### 7.4 OpenAPI 同步
手动刷新（crontab 定时同步未启用）：
```bash
/usr/local/bin/pigstyapi-openapi-sync-all.sh
```
或建表后直接 `GET /pgN/` 拿实时 schema（PostgREST 自带）。

### 7.5 权限说明
`dbuser_app` 经 superuser 在每个 `pgN` 库 `GRANT ALL ON SCHEMA public` 获得读写权限，**库的 owner 保持不变**。

### 7.6 安全隔离（方案 X）
PostgREST 仅暴露 `api` schema，业务表与 `create_table` RPC 均位于 `api`，
PostgreSQL 扩展自带的危险函数（`postgres_fdw_*`、`file_fdw_handler`、`show_trgm`、`show_limit` 等）位于 `public`，
**不会被 API 暴露**。OpenAPI 仅列出 `api` 下的表与 RPC，攻击面最小。

---

## 8. 限制与后续

- **单实例多库**：pg1~pg100 同属 `pg-meta` 实例，非物理隔离；如需强隔离可改 Pigsty 多集群。
- **无多用户体系**：当前为单一 API Key；如需多租户/按用户鉴权，可在 Nginx 与 PostgREST 间加 FastAPI 控制面。
- **压测占位库**：pg3~pg100 原为压测库，目前一并暴露，可按需停掉不常用实例降载。

详细方案与踩坑见 [pigsty-postgrest-nginx-api.md](pigsty-postgrest-nginx-api.md)。
