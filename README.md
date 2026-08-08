# Pigsty 使用教程

> Pigsty 是一套基于 Ansible 的 PostgreSQL 发行版与部署系统，集成了 PostGIS、TimescaleDB、向量插件（pgvector）等扩展，并内置监控（Grafana + Prometheus + Loki）、高可用（Patroni + etcd + HAProxy）、对象存储（MinIO）、Redis、Docker 等模块。
>
> 项目主页：https://pigsty.io ｜ 源码：https://github.com/pgsty/pigsty

---

## 目录

1. [项目结构概览](#1-项目结构概览)
2. [准备工作](#2-准备工作)
3. [安装 Pigsty](#3-安装-pigsty)
4. [配置与自检](#4-配置与自检)
5. [部署](#5-部署)
6. [核心对象概念](#6-核心对象概念)
7. [常用运维操作](#7-常用运维操作)
8. [Playbook 速查表](#8-playbook-速查表)
9. [配置模板](#9-配置模板)
10. [常见问题](#10-常见问题)

---

## 1. 项目结构概览

```
pigsty/
├── configure        # 配置向导：生成 pigsty.yml 与主机清单 (inventory)
├── bootstrap        # 引导：解压离线包、安装 Ansible 依赖
├── pigsty.yml       # 默认配置文件（沙箱/单机最小集）
├── ansible.cfg      # Ansible 配置
├── *.yml            # 顶层 Playbook（部署入口）
├── bin/             # 运维快捷脚本（node-add、pgsql-add 等）
├── conf/            # 配置模板（slim/fat/meta/infra/redis/mongo 等）
├── roles/           # Ansible 角色（node/pgsql/infra/pg_exporters/redis/minio/etcd...）
├── app/             # 应用层 Playbook 与示例
├── templates/       # 配置文件模板
├── files/           # 静态资源（证书、JSON、默认配置）
├── terraform/       # 云上基础设施（AWS/GCP/Aliyun 等）
└── vagrant/         # 本地虚拟机沙箱定义
```

**顶层 Playbook（部署入口）一览：**

| Playbook | 作用 |
|---|---|
| `deploy.yml` | 部署 `pigsty.yml` 中描述的所有内容（一站式入口） |
| `infra.yml` | 部署 infra 基础设施（Nginx/Grafana/Prometheus/Loki/Alertmanager 等） |
| `node.yml` | 初始化节点（安装包、配置内核、NTP、用户、监控 agent） |
| `pgsql.yml` | 部署 PostgreSQL 集群 |
| `redis.yml` | 部署 Redis 集群 |
| `minio.yml` / `etcd.yml` | 部署 MinIO 对象存储 / etcd 共识存储 |
| `pgsql-user.yml` | 创建/删除数据库用户与权限 |
| `pgsql-db.yml` | 创建/删除业务数据库 |
| `pgsql-monitor.yml` | 配置监控接入 |
| `pgsql-pitr.yml` | 时间点恢复（PITR） |
| `pgsql-migration.yml` | 数据迁移 |
| `node-rm.yml` / `pgsql-rm.yml` / `redis-rm.yml` / `etcd-rm.yml` / `minio-rm.yml` / `infra-rm.yml` | 卸载对应模块 |

**`bin/` 下常用运维脚本：**

| 脚本 | 作用 |
|---|---|
| `bin/node-add <ip...>` | 将节点加入 Pigsty 并初始化 |
| `bin/pgsql-add <cls>` | 创建 PostgreSQL 集群 |
| `bin/pgsql-add <cls> <ip...>` | 向集群追加副本 |
| `bin/pgsql-user` | 创建数据库用户 |
| `bin/pgsql-db` | 创建数据库 |
| `bin/pgsql-ext` | 安装扩展 |
| `bin/redis-add` / `bin/redis-rm` | 增减 Redis 节点 |
| `bin/repo-add` | 添加离线软件源 |

---

## 2. 准备工作

### 2.1 系统要求

- **操作系统**：RHEL / Rocky / Alma / CentOS 7/8/9，Ubuntu 22.04/24.04，Debian 12 等（x86_64 / aarch64）。
- **节点数量**：最小 1 节点（沙箱模式），生产建议至少 3 节点做高可用。
- **权限**：需要一个具备 `sudo` 权限的普通用户（root 也可用，但官方建议非 root 管理员）。
- **网络**：节点间可 SSH 互通，建议使用静态 IP 或 DNS 解析。
- **磁盘**：至少预留 100GB；离线部署需下载约 1~2GB 的离线包。

### 2.2 获取源码

```bash
# 在线一键安装（默认 v4.4.0）
curl -fsSL https://repo.pigsty.io/get | bash
cd ~/pigsty

# 安装指定版本
curl -fsSL https://repo.pigsty.io/get | bash -s v4.4.0

# 国内镜像源
curl -fsSL https://repo.pigsty.cc/get | bash
```

脚本会自动下载 `pigsty-<version>.tgz` 并解压到 `$HOME/pigsty`，随后若未检测到 Ansible 则自动执行 `./bootstrap`。

---

## 3. 安装 Pigsty

进入源码目录后，执行引导（通常 `install` 脚本已完成）：

```bash
cd ~/pigsty
./bootstrap      # 解压离线包 + 安装 ansible / ansible-playbook
```

`bootstrap` 会：

1. 解压 `files/pigsty-pkg-*.tgz` 离线软件包（离线环境必需）。
2. 通过本地 yum/apt 源安装 `ansible-core`、`python3` 及依赖。
3. 准备好 `ansible-playbook` 命令。

> 离线环境：将离线包放入 `files/` 后执行 `./bootstrap` 即可，无需公网。

---

## 4. 配置与自检

### 4.1 运行配置向导

```bash
./configure                  # 交互式自检 + 生成配置
./configure -i              # 强制重新生成 inventory
./configure -m slim         # 使用 slim 模板
./configure -m fat          # 使用 fat 模板
```

`configure` 脚本（基于 `bin/inventory_*` 系列）会：

- 探测本机 IP、网卡、CPU、内存、磁盘。
- 根据 `conf/` 下模板生成 `pigsty.yml` 与 `inventory.yml`（主机清单）。
- 执行 `preflight` 预检，输出环境检查结果。

### 4.2 配置文件核心结构（`pigsty.yml`）

```yaml
all:
  vars:
    version: v4.4.0
    admin_ip: 10.10.10.10          # 管理节点（infra）IP
    region: default
    node_tune: tiny                # 节点调优档位: tiny/oltp/olap/crit
    pg_conf: olap.yml              # PostgreSQL 参数模板
  children:
    infra:                         # 基础设施组
      hosts: { 10.10.10.10: { infra_seq: 1 } }
    pg-meta:                       # PostgreSQL 集群 pg-meta
      hosts:
        10.10.10.11: { pg_seq: 1, pg_role: primary }
        10.10.10.12: { pg_seq: 2, pg_role: replica }
      vars:
        pg_cluster: pg-meta
        pg_version: 17
        pg_users:
          - { name: dbuser_meta, password: DBUser.Meta , pgbouncer: true, roles: [dbrole_admin] }
        pg_databases:
          - { name: meta, owner: dbuser_meta }
```

**关键变量说明：**

| 变量 | 含义 |
|---|---|
| `admin_ip` | infra 节点 IP（监控/源/控制台所在） |
| `node_tune` | 节点内核调优：tiny / oltp / olap / crit |
| `pg_conf` | PG 参数模板：oltp.yml / olap.yml / tiny.yml |
| `pg_cluster` | 集群名（全局唯一） |
| `pg_seq` | 实例序号（1 为主，类推） |
| `pg_role` | 角色：primary / replica / offline / delayed |
| `pg_version` | PG 大版本（如 16/17/18） |
| `pg_users` | 数据库用户与权限定义 |
| `pg_databases` | 业务库定义 |

### 4.3 主机清单（inventory）

Pigsty 使用 Ansible inventory 定义节点分组。分组层级：

- `infra`：基础设施节点（必选，至少 1 个）。
- `pg-<cluster>`：PostgreSQL 集群组，组内 `pg_role` 区分主从。
- `redis-<cluster>`：Redis 集群组。
- `minio` / `etcd`：对象存储 / 共识存储组。

可通过 `bin/inventory_cmdb` / `bin/inventory_conf` 生成与校验清单。

---

## 5. 部署

### 5.1 一站式部署

```bash
./deploy.yml        # 部署 pigsty.yml 中描述的一切
```

`deploy.yml` 会依次执行：infra 基础设施 → node 节点初始化 → pgsql 集群 → 其他模块。

### 5.2 分步部署（生产推荐）

```bash
./infra.yml         # 1. 基础设施（监控、源、控制台）
./node.yml          # 2. 所有节点初始化
./etcd.yml          # 3. etcd 共识（高可用必需）
./pgsql.yml         # 4. PostgreSQL 集群
```

### 5.3 限定范围部署

```bash
./pgsql.yml -l pg-meta                 # 仅对 pg-meta 集群
./node.yml  -l 10.10.10.11             # 仅单节点
./pgsql.yml -l pg-meta -t pg_service   # 仅执行 pg_service 任务标签
```

部署完成后，可通过以下地址访问：

- **Grafana 监控面板**：`http://<admin_ip>:3000`（默认 admin / pigsty）
- **PgAdmin / 控制台**：`http://<admin_ip>:8080`
- **Prometheus**：`http://<admin_ip>:9090`
- **HAProxy 状态页**：`http://<pg_ip>:9101`

---

## 6. 核心对象概念

| 概念 | 说明 |
|---|---|
| **Infra 节点** | 承载监控、软件源、控制台的节点，是整套系统的大脑 |
| **Node** | 被纳管的机器；`node.yml` 负责系统级初始化 |
| **PG Cluster** | 一个 Patroni 管理的 PG 集群，含 1 主 N 副本 |
| **PG Instance** | 集群内单个 PG 实例，用 `pg_seq` 编号 |
| **etcd** | 分布式键值存储，用于 Patroni 选主与高可用 |
| **HAProxy** | 在 PG 实例前做读写/只读流量路由与故障转移 |
| **Pgbouncer** | 连接池，默认随 PG 部署 |
| **MinIO** | S3 兼容对象存储，用于备份与 PG 大对象 |

---

## 7. 常用运维操作

### 7.1 新增节点

```bash
bin/node-add 10.10.10.21 10.10.10.22
```

### 7.2 创建 PostgreSQL 集群

在 `pigsty.yml` 中定义 `pg-<name>` 组后：

```bash
bin/node-add pg-newcluster          # 先初始化该组节点
bin/pgsql-add pg-newcluster         # 再创建集群
```

### 7.3 向集群追加副本

```bash
bin/node-add 10.10.10.13            # 先加节点
bin/pgsql-add pg-meta 10.10.10.13   # 追加为副本
```

### 7.4 创建用户与数据库

```bash
bin/pgsql-user pg-meta dbuser_app   # 创建用户
bin/pgsql-db   pg-meta appdb        # 创建数据库
```

亦可编辑 `pigsty.yml` 中 `pg_users` / `pg_databases` 后重新执行 `./pgsql.yml -l <cls>`。

### 7.5 安装扩展

```bash
bin/pgsql-ext pg-meta pgvector postgis timescaledb
```

### 7.6 连接数据库

部署后可直接使用 `psql` 连接（通过 HAProxy 路由）：

```bash
psql postgres://dbuser_meta:DBUser.Meta@10.10.10.10:5436/meta   # 读写（primary）
psql postgres://dbuser_meta:DBUser.Meta@10.10.10.10:5434/meta   # 只读（replica）
```

端口约定（示例，以实际配置为准）：`5432` 直连、`5433` 默认池、`5434` 只读服务、`5436` 读写服务、`5438` 离线/管理。

### 7.7 卸载模块

```bash
./pgsql-rm.yml -l pg-meta     # 卸载 PG 集群
./node-rm.yml  -l 10.10.10.21 # 移除节点
./infra-rm.yml               # 卸载基础设施
```

---

## 8. Playbook 速查表

| 命令 | 说明 |
|---|---|
| `./infra.yml` | 部署监控/源/控制台 |
| `./node.yml [-l <sel>]` | 初始化节点 |
| `./pgsql.yml [-l <cls>] [-t <tag>]` | 部署/维护 PG |
| `./redis.yml` | 部署 Redis |
| `./minio.yml` / `./etcd.yml` | 对象存储 / etcd |
| `./pgsql-user.yml` | 用户管理 |
| `./pgsql-db.yml` | 库管理 |
| `./pgsql-monitor.yml` | 监控接入 |
| `./pgsql-pitr.yml` | 时间点恢复 |
| `./pgsql-migration.yml` | 迁移 |
| `*-rm.yml` | 对应模块卸载 |

通用参数：

- `-l <selector>`：限定执行范围（组名 / IP / 复合选择器，如 `10.0.0.1,&pg-meta`）
- `-t <tag>`：仅执行特定任务标签
- `-e k=v`：覆盖变量
- `--check`：干跑（dry run）

---

## 9. 配置模板

`conf/` 目录提供多种预设模板，通过 `./configure -m <name>` 选用：

| 模板 | 用途 |
|---|---|
| `slim.yml` | 最小集（单机 PG + 基础监控） |
| `fat.yml` | 全功能（PG + Redis + MinIO + Mongo + 监控） |
| `meta.yml` / `infra.yml` | infra 节点专用 |
| `demo/*.yml` | 演示用多集群示例 |
| `app/*.yml` | 应用层示例（含业务应用部署） |
| `pgsql.yml` / `redis.yml` / `mongo.yml` / `mysql.yml` 等 | 各类数据库模板 |

---

## 10. 常见问题

**Q1：需要 root 吗？**
官方建议用具备 sudo 的非 root 管理员账号；root 也能跑但会提示告警。

**Q2：离线环境如何部署？**
将 `pigsty-pkg-<os>-<arch>.tgz` 放入 `files/`，运行 `./bootstrap` 与 `./configure`（离线模式），无需公网访问。

**Q3：部署失败如何排查？**
- 查看 Ansible 输出中的 `FAILED` 任务。
- 运行 `bin/validate` 校验环境。
- 检查 `preflight` 报告中的内核/磁盘/端口冲突。

**Q4：如何升级 Pigsty？**
重新执行 `install` 下载新版本，使用 `rsync` 同步源码（保留 `pigsty.yml` 与 `files/pki`），再按需重跑对应 playbook。

**Q5：监控账号？**
Grafana / Prometheus 默认 `admin` / `pigsty`，可在 `pigsty.yml` 的 `grafana_admin_password` 等变量中修改。

---

> 更多细节参见官方文档：https://pigsty.io/docs ｜ 演示环境：https://demo.pigsty.io

---

## 11. 运维 Web 控制台（`admin/`）

`admin/` 目录内置一个**零依赖**的运维 Web 控制台，把常用运维操作（节点/集群/用户/库/扩展的增删，
以及集群/备份/库存的状态查看）封装成网页界面，方便日常巡检与操作。

```bash
cd /root/pigsty/admin

# 只读模式（仅查看状态）
python3 app.py

# 启用危险操作（创建/删除等需要页面二次确认）
ADMIN_DANGER=1 python3 app.py
```

启动后浏览器访问 `http://127.0.0.1:8080`。详细用法见 [admin/README.md](admin/README.md)。

> ⚠️ 危险操作默认禁用，仅 `ADMIN_DANGER=1` 启动后可用，且每次执行需页面二次确认。
