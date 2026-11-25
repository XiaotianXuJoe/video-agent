"""对话编排：意图路由（新建/修改/问答）+ SSE 流式输出 + 动作消息处理。"""
import asyncio
import json
from pathlib import Path

from .. import artifacts, store
from ..llm import stream_chat, complete, ProviderError
from ..pipeline import machine, prompts
from .suggestions import chips_for

ACTION_PREFIX = "__action:"


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chat_provider():
    p = store.provider_for_key("clarify")
    if p and p.get("api_key"):
        return p
    for q in store.load_settings()["providers"]:
        if q.get("api_key"):
            return q
    return None


URL_RE = None


def _find_urls(text: str):
    import re
    global URL_RE
    if URL_RE is None:
        URL_RE = re.compile(r"https?://[^\s<>）)\"']+")
    return [u.rstrip("。，；、") for u in URL_RE.findall(text)]


async def handle_chat(session_id: str, message: str, attachments=None):
    """异步生成器：产出 SSE 文本。attachments 可含标注 payload。"""
    session = store.get_session(session_id)
    if not session:
        yield _sse("error", {"message": "会话不存在"})
        return
    store.add_message(session_id, "user", message,
                      "annotation" if attachments else "text",
                      {"attachments": attachments} if attachments else None)
    if not message.startswith(ACTION_PREFIX):
        auto_title = store.maybe_autoname_session(session_id, message)
        if auto_title:
            yield _sse("session_renamed", {"session_id": session_id, "title": auto_title})

    project = store.get_project(session["project_id"]) if session.get("project_id") else None
    pstate = machine.load_state(store.project_dir(project["id"])) if project else None

    # ---- 动作消息（chips / 门禁按钮）----
    if message.startswith(ACTION_PREFIX):
        act = json.loads(message[len(ACTION_PREFIX):])
        async for chunk in handle_action(session_id, project, pstate, act):
            yield chunk
        return

    # ---- 门禁等待中的自然语言 ----
    if project and pstate.get("status") == "awaiting_gate":
        low = message.strip().lower()
        if any(w in low for w in ("确认", "可以", "继续", "通过", "ok", "好")) and len(low) < 20:
            async for chunk in handle_action(session_id, project, pstate, {"action": "confirm_gate"}):
                yield chunk
            return
        # 否则视为修改意见 → 修改模式
        async for chunk in run_modify(session_id, project, message, attachments):
            yield chunk
        return

    # ---- 已有项目 + 自由文本：修改请求或问答 ----
    if project and pstate.get("status") in ("tier_done", "failed", "created"):
        modify_words = ("改", "换", "重新", "重跑", "不对", "错了", "太长", "遮挡", "发音", "音色")
        if attachments or any(w in message for w in modify_words):
            async for chunk in run_modify(session_id, project, message, attachments):
                yield chunk
            return

    # ---- 对话（澄清 / 问答）----
    provider = _chat_provider()
    if not provider:
        yield _sse("token", {"text": "还没有配置任何模型供应商。请先到右上角「设置」添加供应商（Kimi、Opus 或任意 OpenAI 兼容端点）并填写 API Key。"})
        yield _sse("suggestions", {"chips": [{"label": "打开设置", "action": "open_settings"}]})
        yield _sse("done", {})
        return

    history = [{"role": m["role"], "content": m["content"]}
               for m in store.session_messages(session_id)[-20:]
               if m["kind"] == "text"]
    system = prompts.CLARIFY_SYSTEM if not project else prompts.CHAT_SYSTEM
    full = ""
    try:
        async for delta in stream_chat(provider, history, system=system):
            full += delta
            yield _sse("token", {"text": delta})
    except ProviderError as e:
        yield _sse("token", {"text": f"\n\n⚠️ 模型调用失败（{e.kind}）：{e.detail}"})
    store.add_message(session_id, "assistant", full)

    # 若用户已提供题目且未建项目 → 提示选择档位
    chips = None
    if not project and any(k in message for k in ("题", "算法", "讲解", "视频", "反转", "排序", "约瑟夫", "链表")):
        chips = [
            {"label": "生成创意方案", "action": "start_tier", "tier": "concept"},
            {"label": "直接生成分镜", "action": "start_tier", "tier": "storyboard"},
            {"label": "直接生成视频", "action": "start_tier", "tier": "video"},
        ]
    else:
        chips = chips_for(project, pstate or {})
    # 消息含链接 → 追加资料摄取入口
    for u in _find_urls(message):
        chips = (chips or []) + [{"label": f"摄取并整理这个链接", "action": "ingest_url", "url": u}]
    yield _sse("suggestions", {"chips": chips})
    yield _sse("done", {})


async def handle_action(session_id, project, pstate, act: dict):
    action = act.get("action")

    if action == "ingest_url":
        from ..ingest import IngestError
        from ..ingest_service import ingest_url
        url = act.get("url", "")
        yield _sse("token", {"text": f"📥 正在摄取资料：{url}\n"})
        try:
            ref = await ingest_url(url)
        except IngestError as e:
            yield _sse("token", {"text": f"摄取失败（{e.kind}）：{e.detail}"})
            yield _sse("done", {})
            return
        except ProviderError as e:
            yield _sse("token", {"text": f"摘要生成失败（{e.kind}）：{e.detail}"})
            yield _sse("done", {})
            return
        except Exception as e:
            yield _sse("token", {"text": f"摄取失败：{type(e).__name__}: {e}"})
            yield _sse("done", {})
            return
        if project:
            store.attach_reference(project["id"], ref["id"])
        kp = "\n".join(f"  · {k}" for k in ref["key_points"][:6])
        text = (f"✅ 已收录资料《{ref['title']}》（{ref['ingested_via']}，"
                f"摘要模型：{ref.get('provider','')}）\n\n{ref['summary']}\n\n关键要点：\n{kp}")
        yield _sse("token", {"text": text})
        store.add_message(session_id, "assistant", text)
        yield _sse("reference", {"reference": ref, "attached": bool(project)})
        chips = chips_for(project, pstate or {})
        if project:
            chips = chips + [{"label": "查看项目参考资料", "action": "open_refs"}]
        yield _sse("suggestions", {"chips": chips})
        yield _sse("done", {})
        return

    if action == "start_tier":
        tier = act["tier"]
        provider = store.provider_for_key("routing") or _chat_provider()
        if not provider:
            yield _sse("error", {"message": "未配置模型供应商"})
            return
        # 提取简报
        history = "\n".join(f"{m['role']}: {m['content']}"
                            for m in store.session_messages(session_id)[-30:]
                            if m["kind"] == "text")
        try:
            raw = await complete(provider, [{"role": "user", "content":
                                            prompts.BRIEF_EXTRACT.format(history=history)}])
            brief = machine.extract_json(raw)
        except Exception as e:
            yield _sse("error", {"message": f"简报提取失败：{e}"})
            return
        title = brief.get("title") or "讲解视频"
        pid = store.create_project(title, tier)
        store.bind_project(session_id, pid)
        pdir = store.project_dir(pid)
        machine._write_json(pdir / "brief.json", brief)
        store.update_project(pid, title=title)
        yield _sse("project", {"project": store.get_project(pid), "brief": brief})
        yield _sse("token", {"text": f"已建立项目《{title}》，开始执行「{ {'concept':'创意档','storyboard':'分镜档','video':'成片档'}[tier] }」流水线。"})
        store.add_message(session_id, "assistant", f"已建立项目《{brief.get('title')}》")
        pl = machine.Pipeline(pid)
        machine.start_run(pid, pl.run_tier(tier))
        async for chunk in stream_project_events(pid, session_id):
            yield chunk
        return

    if not project:
        yield _sse("suggestions", {"chips": chips_for(None, {})})
        yield _sse("done", {})
        return

    pid = project["id"]
    pl = machine.Pipeline(pid)

    if action == "confirm_gate":
        sel = act.get("selected_direction")
        yield _sse("token", {"text": "已确认，继续流水线。"})
        store.add_message(session_id, "assistant", "已确认门禁，继续执行流水线。")
        machine.start_run(pid, pl.resume(selected_direction=sel))
        async for chunk in stream_project_events(pid, session_id):
            yield chunk
        return

    if action == "upgrade_tier":
        tier = act["tier"]
        text = f"升级到「{ {'storyboard':'分镜档','video':'成片档','concept':'创意档'}[tier] }」，继续执行。"
        yield _sse("token", {"text": text})
        store.add_message(session_id, "assistant", text)
        machine.start_run(pid, pl.upgrade_tier(tier))
        async for chunk in stream_project_events(pid, session_id):
            yield chunk
        return

    if action == "redo_stage":
        stage = act["stage"]
        pstate["stage"] = stage - 1
        pstate["status"] = "running"
        pstate["gate"] = None
        machine.save_state(pl.dir, pstate)
        tier = pstate.get("tier") or "video"
        _, end = machine.TIER_STAGES[tier]
        text = f"重新执行阶段「{machine.STAGE_NAMES[stage]}」。"
        yield _sse("token", {"text": text})
        store.add_message(session_id, "assistant", text)
        machine.start_run(pid, pl._run_range(stage, end))
        async for chunk in stream_project_events(pid, session_id):
            yield chunk
        return

    if action == "resume_pipeline":
        yield _sse("token", {"text": "继续执行流水线。"})
        store.add_message(session_id, "assistant", "继续执行流水线。")
        machine.start_run(pid, pl.resume())
        async for chunk in stream_project_events(pid, session_id):
            yield chunk
        return

    if action == "retry_stage":
        stage = pstate.get("stage", 2)
        tier = pstate.get("tier") or "video"
        _, end = machine.TIER_STAGES[tier]
        text = f"重试阶段「{machine.STAGE_NAMES[stage]}」。"
        yield _sse("token", {"text": text})
        store.add_message(session_id, "assistant", text)
        machine.start_run(pid, pl._run_range(stage, end))
        async for chunk in stream_project_events(pid, session_id):
            yield chunk
        return

    if action == "finish":
        yield _sse("token", {"text": "🎬 项目已完成！成片可在右侧产物栏查看下载。如需局部调整，随时在视频上标注或留言。"})
        store.update_project(pid, state="finished")
        yield _sse("suggestions", {"chips": [
            {"label": "标注修改视频", "action": "open_artifact", "artifact": "video"},
            {"label": "再做一个视频", "action": "new_chat"},
        ]})
        yield _sse("done", {})
        return

    # open_settings / open_artifact / show_pipeline / new_chat / free_text：纯前端动作
    yield _sse("frontend_action", act)
    yield _sse("done", {})


async def run_modify(session_id, project, message, attachments):
    """阶段 12：修改路由。LLM 分类 → 分支执行。"""
    pid = project["id"]
    provider = store.provider_for_key("routing") or _chat_provider()
    if not provider:
        yield _sse("error", {"message": "未配置模型供应商"})
        return
    ann_text = ""
    if attachments:
        ann_text = "\n【用户标注】\n" + json.dumps(attachments, ensure_ascii=False)
    tl_path = store.project_dir(pid) / "timeline.json"
    ctx = ""
    if tl_path.exists():
        ctx = f"\n【当前分镜】\n{tl_path.read_text(encoding='utf-8')[:3000]}"
    try:
        raw = await complete(provider, [
            {"role": "system", "content": prompts.MODIFY_SYSTEM},
            {"role": "user", "content": f"修改要求：{message}{ann_text}{ctx}"}])
        route = machine.extract_json(raw)
    except Exception as e:
        yield _sse("token", {"text": f"修改意图解析失败：{e}。请换种说法描述要改的内容。"})
        yield _sse("done", {})
        return

    branch = route.get("branch")
    yield _sse("token", {"text": f"修改路由：{branch} — {route.get('instruction','')}"})
    store.add_message(session_id, "assistant", f"修改路由：{branch}")

    pl = machine.Pipeline(pid)
    pstate = machine.load_state(pl.dir)
    pstate["gate"] = None

    try:
        if branch == "voice":
            # 换音色/语速：重跑 8→11
            machine.start_run(pid, pl._run_range(8, 11))
        elif branch == "tts":
            machine.start_run(pid, pl._run_range(8, 11))
        elif branch in ("visuals", "scene_redo"):
            # 画面修改：让视觉模型改 visuals.py（带进度播报），成功后重跑 7→11（关键帧门禁处暂停）
            progress: list[str] = []
            async for prog in _apply_visuals_fix(pl, route, message):
                progress.append(prog)
                yield _sse("token", {"text": prog})
            store.add_message(session_id, "assistant",
                              "已按修改意见重写视觉代码并通过冒烟自检，正在重跑关键帧。")
            machine.start_run(pid, pl._run_range(7, 11))
        else:  # content 等：回到分镜
            machine.start_run(pid, pl._run_range(4, 11))
    except Exception as e:
        # 修改失败必须如实可见：播报 + 落盘 + 给出建议操作，且流水线保持原状态可继续
        kind = getattr(e, "kind", type(e).__name__)
        detail = getattr(e, "detail", str(e))
        yield _sse("token", {"text": f"\n❌ 修改失败（{kind}）：{detail}\n"})
        store.add_message(session_id, "assistant", f"修改失败（{kind}）：{detail}")
        yield _sse("suggestions", {"chips": chips_for(
            store.get_project(pid), machine.load_state(pl.dir))})
        yield _sse("done", {})
        return
    async for chunk in stream_project_events(pid, session_id):
        yield chunk


async def _apply_visuals_fix(pl, route, message):
    """把修改意见发给视觉阶段模型，重写 visuals.py（异步生成器，逐段播报进度）。
    冒烟/自动修复失败时恢复修改前的可用版本，避免可用代码被失败代码覆盖。"""
    p = machine.provider_for(6)
    vpath = pl.dir / "visuals.py"
    current = vpath.read_text(encoding="utf-8") if vpath.exists() else ""
    fix_prompt = (f"以下是当前 visuals.py 和用户的修改意见，请输出修改后的完整 Python 文件，"
                  f"只输出代码：\n【修改意见】{message}\n【路由指令】{route.get('instruction')}\n"
                  f"【当前代码】\n{current}")
    yield "🔧 正在让视觉模型按修改意见重写 visuals.py（通常 1-2 分钟）…\n"
    raw = await complete(p, [{"role": "user", "content": fix_prompt}], max_tokens=32768,
                         reasoning_effort="low")
    code = machine.strip_code_fences(raw)
    yield "🧪 重写完成，正在编译 + 全场景冒烟自检…\n"
    rounds_log: list[str] = []
    try:
        code, fix_rounds = await machine.visuals_with_autofix(
            pl.dir, p, code, emit=lambda s: rounds_log.append(s))
    except Exception:
        if current:
            vpath.write_text(current, encoding="utf-8")  # 恢复修改前可用版本
        raise
    for s in rounds_log:
        yield f"  · {s}\n"
    yield "✅ 自检通过" + (f"（自动修复 {fix_rounds} 轮）" if fix_rounds else "") + "，开始重新渲染关键帧…\n"
    source = f"{p['name']}:{p['model']}" + (f" + 自动修复{fix_rounds}轮" if fix_rounds else "")
    artifacts_note = artifacts.register(pl.dir, "visuals", "code", "视觉代码 visuals.py（修改版）",
                                        "visuals.py", source,
                                        {"smoke": "passed", "fix_rounds": fix_rounds})
    pl.emit({"event": "artifact", "data": artifacts_note})


async def stream_project_events(pid: str, session_id: str):
    """订阅项目事件总线并转发为 SSE，直到流水线暂停/完成/失败。"""
    # 极速竞态：run 在订阅前已到终态（如最后门禁确认后无剩余阶段），直接按当前状态收尾
    pre = machine.load_state(store.project_dir(pid))
    if pre.get("status") in ("tier_done", "failed") and not machine.is_running(pid):
        if pre["status"] == "tier_done":
            yield _sse("token", {"text": "\n✅ 流水线执行完毕。\n"})
        else:
            yield _sse("token", {"text": f"\n❌ 失败：{pre.get('error','')}\n"})
        yield _sse("suggestions", {"chips": chips_for(store.get_project(pid), pre)})
        yield _sse("done", {})
        return
    q = machine.BUS.subscribe(pid)
    chips = []
    try:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=600)
            except asyncio.TimeoutError:
                yield _sse("error", {"message": "流水线等待超时"})
                break
            kind = ev["event"]
            if kind == "stage_event":
                d = ev["data"]
                yield _sse("stage_event", d)
                if d["status"] == "started":
                    yield _sse("token", {"text": f"\n**[{d['name']}]** 开始…\n"})
                elif d["status"] == "completed":
                    yield _sse("token", {"text": f"[{d['name']}] 完成（{d.get('elapsed','?')}s）\n"})
                    # 落盘：重进会话能看到每个阶段的执行记录
                    store.add_message(session_id, "assistant",
                                      f"✔ 阶段「{d['name']}」完成（{d.get('elapsed','?')}s）")
            elif kind == "artifact":
                yield _sse("artifact", ev["data"])
            elif kind == "gate":
                yield _sse("gate", ev["data"])
                yield _sse("token", {"text": f"\n⏸️ **需要人工确认：{ev['data']['name']}**。请在右侧查看产物后确认或提出修改。\n"})
            elif kind == "pipeline_done":
                yield _sse("token", {"text": "\n✅ 流水线执行完毕。\n"})
            elif kind == "error":
                yield _sse("token", {"text": f"\n❌ 失败（{ev['data'].get('kind')}）：{ev['data'].get('message')}\n"})
            elif kind == "interrupted":
                yield _sse("token", {"text": "\n⏹️ 已手动中断。可在右侧流水线面板点击任意节点继续。\n"})
            # 终止条件
            if kind in ("gate", "pipeline_done", "error", "interrupted"):
                project = store.get_project(pid)
                pstate = machine.load_state(store.project_dir(pid))
                yield _sse("suggestions", {"chips": chips_for(project, pstate)})
                # 落盘终态结果：用户退出再进入时能判断流水线发生了什么
                if kind == "gate":
                    outcome = f"⏸️ 流水线暂停：需要人工确认「{ev['data'].get('name')}」。"
                elif kind == "pipeline_done":
                    outcome = "✅ 流水线执行完毕。"
                elif kind == "interrupted":
                    outcome = "⏹️ 已手动中断，可从流水线面板任意节点继续。"
                else:
                    outcome = (f"❌ 执行失败（{ev['data'].get('kind')}）："
                               f"{str(ev['data'].get('message', ''))[:600]}")
                store.add_message(session_id, "assistant", outcome)
                break
    finally:
        machine.BUS.unsubscribe(pid, q)
    yield _sse("done", {})
