# Pigsty Admin WebUI

把 Pigsty 常用运维操作封装成一个轻量 Web 控制台。**零依赖**（仅用 Python 标准库），
本质是在 `bin/` 运维脚本与 `pig` / `ansible-playbook` 之上套了一层网页界面。

## 功能

### 📊 状态查看（只读，默认开启）
- 集群列表：`pig pt list -f json`（列出所有 Patroni 集群名）
- 集群 HA 状态：`pig pt status -o json`（Leader / 时间线 / 成员角色 / 复制延迟）
- 集群状态：`pig pg list`
- 实例状态：`pig pg status`
- 实例角色：`pig pg role`
- 当前连接：`pig pg ps -a`
- 数据库列表：`pig pg psql -c "SELECT datname ..."`（本实例所有业务库）
- 备份列表：`pig pb list -o json`（备份集时间 / 类型 / LSN / 大小）
- 备份信息：`pig pb info`
- 本地仓库：`files/` 目录
- 主机清单：解析 `pigsty.yml`

### 🛠 运维操作（危险，需显式启用）
| 操作 | 对应命令 | 说明 |
|------|----------|------|
| 新增节点 | `bin/node-add` | 向库存追加并初始化节点 |
| 移除节点 | `bin/node-rm` | 从集群移除节点（危险） |
| 创建 PG 集群 / 追加副本 | `bin/pgsql-add` | 初始化集群或在线加副本 |
| 卸载 PG 集群 | `bin/pgsql-rm` | 删除集群（危险） |
| 创建用户 | `bin/pgsql-user` | 新建数据库用户 |
| 创建库 | `bin/pgsql-db` | 新建数据库 |
| 安装扩展 | `bin/pgsql-ext` | 安装 PostgreSQL 扩展 |
| 新增 Redis 集群 | `bin/redis-add` | 初始化 Redis 集群 |
| 重载 PG 服务 | `pig do pgsql-svc <sel>` | 重载集群/实例服务 |
| 刷新 HBA 规则 | `pig do pgsql-hba <sel>` | 重新渲染并加载 pg_hba |
| Patroni 主从切换 | `pig pt switchover <cls>` | 触发主从切换（高风险） |
| 添加远程监控目标 | `bin/pgmon-add` | 把集群加入监控 |
| 移除远程监控目标 | `bin/pgmon-rm` | 移出监控（高风险） |

> 所有危险操作在 Web 上会弹窗二次确认，且默认 **禁用**，需用 `ADMIN_DANGER=1` 启动。

## 创建数据库操作流程

Pigsty 采用「配置即真相」：数据库必须先在 `pigsty.yml` 中声明，才能通过 playbook 创建。
直接凭空建库会被拒绝，并报 `define database xxx in pg_databases first`。

1. **在 `pigsty.yml` 声明库**（以 `pg-meta` 集群为例，在其 `vars.pg_databases` 下追加）：
   ```yaml
   pg_databases:
     - name: meta
       ...
     - name: mydb          # 新增
       comment: "created via admin webui"
   ```
   可一并指定 `owner` / `extensions` / `schemas` / `baseline` 等。

2. **通过 Web 创建**：在「运维操作 → 创建库」填写 **集群名**（如 `pg-meta`）与
   **库名**（如 `mydb`），确认执行，等价于：
   ```bash
   ./pgsql-db.yml -l pg-meta -e dbname=mydb
   ```
   该 playbook 会自动：建立数据库、注册 pgbouncer 入口、注册 Grafana 数据源。

3. **验证**：在「状态查看 → 数据库列表」中即可看到新建的库。

> 若仅需临时建库、不进入版本管理，可绕过配置直接用 `psql` 创建，但属于配置漂移，
> 后续重跑 `pgsql.yml` 不会自动纳管该库。

## 启动

```bash
cd /root/pigsty/admin

# 只读模式（仅查看状态，默认）
python3 app.py

# 开启危险操作按钮
ADMIN_DANGER=1 python3 app.py

# 自定义监听地址与端口
ADMIN_HOST=0.0.0.0 ADMIN_PORT=9000 ADMIN_DANGER=1 python3 app.py
```

启动后浏览器访问：`http://127.0.0.1:8080`

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ADMIN_HOST` | `127.0.0.1` | 监听地址（**请勿直接暴露公网**） |
| `ADMIN_PORT` | `8080` | 监听端口 |
| `ADMIN_DANGER` | `0` | 置 `1` 启用危险操作按钮 |
| `PIGSTY_HOME` | `/root/pigsty` | Pigsty 安装目录 |

## 生产部署（systemd 自启 + Nginx 反代 + Basic Auth）

部署配置统一放在仓库 `admin/deploy/` 目录下，便于纳入 git 版本管理：

```
admin/deploy/
├── pigsty-admin.service     # systemd 服务单元
├── admin-nginx.conf         # Nginx /admin/ 反代片段（include 引入）
└── admin.htpasswd.example   # 密码文件示例（不含真实密码）
```

> 真实的 `/etc/nginx/admin.htpasswd` 含明文凭据，已被 `.gitignore` 忽略，不会提交到 git。

### 1. 从仓库同步到 /etc

```bash
# systemd 服务
cp admin/deploy/pigsty-admin.service /etc/systemd/system/pigsty-admin.service
systemctl daemon-reload
systemctl enable --now pigsty-admin.service
systemctl status pigsty-admin.service

# Nginx 反代：把片段 include 到 home.conf 的 server 块内
cp admin/deploy/admin-nginx.conf /etc/nginx/conf.d/admin.conf
# （或：将 admin-nginx.conf 内容 include 进 /etc/nginx/conf.d/home.conf 的 server 块）
nginx -t && systemctl reload nginx
```

### 2. 生成 Basic Auth 密码

```bash
# 从示例复制并生成真实密码文件（权限 640，属主 root:nginx）
htpasswd -c /etc/nginx/admin.htpasswd admin    # 交互输入密码
chmod 640 /etc/nginx/admin.htpasswd
chown root:nginx /etc/nginx/admin.htpasswd
systemctl reload nginx
```

`/admin/` location 配置（来自 `admin/deploy/admin-nginx.conf`）：

```nginx
location /admin/ {
    auth_basic           "Pigsty Admin 运维控制台";
    auth_basic_user_file /etc/nginx/admin.htpasswd;

    proxy_pass http://127.0.0.1:9000/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 900s;
    proxy_send_timeout 900s;
    proxy_no_cache 1;
    add_header Cache-Control "no-store, no-cache, must-revalidate" always;
}
```

### 3. 访问凭据

浏览器访问 `http://<vps-ip>/admin/` 会弹出账号密码框，输入你用 `htpasswd` 设置的
用户名/密码（默认用户名为 `admin`）即可进入。

> 修改密码：`htpasswd /etc/nginx/admin.htpasswd admin`，然后 `systemctl reload nginx`。
> 查看当前密码哈希（不可逆向）：`cat /etc/nginx/admin.htpasswd`。

## 安全建议

- 通过 Nginx 反代 + Basic Auth 暴露到公网，已在本仓库部署中启用；
  不要将 `app.py` 直接以 `ADMIN_HOST=0.0.0.0` 裸奔在公网端口。
- 危险操作（删除集群/节点、建用户库等）会调用 Ansible Playbook，
  执行前务必在弹窗中核对参数，并确认有近期备份。
- 若需更严格控制，可额外在 Nginx `/admin/` location 加 `allow/deny` IP 白名单。
- 修改 Basic Auth 密码或回收访问权限，只需更新 `/etc/nginx/admin.htpasswd` 并重载 Nginx。

## 文件结构

```
admin/
├── app.py            # 后端：零依赖 HTTP 服务 + 运维操作封装
├── static/
│   ├── index.html    # 前端页面
│   ├── style.css     # 样式
│   └── app.js        # 前端逻辑
└── README.md         # 本文
```
