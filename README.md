# 讲解视频 Agent（video-agent）

把算法题解 / 科普主题自动做成**讲解视频**的对话式 Agent：给它题目和正确代码（或一个主题、一个飞书文档链接），它按流水线完成创意、分镜、视觉动画、配音、渲染合成与 QC 验收，每个关键阶段都可以人工确认或直接输入修改意见。

## 功能

- **10 阶段可执行流水线**（+ 对话式「需求澄清 / 修改」两态）：算法校验 → 创意方案（门禁）→ 讲稿分镜 → 事实校对（门禁）→ 视觉代码 → 关键帧预览（门禁）→ 配音合成 → 时间轴组装 → 渲染合成 → QC 验收（门禁）；资料摄取（网页 / 飞书）为独立能力，可随时把资料注入创意与分镜
- **对话驱动**：SSE 流式对话；流水线每个阶段的进度、完成、失败都会落盘，刷新/重进会话可见
- **人工门禁**：概念、讲稿、关键帧、成片四道确认关卡；在门禁处直接输入修改意见即进入修改流（如「第 3 镜的减号显示为方框」）
- **视觉自愈**：视觉代码由视觉模型生成（Pillow 逐帧动画），编译 + 全场景冒烟自检失败后自动修复，最多 2 轮
- **配音**：MiniMax TTS（支持个人克隆音色），自然语速时间轴对齐
- **QC 验收**：逐帧比对、响度（LUFS）、真峰值、静音段检测
- **产物面板**：关键帧画廊（逐帧「修改这帧」）、视频播放器（帧标注 / 片段标记回传对话）、流水线面板常开

## 技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | FastAPI 单进程（REST + SSE + 静态托管前端），Python 3.11+ |
| 前端 | React 18 + TypeScript + Vite 6 + Tailwind CSS v4 |
| 渲染 | Pillow 逐帧 → FFmpeg 合成 |
| 配音 | MiniMax TTS API |
| 存储 | SQLite（会话/项目/产物索引）+ 文件系统（每个项目一个目录） |

## 目录结构

```
backend/
  app/
    main.py            # FastAPI 入口：API 路由 + SSE + 静态托管
    store.py           # SQLite 存储 + 设置（key 脱敏返回）
    agent/chat.py      # 对话编排：handle_chat / handle_action / run_modify
    pipeline/          # 流水线状态机与提示词
    tools/             # keyframe_render / minimax_tts / render_video / qc_video
    ingest/            # 飞书文档 / 网页摄取
  test_*.py            # 本地 mock 验收测试（不依赖外部服务）
frontend/
  src/                 # React 应用（聊天视图 / 产物面板 / 视频标注 / 设置）
docs/PRODUCTION.md     # 生产部署说明
```

## 本地运行

```bash
# 后端
cd backend
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 3300

# 前端（开发模式，代理到 3300）
cd frontend
npm install
npm run dev

# 前端（生产构建，构建后由后端直接托管）
npm run build
```

打开 `http://localhost:3300`，在「设置」中填入模型 Provider（OpenAI 兼容接口）与 MiniMax 配音 key 即可使用。配置项见 `backend/.env.example`；界面内设置优先于环境变量。

## 测试

```bash
cd backend
python3 test_modify_flow.py        # 修改流 17 断言（成功/报错/修复耗尽三路径）
python3 test_chat_persistence.py   # 落盘可见性 8 断言
python3 test_llm_anthropic.py      # LLM 客户端（需 mock server，脚本自带）
python3 test_stage6_autofix.py     # 视觉自愈
python3 test_cc_repro.py           # cc 适配回归
```

前端：`cd frontend && npm run build`（含 `tsc -b` 类型检查）。

## 许可

个人项目，未指定开源许可；如要复用请先联系作者。
