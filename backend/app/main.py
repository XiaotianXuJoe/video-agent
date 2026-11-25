"""FastAPI 入口：API 路由 + SSE + 静态托管前端产物。"""
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import annotations, artifacts, store
from .agent import chat as chat_agent
from .pipeline import machine

BASE = Path(__file__).resolve().parent.parent
DIST = BASE.parent / "frontend" / "dist"

app = FastAPI(title="讲解视频 Agent")
store.init_db()

# 启动收割：进程重启后，状态仍停留在 running 的项目永远等不到事件——标记为失败以便重试
for _p in store.list_projects():
    _dir = store.project_dir(_p["id"])
    _st = machine.load_state(_dir)
    if _st.get("status") == "running":
        _st["status"] = "failed"
        _st["error"] = "服务重启导致流水线中断，可用「重试」从当前阶段继续"
        machine.save_state(_dir, _st)
        store.update_project(_p["id"], state="failed")


# ---------- 设置 ----------

@app.get("/api/settings")
def get_settings():
    return store.masked_settings()


@app.put("/api/settings")
def put_settings(payload: dict):
    current = store.load_settings()
    incoming = payload
    # 部分更新保护：payload 未携带的区块沿用旧值，避免覆盖式丢失
    if "providers" not in incoming:
        incoming["providers"] = current["providers"]
    # 供应商 key 脱敏保护：含省略号或留空时保留旧 key
    old_by_id = {p["id"]: p for p in current["providers"]}
    for p in incoming.get("providers", []):
        old = old_by_id.get(p.get("id"))
        k = p.get("api_key", "")
        if old and ("…" in k or set(k) == {"*"} or (k == "" and old.get("api_key"))):
            p["api_key"] = old["api_key"]
    # voice key 同理
    v_old = current.get("voice", {})
    v_new = incoming.get("voice", {})
    if "…" in v_new.get("minimax_api_key", "") or v_new.get("minimax_api_key", "") == "":
        v_new["minimax_api_key"] = v_old.get("minimax_api_key", "")
    incoming["voice"] = {**v_old, **v_new}
    # 飞书凭据同理：脱敏回传或留空时保留旧值
    f_old = current.get("feishu", {})
    f_new = incoming.get("feishu", {})
    if set(f_new.get("app_secret", "")) == {"*"} or f_new.get("app_secret", "") == "":
        f_new["app_secret"] = f_old.get("app_secret", "")
    incoming["feishu"] = {**f_old, **f_new}
    incoming["stage_models"] = {**current["stage_models"], **incoming.get("stage_models", {})}
    store.save_settings(incoming)
    return store.masked_settings()


class ProviderTestIn(BaseModel):
    provider: dict  # 前端表单中的未保存值（type/base_url/model/api_key）


@app.post("/api/settings/test_provider")
async def test_provider(body: ProviderTestIn):
    from .llm import test_provider as _test
    p = dict(body.provider)
    # 表单里的 key 若是脱敏值，回取已保存的真实 key 再测
    k = p.get("api_key", "")
    if "…" in k or set(k) == {"*"} or not k:
        old = store.get_provider(p.get("id", ""))
        if old:
            p["api_key"] = old["api_key"]
    return await _test(p)


@app.post("/api/settings/test_minimax")
async def test_minimax(body: dict):
    """MiniMax 连通性测试：表单未保存的 key 脱敏时回取已保存值。"""
    import httpx as _hx
    import time as _t
    v = store.load_settings()["voice"]
    key = body.get("api_key", "")
    if "…" in key or set(key) == {"*"} or not key:
        key = v.get("minimax_api_key", "")
    base = (body.get("base_url") or v.get("minimax_base_url") or "").rstrip("/")
    if not key:
        return {"ok": False, "error": {"kind": "auth", "detail": "未填写 MiniMax API Key"}}
    # 用真实配音管线同一个端点 /t2a_v2 做最小化合成测试（1 个字符），
    # 与正式 TTS 行为一致——chat/completions 不支持 speech 模型，不能当探测点。
    model = body.get("model") or v.get("model") or "minimax-speech-2.8-hd"
    voice_id = body.get("voice_id") or v.get("voice_id") or ""
    t0 = _t.time()
    try:
        async with _hx.AsyncClient(timeout=_hx.Timeout(30, connect=10)) as client:
            resp = await client.post(
                f"{base}/t2a_v2",
                json={"model": model, "text": "测", "stream": False,
                      "voice_setting": {"voice_id": voice_id, "speed": 1.0},
                      "audio_setting": {"format": "mp3", "sample_rate": 32000}},
                headers={"Authorization": f"Bearer {key}"})
        lat = int((_t.time() - t0) * 1000)
        if resp.status_code in (401, 403):
            return {"ok": False, "latency_ms": lat,
                    "error": {"kind": "auth", "detail": f"HTTP {resp.status_code} 鉴权失败"}}
        if resp.status_code != 200:
            return {"ok": False, "latency_ms": lat,
                    "error": {"kind": "http", "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"}}
        try:
            data = resp.json()
        except ValueError:
            return {"ok": False, "latency_ms": lat,
                    "error": {"kind": "http", "detail": f"响应非 JSON: {resp.text[:150]}"}}
        base_resp = data.get("base_resp") or {}
        if base_resp.get("status_code", 0) != 0:
            msg = base_resp.get("status_msg", "")
            code = base_resp.get("status_code")
            kind = "auth" if code in (1004, 2049) else "http"
            return {"ok": False, "latency_ms": lat,
                    "error": {"kind": kind, "detail": f"base_resp {code}: {msg}"[:200]}}
        audio_hex = (data.get("data") or {}).get("audio", "")
        if not audio_hex:
            return {"ok": False, "latency_ms": lat,
                    "error": {"kind": "http", "detail": "HTTP 200 但无音频数据"}}
        return {"ok": True, "latency_ms": lat,
                "model_echo": f"TTS 合成成功（{len(audio_hex) // 2} 字节音频，音色 {voice_id}）"}
    except _hx.ConnectError as e:
        return {"ok": False, "error": {"kind": "network", "detail": str(e)[:200]}}
    except _hx.ReadTimeout as e:
        return {"ok": False, "error": {"kind": "timeout", "detail": str(e)[:200]}}


# ---------- 会话 ----------

@app.get("/api/sessions")
def list_sessions():
    return store.list_sessions()


@app.post("/api/sessions")
def create_session():
    sid = store.create_session()
    return store.get_session(sid)


@app.get("/api/sessions/{sid}/messages")
def get_messages(sid: str):
    return store.session_messages(sid)


class SessionPatchIn(BaseModel):
    title: str


@app.patch("/api/sessions/{sid}")
def patch_session(sid: str, body: SessionPatchIn):
    if not store.rename_session(sid, body.title):
        raise HTTPException(400, "标题为空或会话不存在")
    return store.get_session(sid)


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str):
    with store._conn() as c:
        c.execute("DELETE FROM sessions WHERE id=?", (sid,))
        c.execute("DELETE FROM messages WHERE session_id=?", (sid,))
    return {"ok": True}


# ---------- 对话（SSE） ----------

class ChatIn(BaseModel):
    session_id: str
    message: str
    attachments: list | None = None


@app.post("/api/chat/stream")
async def chat_stream(body: ChatIn):
    async def gen():
        try:
            async for chunk in chat_agent.handle_chat(
                    body.session_id, body.message, body.attachments):
                yield chunk
        except Exception as e:
            yield chat_agent._sse("error", {"message": f"{type(e).__name__}: {e}"})
            yield chat_agent._sse("done", {})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------- 项目 / 产物 ----------

@app.get("/api/projects")
def list_projects():
    return store.list_projects()


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    p = store.get_project(pid)
    if not p:
        raise HTTPException(404)
    # 读取时收割：状态停在 running 但进程内无存活任务 → 如实标记 failed（可重试）
    pstate = machine.reap_if_stale(pid)
    from .agent.suggestions import chips_for
    return {"project": p, "pipeline": pstate, "chips": chips_for(p, pstate),
            "stage_names": machine.STAGE_NAMES, "gates": machine.GATES}


@app.post("/api/projects/{pid}/run_stage")
async def run_stage(pid: str, body: dict):
    """手动单点执行：重跑指定阶段后停下（门禁阶段进入待确认，其余进入 paused）。"""
    p = store.get_project(pid)
    if not p:
        raise HTTPException(404)
    if machine.is_running(pid):
        raise HTTPException(409, "流水线正在运行中，请先中断")
    try:
        stage = int(body.get("stage", 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "stage 必须是数字")
    if not (2 <= stage <= 11):
        raise HTTPException(400, "stage 必须在 2-11 之间")
    pl = machine.Pipeline(pid)
    machine.start_run(pid, pl.run_one_stage(stage))
    return {"ok": True, "stage": stage, "name": machine.STAGE_NAMES[stage]}


@app.post("/api/projects/{pid}/stop")
async def stop_project(pid: str):
    """中断正在运行的流水线；若无存活任务但状态停在 running，则同步收割。"""
    p = store.get_project(pid)
    if not p:
        raise HTTPException(404)
    if machine.stop(pid):
        return {"ok": True, "stopped": True}
    st = machine.reap_if_stale(pid)
    if st.get("status") == "failed":
        return {"ok": True, "stopped": False, "reaped": True}
    return {"ok": False, "error": "没有正在运行的流水线"}


@app.get("/api/projects/{pid}/artifacts")
def list_artifacts(pid: str):
    return artifacts.list_artifacts(store.project_dir(pid))


@app.get("/api/projects/{pid}/file")
def get_file(pid: str, path: str = Query(...)):
    proj_dir = store.project_dir(pid)
    try:
        p = artifacts.resolve_path(proj_dir, path)
    except ValueError:
        raise HTTPException(403)
    if not p.exists() or not p.is_file():
        raise HTTPException(404)
    # no-cache：产物会被重跑覆盖（同名文件，如关键帧 PNG），必须让浏览器每次回源校验
    return FileResponse(p, headers={"Cache-Control": "no-cache"})


# ---------- 资料摄取（M6） ----------

class IngestIn(BaseModel):
    url: str


@app.post("/api/ingest")
async def ingest(body: IngestIn):
    from .ingest import IngestError
    from .ingest_service import ingest_url
    from .llm import ProviderError

    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "仅支持 http/https 链接")
    try:
        return await ingest_url(url)
    except IngestError as e:
        raise HTTPException(502, {"kind": e.kind, "detail": e.detail})
    except ProviderError as e:
        raise HTTPException(502, {"kind": e.kind, "detail": e.detail})
    except ValueError as e:
        raise HTTPException(502, {"kind": "parse", "detail": f"摘要 JSON 解析失败: {e}"})


@app.get("/api/references")
def list_references():
    return store.list_references()


@app.delete("/api/references/{rid}")
def delete_reference(rid: str):
    with store._conn() as c:
        c.execute("DELETE FROM references_ WHERE id=?", (rid,))
        c.execute("DELETE FROM project_refs WHERE ref_id=?", (rid,))
    return {"ok": True}


@app.get("/api/projects/{pid}/references")
def project_refs(pid: str):
    return store.project_references(pid)


class AttachRefIn(BaseModel):
    ref_id: str


@app.post("/api/projects/{pid}/attach_ref")
def attach_ref(pid: str, body: AttachRefIn):
    if not store.get_project(pid):
        raise HTTPException(404, "项目不存在")
    if not store.get_reference(body.ref_id):
        raise HTTPException(404, "资料不存在")
    store.attach_reference(pid, body.ref_id)
    return {"ok": True}


# ---------- 标注 ----------

@app.get("/api/projects/{pid}/annotations")
def list_ann(pid: str):
    return annotations.list_annotations(pid)


@app.post("/api/projects/{pid}/annotations")
def create_ann(pid: str, data: dict):
    return annotations.save_annotation(pid, data)


@app.get("/api/projects/{pid}/frame")
def get_frame(pid: str, video: str = Query(...), t: float = Query(...)):
    import tempfile
    proj_dir = store.project_dir(pid)
    try:
        vp = artifacts.resolve_path(proj_dir, video)
    except ValueError:
        raise HTTPException(403)
    if not vp.exists():
        raise HTTPException(404)
    out = Path(tempfile.gettempdir()) / f"frame_{pid}_{int(t*1000)}.jpg"
    if not out.exists():
        try:
            annotations.extract_frame(vp, t, out)
        except Exception as e:
            raise HTTPException(500, f"抽帧失败: {e}")
    return FileResponse(out, headers={"Cache-Control": "no-cache"})


# ---------- 健康 ----------

@app.get("/api/health")
def health():
    return {"ok": True, "service": "video-agent"}


# ---------- 静态前端（HTML no-cache，哈希资源长缓存） ----------

if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(404)
        target = (DIST / full_path).resolve()
        if full_path and target.exists() and target.is_file() \
                and str(target).startswith(str(DIST.resolve())):
            return FileResponse(target)
        return FileResponse(DIST / "index.html",
                            headers={"Cache-Control": "no-cache"})
