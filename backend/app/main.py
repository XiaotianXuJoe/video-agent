"""FastAPI 入口：API 路由 + SSE + 静态托管前端产物。"""
import json
import re
import time
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import annotations, artifacts, auth, store
from .agent import chat as chat_agent
from .pipeline import machine

BASE = Path(__file__).resolve().parent.parent
DIST = BASE.parent / "frontend" / "dist"

app = FastAPI(title="讲解视频 Agent")
# 全站鉴权：/api/* 全部要求 token（/api/auth/login 除外）。静态前端公开，保证登录页能加载。
app.add_middleware(auth.AuthMiddleware)
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


# ---------- 鉴权（单用户 token） ----------

class LoginIn(BaseModel):
    password: str


@app.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request):
    """密码即 settings.auth.token。成功时下发 HttpOnly cookie（供 img/video/EventSource）。"""
    expected = auth.configured_token()
    if not expected:
        return JSONResponse({"error": {"kind": "auth_unconfigured",
                                       "detail": "服务端尚未配置访问密码（auth.token）"}},
                            status_code=401)
    ip = request.client.host if request.client else "unknown"
    if auth.login_rate_limited(ip):
        return JSONResponse({"error": {"kind": "rate_limited",
                                       "detail": "尝试过于频繁，请一分钟后再试"}},
                            status_code=429)
    if not auth.token_matches((body.password or "").strip(), expected):
        return JSONResponse({"error": {"kind": "auth_failed", "detail": "密码错误"}},
                            status_code=401)
    resp = JSONResponse({"ok": True})
    # 纯 HTTP 环境：不加 Secure（加了浏览器会拒收 cookie）；SameSite=Strict 防 CSRF
    resp.set_cookie(auth.COOKIE_NAME, expected, max_age=auth.COOKIE_MAX_AGE,
                    httponly=True, samesite="strict", path="/")
    return resp


@app.post("/api/auth/logout")
def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


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
    # voice key 同理：省略号 / 星号掩码 / 留空都保留旧 key
    # （掩码是 ******** 形态，漏判会把星号当真 key 覆盖落盘 → 线上 TTS 全部 401）
    v_old = current.get("voice", {})
    v_new = incoming.get("voice", {})
    vk = v_new.get("minimax_api_key", "")
    if "…" in vk or set(vk) == {"*"} or vk == "":
        v_new["minimax_api_key"] = v_old.get("minimax_api_key", "")
    incoming["voice"] = {**v_old, **v_new}
    # 飞书凭据同理：脱敏回传或留空时保留旧值
    f_old = current.get("feishu", {})
    f_new = incoming.get("feishu", {})
    if set(f_new.get("app_secret", "")) == {"*"} or f_new.get("app_secret", "") == "":
        f_new["app_secret"] = f_old.get("app_secret", "")
    incoming["feishu"] = {**f_old, **f_new}
    incoming["stage_models"] = {**current["stage_models"], **incoming.get("stage_models", {})}
    # 访问密码同理：脱敏回传（********）或留空时保留旧值（也意味着不能在此清空密码）
    a_old = current.get("auth", {})
    a_new = dict(incoming.get("auth") or {})
    a_new.pop("has_token", None)
    tok = (a_new.get("token") or "").strip()
    if set(tok) == {"*"} or tok == "":
        a_new["token"] = a_old.get("token", "")
    incoming["auth"] = {**a_old, **a_new}
    incoming["security"] = {**current.get("security", {}), **(incoming.get("security") or {})}
    # 创作规范 / 流水线参数 / BGM：逐键合并，未携带的沿用旧值
    incoming["content_rules"] = {**current.get("content_rules", {}),
                                 **(incoming.get("content_rules") or {})}
    incoming["pipeline"] = {**current.get("pipeline", {}), **(incoming.get("pipeline") or {})}
    incoming["bgm"] = {**current.get("bgm", {}), **(incoming.get("bgm") or {})}
    store.save_settings(incoming)
    return store.masked_settings()


BGM_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
BGM_MAX_BYTES = 20 * 1024 * 1024


@app.post("/api/settings/bgm")
async def upload_bgm(file: UploadFile = File(...)):
    """自定义 BGM 上传：只收音频后缀、限 20MB、文件名净化后存入 assets 目录。

    只存文件，不自动改设置——前端上传成功后再把设置页里的「BGM 文件」指向它，
    用户点保存才生效，避免一次误选就替换掉正在用的配乐。
    """
    original = Path(file.filename or "")
    suffix = original.suffix.lower()
    if suffix not in BGM_SUFFIXES:
        raise HTTPException(status_code=400, detail={
            "kind": "bad_file",
            "detail": f"不支持的音频格式 {suffix or '(无后缀)'}，请用 "
                      f"{' / '.join(sorted(BGM_SUFFIXES))}"})
    stem = re.sub(r"[^A-Za-z0-9_-]", "_", original.stem)[:40] or "bgm"
    name = f"bgm_{int(time.time())}_{stem}{suffix}"
    target = store.ASSETS_DIR / name
    total = 0
    try:
        with open(target, "wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > BGM_MAX_BYTES:
                    raise HTTPException(status_code=413, detail={
                        "kind": "too_large",
                        "detail": f"BGM 文件不能超过 {BGM_MAX_BYTES // (1024 * 1024)}MB"})
                fh.write(chunk)
    except HTTPException:
        target.unlink(missing_ok=True)
        raise
    if total == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail={"kind": "empty_file", "detail": "上传文件为空"})
    return {"path": name, "bytes": total}


class ProviderTestIn(BaseModel):
    id: str


@app.post("/api/settings/test_provider")
async def test_provider(body: ProviderTestIn):
    """连通性测试只接受「已保存供应商的 id」。
    请求体里的 base_url / api_key 一律忽略——否则已存 key 会被发到攻击者域名（C-SEC-02）。"""
    from .llm import test_provider as _test
    p = store.get_provider(body.id)
    if not p:
        raise HTTPException(404, "供应商尚未保存：请先保存设置，再测试连接")
    return await _test(dict(p))


@app.post("/api/settings/test_minimax")
async def test_minimax():
    """MiniMax 连通性测试：只用已保存配置（不接受请求体的 base_url / key）。"""
    import httpx as _hx
    import time as _t
    v = store.load_settings()["voice"]
    key = v.get("minimax_api_key", "")
    base = (v.get("minimax_base_url") or "").rstrip("/")
    if not key:
        return {"ok": False, "error": {"kind": "auth",
                                       "detail": "未填写 MiniMax API Key（请先在设置页填写并保存）"}}
    # 用真实配音管线同一个端点 /t2a_v2 做最小化合成测试（1 个字符），
    # 与正式 TTS 行为一致——chat/completions 不支持 speech 模型，不能当探测点。
    model = v.get("model") or "minimax-speech-2.8-hd"
    voice_id = v.get("voice_id") or ""
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


@app.get("/api/projects/{pid}/events")
def project_events(pid: str, after: int = Query(0), limit: int = Query(500)):
    """流水线事件回放（D1）：断线/刷新后按 seq 游标增量拉取，不再靠轮询猜进度。"""
    if store.get_project(pid) is None:
        raise HTTPException(404)
    return {"events": store.list_project_events(pid, after, min(limit, 2000)),
            "latest": store.latest_event_seq(pid)}


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
    # 音色 ID 等敏感 extra 只回显后 4 位（C-SEC-09）
    return artifacts.list_artifacts_masked(store.project_dir(pid))


@app.get("/api/projects/{pid}/raw_responses")
def list_raw_responses(pid: str):
    """列出项目 raw_responses/ 下的模型原始响应（最新在前），供失败详情「查看最近原始响应」使用（C-UX-07）。
    只回文件元信息；内容仍走 /file 端点，那里有路径校验与 no-cache。"""
    proj_dir = store.project_dir(pid)
    d = proj_dir / "raw_responses"
    if not d.is_dir():
        return {"latest": None, "files": []}
    files = [p for p in d.iterdir() if p.is_file()]
    try:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        pass
    out = [{"name": p.name, "size": p.stat().st_size, "mtime": p.stat().st_mtime}
           for p in files[:50]]
    return {"latest": out[0] if out else None, "files": out}


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
        # 被 SSRF 防护拒绝的地址属于「请求非法」，返回 400 而不是上游错误
        raise HTTPException(400 if e.kind == "config" else 502,
                            {"kind": e.kind, "detail": e.detail})
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
        try:
            inside = target.relative_to(DIST.resolve()) is not None
        except ValueError:
            inside = False
        if full_path and inside and target.is_file():
            return FileResponse(target)
        return FileResponse(DIST / "index.html",
                            headers={"Cache-Control": "no-cache"})
