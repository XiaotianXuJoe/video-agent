# 生产部署说明

> 本文档中的服务器地址、路径均为占位符；真实值只保存在部署者的私有运维记录中，绝不提交到本仓库。

## 拓扑

- 单台 Linux VPS，PM2 进程管理，进程名 `video-agent`
- 服务端口：**3300**（纯 HTTP 内网/IP 直连，无域名无 HTTPS）
- 服务器目录：`~/video-agent/`（`backend/` + `frontend/dist/`）
- 单进程架构：FastAPI 同时提供 API、SSE 和前端静态文件，无 nginx 反代

## 鉴权（必读）

v1.5.3 起应用内置**单用户 token 鉴权**，不再是裸接口：

- 保护范围：全部 `/api/*`，唯一例外是 `POST /api/auth/login`。
- 公开范围：前端静态文件、`/assets`、SPA 兜底路由（登录页必须能加载）。
- 产物文件（视频/图片/音频）都经 `/api/projects/{pid}/file`、`/api/projects/{pid}/frame` 下发，已包含在保护范围内；没有额外的 `projects/` 静态挂载。

### token 从哪来

按优先级两种来源：

1. 环境变量 `VA_AUTH_TOKEN`（推荐用于首次部署，或不想把密码写进 `settings.json`）：在 PM2 的 `ecosystem.config.js` 里设置后重启。
2. 设置页 →「访问密码」，保存到 `backend/data/settings.json` 的 `auth.token`。

两者都为空时（`auth_unconfigured`），除登录接口外所有 API 返回 `401 {"error":{"kind":"auth_unconfigured"}}`，设置页本身也进不去——**此时只能先用 `VA_AUTH_TOKEN` 引导，或直接编辑 `settings.json` 的 `auth.token` 后重启**。

### 登录方式

```bash
# 1) 登录：成功返回 HttpOnly cookie（va_token）与 {"ok": true}
curl -s -i -X POST http://<host>:3300/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"<你的访问密码>"}'

# 2) 脚本/CI 带 token 调 API
TOKEN='<你的访问密码>'
curl -s http://<host>:3300/api/settings -H "Authorization: Bearer $TOKEN"

# 3) 用 cookie 下载产物（浏览器同源会自动携带 cookie）
curl -s -c /tmp/va_jar.txt -X POST http://<host>:3300/api/auth/login \
  -H 'Content-Type: application/json' -d '{"password":"<你的访问密码>"}'
curl -s -b /tmp/va_jar.txt \
  "http://<host>:3300/api/projects/<pid>/file?path=timeline.json" -o timeline.json

# 4) 流式对话（SSE）：header 或 ?token= 均可
curl -N -X POST http://<host>:3300/api/chat/stream \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"session_id":"<sid>","message":"你好"}'
```

浏览器端：登录后密码保存在 localStorage（`va_token`），所有 fetch 自动带 `Authorization: Bearer`；`<img>`、`<video>`、EventSource 这类无法设置请求头的地方依赖登录时下发的同源 HttpOnly cookie。

### 修改密码 / 登出 / 限频

- 设置页修改「访问密码」保存后立即登出并回到登录页（后端 cookie + 本地 token 同时清除），需重新登录。
- `POST /api/auth/logout` 清除 cookie。
- 登录失败限频：每 IP 每分钟 10 次，超出返回 `429`。

### 上线自检

```bash
# 外网应返回 401（auth_required / auth_unconfigured），绝不能是 200
curl -i http://<host>:3300/api/settings
```

纯 HTTP 环境下 cookie 不带 `Secure` 标记。仍强烈建议在反代上启用 HTTPS，否则密码与产物依旧明文过网。

## 部署步骤

```bash
# 1. 本地构建前端
cd frontend && npm run build

# 2. 同步到服务器（示例，地址以私有运维记录为准）
rsync -avz --delete --exclude data --exclude projects --exclude venv \
  backend/ user@<SERVER>:~/video-agent/backend/
rsync -avz --delete frontend/dist/ user@<SERVER>:~/video-agent/frontend/dist/

# 3. 重启
ssh user@<SERVER> 'pm2 restart video-agent'
```

## 不得覆盖的文件

| 路径 | 说明 |
| --- | --- |
| `~/video-agent/backend/data/` | SQLite 数据库 + settings.json（真实 API key、音色 ID） |
| `~/video-agent/backend/projects/` | 全部项目运行数据（讲稿、关键帧、配音、成片） |
| `~/video-agent/backend/venv/` | 服务器上的 Python 虚拟环境 |
| PM2 `ecosystem.config.js` | 服务器真实配置，可能含明文环境变量 |

## 升级注意

- 部署前先在服务器备份将被覆盖的文件（`cp 文件 文件.bak$(date +%s)`）。
- `backend/app/store.py` 的 `DEFAULT_SETTINGS` 新增字段后，旧 settings.json 会在 `load_settings()` 中自动补默认值，无需迁移。
- 本次新增两个配置块：`auth.token`（访问密码，见上文「鉴权」）与 `security.allow_generated_code_exec`（默认 `true`）。在设置页关闭后者后，阶段 6/7/10 会直接失败——这是有意行为，用于临时禁用以模型生成代码执行。
- 部署后旧客户端/脚本若未带 token 会收到 401，请按上文「登录方式」补请求头。
- 进程重启时 `main.py` 会把停留在 `running` 状态的项目标记为 `failed`（可重试），这是有意行为，不要删除。
- HTTP 环境下 `navigator.clipboard` 不可用，前端复制功能必须保留 `execCommand` 降级路径。
