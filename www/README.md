# Pigsty 首页静态文件（/www）

本目录是 VPS 上 `/www` 目录的同步副本，由 nginx（`/etc/nginx/conf.d/home.conf`）作为站点根
提供，对应 `http://<vps-ip>/` 的 Pigsty 主页。

## 文件说明

| 文件 | 说明 |
|------|------|
| `index.html` | 英文首页（导航栏含「运维管理」入口，指向 `/admin/`） |
| `zh.html` | 中文首页 |
| `404.html` | 404 页面 |
| `pev.html` | Postgres Explain Visualizer |
| `ca.crt` | 自签 CA 证书（供客户端信任） |
| `acme/ logs/ pigsty/ report/ repos/ schema/` | 运行时目录占位（由其他流程生成，`.gitkeep` 仅为保留目录结构） |

> ⚠️ `index.html` / `zh.html` 中硬编码了当前 VPS 的公网 IP（如 `217.69.2.217`），
> 部署到其他机器时需替换为对应 IP，或改为域名。

## 部署（同步回 /www）

```bash
# 复制静态文件到运行目录
cp www/index.html www/zh.html www/404.html www/pev.html www/ca.crt /www/
# 如需同步空目录占位（通常不需要，运行时自动生成）
# cp -r www/acme www/logs www/pigsty www/report www/repos www/schema /www/
nginx -t && systemctl reload nginx
```

> 首页的「运维管理」入口依赖 `admin/` 控制台（见 `../admin/README.md`）与 nginx 的
> `/admin/` 反代配置。
