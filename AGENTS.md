# AGENTS.md — 维护约束

## 项目一句话

FastAPI + React 的对话式讲解视频生产 Agent；后端单进程，前端构建产物由后端静态托管。

## 测试命令

- `cd backend && python3 test_modify_flow.py` —— 修改流（17 断言，必须全过）
- `cd backend && python3 test_chat_persistence.py` —— 落盘可见性（8 断言，必须全过）
- `cd backend && python3 test_security.py` —— 安全回归（鉴权/路径穿越/密钥外发/执行加固/SSRF，必须全过）
- `cd frontend && npm run build` —— 含 `tsc -b`，类型错误必须为零
- 其余 `backend/test_*.py` 为专项回归，改动对应模块时必跑

## 硬性约束

1. **绝不提交** `backend/data/`、`backend/projects/`、任何 `.env`、真实 API key、音色 ID、服务器地址。设置页所有 key 默认值必须为空字符串。
2. 不修改、不重启线上服务来完成代码任务；部署是独立动作。
3. 后端修改对话/流水线行为（chat.py / machine.py）后，必须同步更新对应测试的断言。
4. 前端改动后必须 `npm run build` 通过，并确认 SSE 事件契约（token / stage_event / artifact / gate / error / done）未破坏。
5. 文件下载/图片端点必须保留 `Cache-Control: no-cache`，否则浏览器会缓存同名关键帧 PNG。
6. 配音 key 只从环境变量或设置页读取，绝不写进项目目录或代码。
7. 给用户写 Python 代码时避免 for-else 语法，改用循环后显式条件判断。

## 结构速查

- 对话编排：`backend/app/agent/chat.py`
- 流水线状态机：`backend/app/pipeline/machine.py`（11 阶段，门禁 = 3/5/7/11）
- 自愈逻辑：`visuals_with_autofix`（修复失败时恢复修改前代码，最多 2 轮）
- 落盘约定：动作、阶段完成、终态（gate/done/error/interrupted）均 `store.add_message` 落盘
- 生产部署：见 `docs/PRODUCTION.md`
