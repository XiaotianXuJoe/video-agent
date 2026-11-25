# 生产部署说明

> 本文档中的服务器地址、路径均为占位符；真实值只保存在部署者的私有运维记录中，绝不提交到本仓库。

## 拓扑

- 单台 Linux VPS，PM2 进程管理，进程名 `video-agent`
- 服务端口：**3300**（纯 HTTP 内网/IP 直连，无域名无 HTTPS）
- 服务器目录：`~/video-agent/`（`backend/` + `frontend/dist/`）
- 单进程架构：FastAPI 同时提供 API、SSE 和前端静态文件，无 nginx 反代

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
- 进程重启时 `main.py` 会把停留在 `running` 状态的项目标记为 `failed`（可重试），这是有意行为，不要删除。
- HTTP 环境下 `navigator.clipboard` 不可用，前端复制功能必须保留 `execCommand` 降级路径。
