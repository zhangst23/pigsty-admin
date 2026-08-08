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
    "redis-add",
]

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
        {"id": "clusters", "name": "集群状态 (pig pg list)", "fn": status_clusters},
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
