# Pigsty Admin WebUI

把 Pigsty 常用运维操作封装成一个轻量 Web 控制台。**零依赖**（仅用 Python 标准库），
本质是在 `bin/` 运维脚本与 `pig` / `ansible-playbook` 之上套了一层网页界面。

## 功能

### 📊 状态查看（只读，默认开启）
- 集群状态：`pig pg list`
- 备份信息：`pig pb info`
- 本地仓库：`files/` 目录
- 主机清单：解析 `pigsty.yml`

### 🛠 运维操作（危险，需显式启用）
| 操作 | 对应脚本 | 说明 |
|------|----------|------|
| 新增节点 | `bin/node-add` | 向库存追加并初始化节点 |
| 移除节点 | `bin/node-rm` | 从集群移除节点（危险） |
| 创建 PG 集群 / 追加副本 | `bin/pgsql-add` | 初始化集群或在线加副本 |
| 卸载 PG 集群 | `bin/pgsql-rm` | 删除集群（危险） |
| 创建用户 | `bin/pgsql-user` | 新建数据库用户 |
| 创建库 | `bin/pgsql-db` | 新建数据库 |
| 安装扩展 | `bin/pgsql-ext` | 安装 PostgreSQL 扩展 |
| 新增 Redis 集群 | `bin/redis-add` | 初始化 Redis 集群 |

> 所有危险操作在 Web 上会弹窗二次确认，且默认 **禁用**，需用 `ADMIN_DANGER=1` 启动。

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

推荐通过 systemd 托管，并由 Nginx 反向代理到 `/admin/` 子路径，前缀 Basic Auth 保护。

### 1. systemd 自启服务

已提供 `/etc/systemd/system/pigsty-admin.service`（以 `root` 运行，监听 `127.0.0.1:9000`，
启用危险操作并 `Restart=on-failure`）：

```bash
systemctl daemon-reload
systemctl enable --now pigsty-admin.service
systemctl status pigsty-admin.service
```

### 2. Nginx 反代 + Basic Auth

`/etc/nginx/conf.d/home.conf` 中的 `/admin/` location：

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

应用配置：`nginx -t && systemctl reload nginx`

### 3. 访问凭据（Basic Auth）

密码文件：`/etc/nginx/admin.htpasswd`（属主 `root:nginx`，权限 `640`）。

| 项目 | 值 |
|------|-----|
| 用户名 | `admin` |
| 密码 | `Uu0FuhKIaj0a66du` |

> ⚠️ 此为首次部署时自动生成的默认密码，请尽快修改：
> ```bash
> htpasswd /etc/nginx/admin.htpasswd admin   # 交互输入新密码
> systemctl reload nginx
> ```
> 查看当前密码哈希（不可逆向，仅用于确认文件存在）：
> `cat /etc/nginx/admin.htpasswd`

访问地址：`http://<vps-ip>/admin/`，浏览器会弹出账号密码框，输入上述凭据进入。

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
