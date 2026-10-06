# 安全部署要点

单用户部署的最低安全基线（与代码默认值一致）。

## 凭据

- `API_KEY` 必填且不能为空；`APP_ENV=prod` 时拒绝少于 32 字符或以 `change-me` 开头的 key，dev 只打一次警告。生成：
  `python -c "import secrets;print(secrets.token_urlsafe(32))"`
- `WEB_SESSION_SECRET`（可选）：浏览器会话签名密钥，未设置时由 `API_KEY` 派生。修改它会让所有浏览器会话失效。

## 浏览器 UI（/app）

- 会话 cookie：`HttpOnly`、`SameSite=Strict`；经 HTTPS 提供时必须设 `WEB_COOKIE_SECURE=true`。
- 退出登录仅支持 POST，并在 `DATA_DIR/web_session_not_before` 写入时间戳，之前签发的所有会话立即失效（单用户 = 退出所有设备）。删除该文件即撤销此限制。
- CSRF：cookie 认证的写请求需同源 `Origin`/`Referer`，或（两者都缺失时）`X-Requested-With: fetch`；`Origin: null` 视为跨域。反向代理改写 Host 时设置 `WEB_PUBLIC_ORIGIN=https://你的域名`。`X-API-Key` 请求不受影响。
- `/app` 响应带 CSP（`default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'`），模板不得使用内联脚本/样式。
- 登录与 `X-API-Key` 失败按客户端 IP 限流：5 分钟内 5 次失败后返回 429 + `Retry-After`。计数在进程内存中，重启清零；反向代理后需让 uvicorn 获得真实客户端 IP（`--proxy-headers` + `--forwarded-allow-ips`），否则所有请求共享代理 IP。

## API 文档

`APP_ENV=prod` 时关闭 `/docs`、`/redoc`、`/openapi.json`。

## Docker

- 镜像以非 root 用户（uid/gid 10001，可用 `--build-arg APP_UID/APP_GID` 修改）运行，仅 `/app/data` 可写；Linux 上宿主机 `./data` 需对该 uid 可写。
- 代码（`src/`、`alembic/`）打包在镜像内，不再挂载；`config/`、`strategies/`、`prompts/` 以只读方式挂载。改代码后需 `docker compose build`。
- 端口只发布到 `127.0.0.1:8000`；远程访问请在前面加 TLS 反向代理，并设置 `WEB_COOKIE_SECURE=true`、`WEB_PUBLIC_ORIGIN`。
