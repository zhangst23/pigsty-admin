#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pigsty Admin WebUI —— 常用运维操作的轻量 Web 控制台

零依赖：仅使用 Python 标准库（http.server / subprocess / json）。
本质是把 bin/ 下的运维脚本与 pig / ansible-playbook 封装成 Web 界面。

⚠️ 安全提示：
  - 危险操作（创建/删除集群、节点、用户、库、扩展等）默认禁止，
    需要显式通过「确认模式」开关（设置环境变量 ADMIN_DANGER=1）才能启用。
  - 默认监听 127.0.0.1:8080，请勿在公网开放。可通过环境变量调整：
      ADMIN_HOST   (默认 127.0.0.1)
      ADMIN_PORT   (默认 8080)
      ADMIN_DANGER (默认 0，置 1 启用危险操作按钮)

启动：
  cd /root/pigsty/admin
  python3 app.py
  # 或允许危险操作：
  ADMIN_DANGER=1 python3 app.py
"""

import os
import sys
import json
import time
import shlex
import signal
import subprocess
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
PIGSTY_HOME = os.environ.get("PIGSTY_HOME", "/root/pigsty")
BIN_DIR = os.path.join(PIGSTY_HOME, "bin")
ADMIN_HOST = os.environ.get("ADMIN_HOST", "127.0.0.1")
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8080"))
DANGER_ENABLED = os.environ.get("ADMIN_DANGER", "0") == "1"
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# 危险操作白名单：命令必须精确匹配这里的 argv 前缀（第一个词为 bin/ 脚本名或 pig/ansible）
DANGER_WHITELIST = [
    "node-add", "node-rm",
    "pgsql-add", "pgsql-rm",
    "pgsql-user", "pgsql-db", "pgsql-ext",
    "pgsql-svc", "pgsql-hba",
    "pgmon-add", "pgmon-rm",
    "redis-add",
]

# 命令操作（/api/shell）允许的命令前缀。
# 仅允许与 Pigsty 运维相关的命令，避免任意 shell 命令执行风险。
SHELL_ALLOW_PREFIX = [
    "cd ",                          # 仅允许 cd /root/pigsty && 形式需另行约束
    "ansible-playbook ",
    "ansible ",
    "pig ",
    "pg ",
    "psql ",
    "pgbackrest ",
    "patronictl ",
    "pg_ctl ",
    "systemctl ",
    "df ", "free ", "uptime ", "ps ", "top ", "ip ", "ss ",
]


def shell_allowed(command):
    """校验命令是否被允许执行（防任意命令执行）。返回 (ok, reason)。"""
    if not command:
        return False, "命令为空"
    # 允许的特殊前缀：cd 到 pigsty 目录后再执行运维命令（最常见的用法）
    CD_PREFIX = "cd /root/pigsty && "
    rest = command
    if command.startswith(CD_PREFIX):
        rest = command[len(CD_PREFIX):].strip()
    # 禁止常见高危/逃逸字符与 shell 元命令（我们的命令只允许空格参数，不解释 shell）
    forbidden = [";", "||", "|", "$((", "${", "`", ">", ">>", "<",
                 "sudo", "rm ", "mv ", "cp ", "chmod", "chown", "kill",
                 "curl", "wget", "nc ", "ssh", "scp", "echo", "cat ", "tee"]
    for f in forbidden:
        if f in rest:
            return False, "命令含不允许的字符/子串: '{}'（命令操作仅支持单次调用，不含管道/重定向/复合命令）".format(f)
    # 必须以白名单前缀开头（rest 或原命令）
    target = rest if rest != command else command
    for p in SHELL_ALLOW_PREFIX:
        if target.startswith(p.strip()):
            return True, ""
    return False, ("命令不在允许列表内（仅允许: " +
                   ", ".join(p.strip() for p in SHELL_ALLOW_PREFIX) + "）")

# 简单的内存日志（最近 200 条）
LOG_LIMIT = 200
_op_log = []


def log_op(kind, cmd, ok, msg):
    _op_log.insert(0, {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        "cmd": cmd,
        "ok": ok,
        "msg": msg[:500],
    })
    del _op_log[LOG_LIMIT:]


# --------------------------------------------------------------------------- #
# 命令执行辅助
# --------------------------------------------------------------------------- #
def run(cmd_args, timeout=600):
    """执行一个命令，返回 (rc, stdout, stderr)。"""
    try:
        proc = subprocess.run(
            cmd_args,
            cwd=PIGSTY_HOME,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=True,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or ""), "命令执行超时 ({}s)".format(timeout)
    except FileNotFoundError as e:
        return 127, "", "命令不存在: {}".format(e)


def run_script(script, args, timeout=900):
    """运行 bin/ 下的脚本，参数为字符串列表。"""
    path = os.path.join(BIN_DIR, script)
    if not os.path.exists(path):
        return 127, "", "脚本不存在: {}".format(path)
    return run([path] + [str(a) for a in args], timeout=timeout)


# --------------------------------------------------------------------------- #
# 业务逻辑：只读查询
# --------------------------------------------------------------------------- #
def status_clusters():
    rc, out, err = run(["pig", "pg", "list"])
    return {"rc": rc, "text": out + (("\n" + err) if err else ""), "error": err.strip()}


def status_pg_status():
    """本地 PostgreSQL 实例状态（pig pg status）。"""
    rc, out, err = run(["pig", "pg", "status"])
    return {"rc": rc, "text": out + (("\n" + err) if err else ""), "error": err.strip()}


def status_pg_role():
    """检测本地实例角色（primary / replica），以 JSON 结构化输出。"""
    rc, out, err = run(["pig", "pg", "role", "-V"])
    return {"rc": rc, "text": out + (("\n" + err) if err else ""), "error": err.strip()}


def status_pg_ps():
    """当前连接（pig pg ps）。"""
    rc, out, err = run(["pig", "pg", "ps", "-a"])
    return {"rc": rc, "text": out + (("\n" + err) if err else ""), "error": err.strip()}


def status_pg_dbs():
    """列出本实例所有非模板数据库（pig pg psql）。"""
    rc, out, err = run(["pig", "pg", "psql", "postgres", "-c",
                        "SELECT datname FROM pg_database "
                        "WHERE NOT datistemplate ORDER BY datname;"])
    if rc != 0:
        return {"rc": rc, "text": out, "error": err.strip() or "查询数据库列表失败"}
    # psql 输出含表头/分隔线/行数，简单清洗：取 datname 列
    lines = out.splitlines()
    names = []
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("-") or ln.startswith("datname") \
           or ln.startswith("(") or ln.endswith("rows)") \
           or ln.lower().startswith("time:") or ln.lower().startswith("select "):
            continue
        names.append(ln)
    if not names:
        return {"rc": 0, "text": "(未发现业务数据库)", "error": ""}
    return {"rc": 0,
            "text": "共 {} 个数据库:\n".format(len(names)) +
                    "\n".join("  • {}".format(n) for n in names),
            "error": ""}


def status_pt_clusters():
    """列出所有 Patroni 集群名称（pig pt list -f json）。"""
    rc, out, err = run(["pig", "pt", "list", "-f", "json"])
    if rc != 0:
        # 回退：尝试不带 -f（旧版 pig），用文本解析
        rc2, out2, err2 = run(["pig", "pt", "list"])
        if rc2 == 0:
            return {"rc": 0, "text": out2, "error": ""}
        return {"rc": rc, "text": out, "error": err.strip() or "pig pt list 执行失败"}
    try:
        rows = json.loads(out) if out.strip().startswith("[") else []
    except json.JSONDecodeError:
        return {"rc": 0, "text": out, "error": ""}
    names = []
    for r in rows:
        c = r.get("Cluster") or r.get("cluster")
        if c and c not in names:
            names.append(c)
    if not names:
        return {"rc": 0, "text": "(未发现任何 Patroni 集群)", "error": ""}
    return {"rc": 0,
            "text": "共 {} 个集群:\n".format(len(names)) +
                    "\n".join("  • {}".format(n) for n in names),
            "error": ""}


def status_pt_status():
    """Patroni HA 集群状态（pig pt status -o json）"""
    rc, out, err = run(["pig", "pt", "status", "-o", "json"])
    if rc != 0:
        return {"rc": rc, "text": out, "error": err.strip() or "pig pt status 执行失败"}
    try:
        d = json.loads(out)
        data = d.get("data", {})
    except json.JSONDecodeError:
        return {"rc": 0, "text": out, "error": ""}
    if not data:
        return {"rc": 0, "text": "(未获取到集群状态)", "error": ""}
    lines = []
    lines.append("集群: {}".format(data.get("cluster", "?")))
    lines.append("Leader: {}".format(data.get("leader", "?")))
    lines.append("时间线 TL: {}".format(data.get("timeline", "?")))
    lines.append("成员数: {}".format(data.get("member_count", "?")))
    lines.append("服务运行: {}".format("是" if data.get("service_running") else "否"))
    lines.append("")
    lines.append("Members:")
    for m in data.get("members", []):
        lag = m.get("lag")
        lag_s = "lag={}".format(lag) if lag is not None else "lag=0"
        lines.append("  • {} [{}] {} ({}) {}".format(
            m.get("member", "?"), m.get("role", "?"),
            m.get("host", "?"), m.get("state", "?"), lag_s))
    return {"rc": 0, "text": "\n".join(lines), "error": ""}


def status_pb_list():
    """pgBackRest 备份列表（pig pb list -o json）"""
    rc, out, err = run(["pig", "pb", "list", "-o", "json"])
    if rc != 0:
        return {"rc": rc, "text": out, "error": err.strip() or "pig pb list 执行失败"}
    try:
        d = json.loads(out)
        data = d.get("data", {})
    except json.JSONDecodeError:
        return {"rc": 0, "text": out, "error": ""}
    stanzas = data.get("backups", []) if isinstance(data, dict) else []
    if not stanzas:
        return {"rc": 0, "text": "(未发现备份 stanza)", "error": ""}
    lines = []
    total = 0
    for st in stanzas:
        name = st.get("name", "?")
        status = st.get("status", {}).get("message", "?")
        bks = st.get("backup", [])
        total += len(bks)
        lines.append("Stanza: {}  状态: {}".format(name, status))
        for b in bks:
            ts = b.get("timestamp", {})
            import datetime as _dt
            try:
                t = _dt.datetime.fromtimestamp(ts.get("start", 0)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                t = str(ts.get("start", "?"))
            size = b.get("info", {}).get("size", 0)
            lines.append("  • {} [{}] {}  size={}B  label={}".format(
                t, b.get("type", "?"), b.get("lsn", {}).get("stop", "?"),
                size, b.get("label", "?")))
    lines.insert(0, "共 {} 个 stanza, {} 个备份集:\n".format(len(stanzas), total))
    return {"rc": 0, "text": "\n".join(lines), "error": ""}


def status_backup():
    rc, out, err = run(["pig", "pb", "info"])
    return {"rc": rc, "text": out + (("\n" + err) if err else ""), "error": err.strip()}


def status_repo():
    rc, out, err = run(["pig", "repo", "status"])
    if rc != 0:
        # repo 子命令可能不存在，回退到 ls
        rc, out, err = run(["ls", "-la", os.path.join(PIGSTY_HOME, "files")])
    return {"rc": rc, "text": out, "error": err.strip()}


def list_inventory():
    """解析 pigsty.yml 的 all.children 主机清单（仅展示IP/别名）。"""
    try:
        import yaml  # 标准库没有 yaml，尝试 import；失败则用 grep 粗略解析
    except ImportError:
        yaml = None

    if yaml is None:
        rc, out, err = run(["grep", "-nE", "hosts:|ip:|^[a-z0-9_-]+:",
                            os.path.join(PIGSTY_HOME, "pigsty.yml")])
        return {"rc": rc, "text": out, "error": "未安装 pyyaml，已用 grep 粗略展示"}

    try:
        with open(os.path.join(PIGSTY_HOME, "pigsty.yml")) as f:
            data = yaml.safe_load(f)
        lines = []
        children = data.get("all", {}).get("children", {})
        for group, gdata in children.items():
            lines.append("## group: {}".format(group))
            hosts = gdata.get("hosts", {}) if isinstance(gdata, dict) else {}
            for name, h in (hosts or {}).items():
                ip = h.get("ip") if isinstance(h, dict) else ""
                lines.append("  - {}  ({})".format(name, ip))
        return {"rc": 0, "text": "\n".join(lines), "error": ""}
    except Exception as e:
        return {"rc": 1, "text": "", "error": str(e)}


def api_status():
    return [
        {"id": "pt_clusters", "name": "集群列表 (pig pt list)", "fn": status_pt_clusters},
        {"id": "pt_status", "name": "集群HA状态 (pig pt status)", "fn": status_pt_status},
        {"id": "clusters", "name": "集群状态 (pig pg list)", "fn": status_clusters},
        {"id": "pg_status", "name": "实例状态 (pig pg status)", "fn": status_pg_status},
        {"id": "pg_role", "name": "实例角色 (pig pg role)", "fn": status_pg_role},
        {"id": "pg_ps", "name": "当前连接 (pig pg ps)", "fn": status_pg_ps},
        {"id": "pg_dbs", "name": "数据库列表 (pig pg psql)", "fn": status_pg_dbs},
        {"id": "pb_list", "name": "备份列表 (pig pb list)", "fn": status_pb_list},
        {"id": "backup", "name": "备份信息 (pig pb info)", "fn": status_backup},
        {"id": "repo", "name": "本地仓库 (files)", "fn": status_repo},
        {"id": "inventory", "name": "主机清单 (pigsty.yml)", "fn": list_inventory},
    ]


# --------------------------------------------------------------------------- #
# 业务逻辑：危险操作
# --------------------------------------------------------------------------- #
def op_node_add(params):
    ips = params.get("ips", "")
    targets = [x.strip() for x in ips.replace(",", " ").split() if x.strip()]
    if not targets:
        return 1, "", "请提供至少一个节点 IP"
    rc, out, err = run_script("node-add", targets)
    return rc, out, err


def op_node_rm(params):
    ips = params.get("ips", "")
    targets = [x.strip() for x in ips.replace(",", " ").split() if x.strip()]
    if not targets:
        return 1, "", "请提供至少一个节点 IP"
    rc, out, err = run_script("node-rm", targets)
    return rc, out, err


def op_pg_add(params):
    cls = (params.get("cluster") or "").strip()
    if not cls:
        return 1, "", "请提供集群名称"
    ips = [x.strip() for x in params.get("ips", "").replace(",", " ").split() if x.strip()]
    rc, out, err = run_script("pgsql-add", [cls] + ips)
    return rc, out, err


def op_pg_rm(params):
    cls = (params.get("cluster") or "").strip()
    if not cls:
        return 1, "", "请提供集群名称"
    rc, out, err = run_script("pgsql-rm", [cls])
    return rc, out, err


def op_pg_user(params):
    cls = (params.get("cluster") or "").strip()
    user = (params.get("user") or "").strip()
    if not cls or not user:
        return 1, "", "请提供集群名称与用户名"
    rc, out, err = run_script("pgsql-user", [cls, user])
    return rc, out, err


def op_pg_db(params):
    cls = (params.get("cluster") or "").strip()
    db = (params.get("db") or "").strip()
    if not cls or not db:
        return 1, "", "请提供集群名称与库名"
    rc, out, err = run_script("pgsql-db", [cls, db])
    return rc, out, err


def op_pg_ext(params):
    cls = (params.get("cluster") or "").strip()
    exts = [x.strip() for x in params.get("exts", "").replace(",", " ").split() if x.strip()]
    if not cls:
        return 1, "", "请提供集群名称"
    rc, out, err = run_script("pgsql-ext", [cls] + exts)
    return rc, out, err


def op_redis_add(params):
    cls = (params.get("cluster") or "").strip()
    if not cls:
        return 1, "", "请提供 Redis 集群名称"
    rc, out, err = run_script("redis-add", [cls])
    return rc, out, err


def op_pg_svc(params):
    sel = (params.get("sel") or "").strip()
    if not sel:
        return 1, "", "请提供目标 (集群名 / 选择器)"
    rc, out, err = run(["pig", "do", "pgsql-svc", sel])
    return rc, out, err


def op_pg_hba(params):
    sel = (params.get("sel") or "").strip()
    if not sel:
        return 1, "", "请提供目标 (集群名 / 选择器)"
    rc, out, err = run(["pig", "do", "pgsql-hba", sel])
    return rc, out, err


def op_pg_switchover(params):
    cls = (params.get("cluster") or "").strip()
    if not cls:
        return 1, "", "请提供集群名称"
    rc, out, err = run(["pig", "pt", "switchover", cls, "--force"])
    return rc, out, err


def op_pgmon_add(params):
    cls = (params.get("cluster") or "").strip()
    if not cls:
        return 1, "", "请提供集群名称"
    rc, out, err = run_script("pgmon-add", [cls])
    return rc, out, err


def op_pgmon_rm(params):
    cls = (params.get("cluster") or "").strip()
    if not cls:
        return 1, "", "请提供集群名称"
    rc, out, err = run_script("pgmon-rm", [cls])
    return rc, out, err


def op_citus_add(params):
    cls = (params.get("cluster") or "").strip()
    if not cls:
        return 1, "", "请提供 Citus 集群名称"
    rc, out, err = run_script("citus-add", [cls])
    return rc, out, err


def api_ops():
    return [
        {"id": "node_add", "name": "新增节点 (node-add)", "fn": op_node_add,
         "fields": [{"key": "ips", "label": "节点 IP（空格/逗号分隔）", "required": True}]},
        {"id": "node_rm", "name": "移除节点 (node-rm)", "fn": op_node_rm,
         "fields": [{"key": "ips", "label": "节点 IP（空格/逗号分隔）", "required": True}]},
        {"id": "pg_add", "name": "创建 PG 集群 / 追加副本 (pgsql-add)", "fn": op_pg_add,
         "fields": [
             {"key": "cluster", "label": "集群名", "required": True},
             {"key": "ips", "label": "新实例 IP（可空=用库存定义）", "required": False},
         ]},
        {"id": "pg_rm", "name": "卸载 PG 集群 (pgsql-rm)", "fn": op_pg_rm,
         "fields": [{"key": "cluster", "label": "集群名", "required": True}]},
        {"id": "pg_user", "name": "创建用户 (pgsql-user)", "fn": op_pg_user,
         "fields": [
             {"key": "cluster", "label": "集群名", "required": True},
             {"key": "user", "label": "用户名", "required": True},
         ]},
        {"id": "pg_db", "name": "创建库 (pgsql-db)", "fn": op_pg_db,
         "fields": [
             {"key": "cluster", "label": "集群名", "required": True},
             {"key": "db", "label": "库名", "required": True},
         ]},
        {"id": "pg_ext", "name": "安装扩展 (pgsql-ext)", "fn": op_pg_ext,
         "fields": [
             {"key": "cluster", "label": "集群名", "required": True},
             {"key": "exts", "label": "扩展名（空格分隔，可空）", "required": False},
         ]},
        {"id": "redis_add", "name": "新增 Redis 集群 (redis-add)", "fn": op_redis_add,
         "fields": [{"key": "cluster", "label": "集群名", "required": True}]},
        {"id": "pg_svc", "name": "重载 PG 服务 (pg do pgsql-svc)", "fn": op_pg_svc,
         "fields": [{"key": "sel", "label": "目标 (集群名/选择器，如 pg-meta)", "required": True}]},
        {"id": "pg_hba", "name": "刷新 HBA 规则 (pg do pgsql-hba)", "fn": op_pg_hba,
         "fields": [{"key": "sel", "label": "目标 (集群名/选择器，如 pg-meta)", "required": True}]},
        {"id": "pg_switchover", "name": "Patroni 主从切换 (pig pt switchover)", "fn": op_pg_switchover,
         "fields": [{"key": "cluster", "label": "集群名 (如 pg-meta)", "required": True}]},
        {"id": "pgmon_add", "name": "添加远程监控目标 (pgmon-add)", "fn": op_pgmon_add,
         "fields": [{"key": "cluster", "label": "集群名", "required": True}]},
        {"id": "pgmon_rm", "name": "移除远程监控目标 (pgmon-rm)", "fn": op_pgmon_rm,
         "fields": [{"key": "cluster", "label": "集群名", "required": True}]},
    ]


# --------------------------------------------------------------------------- #
# HTTP 处理
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "PigstyAdmin/1.0"

    def log_message(self, fmt, *args):  # 静默默认访问日志
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 禁止缓存，避免更新后浏览器仍用旧版前端
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # 静态资源
        if path in ("/", "/index.html"):
            self._send_file(os.path.join(STATIC_DIR, "index.html"),
                            "text/html; charset=utf-8")
            return
        if path == "/app.js":
            self._send_file(os.path.join(STATIC_DIR, "app.js"),
                            "application/javascript; charset=utf-8")
            return
        if path == "/style.css":
            self._send_file(os.path.join(STATIC_DIR, "style.css"),
                            "text/css; charset=utf-8")
            return

        # API
        if path == "/api/config":
            # 注意：不要将函数对象 (fn) 序列化，只返回前端需要的元数据
            status_meta = [{"id": s["id"], "name": s["name"]} for s in api_status()]
            ops_meta = [{"id": o["id"], "name": o["name"], "fields": o["fields"]}
                        for o in api_ops()]
            self._send_json({
                "danger_enabled": DANGER_ENABLED,
                "host": ADMIN_HOST,
                "port": ADMIN_PORT,
                "pigsty_home": PIGSTY_HOME,
                "status": status_meta,
                "ops": ops_meta,
            })
            return

        if path == "/api/log":
            self._send_json({"log": _op_log})
            return

        if path.startswith("/api/status/"):
            sid = path[len("/api/status/"):]
            for s in api_status():
                if s["id"] == sid:
                    res = s["fn"]()
                    log_op("status", s["name"], res["rc"] == 0,
                           (res.get("error") or "")[:200])
                    self._send_json(res)
                    return
            self._send_json({"rc": 404, "text": "", "error": "未知的状态项"}, 404)
            return

        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # ---- 命令操作：直接执行受限命令 ----
        if path == "/api/shell":
            if not DANGER_ENABLED:
                self._send_json({"rc": 403, "text": "", "error":
                    "命令操作已禁用。请以 ADMIN_DANGER=1 启动服务后重试。"}, 403)
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                data = {}
            command = (data.get("command") or "").strip()
            if not command:
                self._send_json({"rc": 400, "text": "", "error": "命令为空"}, 400)
                return
            ok, why = shell_allowed(command)
            if not ok:
                self._send_json({"rc": 403, "text": "", "error": why}, 403)
                return
            try:
                argv = shlex.split(command)
            except ValueError as e:
                self._send_json({"rc": 400, "text": "", "error": "命令解析失败: " + str(e)}, 400)
                return
            rc, out, err = run(argv, timeout=1800)
            log_op("shell", command, rc == 0, (err or "")[:200])
            self._send_json({"rc": rc, "text": out, "error": err})
            return

        if not path.startswith("/api/op/"):
            self.send_error(404)
            return

        if not DANGER_ENABLED:
            self._send_json({"rc": 403, "text": "", "error":
                "危险操作已禁用。请以 ADMIN_DANGER=1 启动服务后重试。"}, 403)
            return

        oid = path[len("/api/op/"):]
        op = next((o for o in api_ops() if o["id"] == oid), None)
        if op is None:
            self._send_json({"rc": 404, "text": "", "error": "未知的操作"}, 404)
            return

        # 读取 body（JSON 或 form）
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        params = {}
        if raw:
            try:
                params = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                from urllib.parse import parse_qs
                params = {k: v[0] for k, v in parse_qs(raw.decode("utf-8")).items()}

        # 必填校验
        for f in op["fields"]:
            if f.get("required") and not (params.get(f["key"]) or "").strip():
                self._send_json({"rc": 400, "text": "",
                                 "error": "缺少必填项: {}".format(f["label"])}, 400)
                return

        rc, out, err = op["fn"](params)
        log_op("op", op["name"] + " " + json.dumps(params, ensure_ascii=False),
               rc == 0, (err or "")[:200])
        self._send_json({"rc": rc, "text": out, "error": err})


def main():
    httpd = ThreadingHTTPServer((ADMIN_HOST, ADMIN_PORT), Handler)
    banner = (
        "\n"
        "  Pigsty Admin WebUI 已启动\n"
        "  ──────────────────────────────────────────\n"
        "  监听地址 : http://{}:{}\n"
        "  危险操作 : {}\n"
        "  Pigsty目录: {}\n"
        "  按 Ctrl+C 停止\n"
        "  ──────────────────────────────────────────\n"
    ).format(ADMIN_HOST, ADMIN_PORT,
             "已启用 (ADMIN_DANGER=1)" if DANGER_ENABLED else "已禁用（只读模式）",
             PIGSTY_HOME)
    print(banner)

    def _stop(signum, frame):
        print("\n正在关闭 Pigsty Admin WebUI ...")
        httpd.shutdown()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
