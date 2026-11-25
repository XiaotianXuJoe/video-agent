#!/usr/bin/env python3
"""评审 C5 用户体验修复的后端回归（mock，不依赖外部服务）。
断言：
A. C-UX-01 门禁自然语言判定：否定/含糊不确认、确认词才过、单字「好」不做子串匹配
B. C-UX-02 tts 分支：instruction 写回 timeline.scenes[].tts，指定镜头只改对应镜头，
   未指定逐镜追加；TTS 缓存签名随文本变化而失效；随后重跑 8→11
C. C-UX-03 finish：流水线状态落为 finished，门禁清空
D. C-UX-04 _run_tool：逐行解析 render_video 进度并推 stage_event(progress)
E. C-UX-09 标注快照：data URL 落盘为 annotations/<id>.jpg，JSON 只存 snapshot_path；
   旧 base64 数据仍能原样读取（向后兼容）
"""
import asyncio
import base64
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import annotations, store  # noqa: E402
from app.agent import chat  # noqa: E402
from app.pipeline import machine  # noqa: E402

TOOLS = Path(__file__).resolve().parent / "app" / "tools"

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def parse_events(chunks):
    evs = []
    for c in chunks:
        ev = c.split("\n")[0].replace("event: ", "").strip()
        data = json.loads(c.split("data: ", 1)[1].strip())
        evs.append((ev, data))
    return evs


def tmp_project(timeline: dict, state: dict | None = None) -> Path:
    d = Path(tempfile.mkdtemp(prefix="va_ux_test_"))
    (d / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False), encoding="utf-8")
    if state is not None:
        (d / "pipeline_state.json").write_text(json.dumps(state), encoding="utf-8")
    return d


# ---------- A. 门禁自然语言判定 ----------

def case_A():
    real_handle_action, real_run_modify = chat.handle_action, chat.run_modify
    table = [
        ("这个方案不太好", False, "含否定词，且不应再命中单字「好」"),
        ("不确认", False, "否定优先"),
        ("换个方向", False, "返工词"),
        ("先改一下第三镜", False, "返工词"),
        ("确认", True, "显式确认词"),
        ("可以，继续", True, "确认词命中"),
        ("好的", True, "整句即确认"),
        ("好", True, "整句即确认（不做子串匹配）"),
        ("ok", True, "英文确认"),
        ("这个方案好的部分不错，但第三镜要改", False, "长句+返工词"),
        ("", False, "空消息"),
    ]
    for text, want, why in table:
        got = chat.gate_confirmed(text)
        check(f"A1 gate_confirmed({text!r}) == {want}", got == want, why)

    # 集成：门禁等待中，「不太好」必须走修改流而不是 confirm_gate
    d = tmp_project(
        {"scenes": [{"id": 1, "tts": "旁白"}]},
        {"stage": 3, "status": "awaiting_gate",
         "gate": {"stage": 3, "name": "创意确认"}, "tier": "video", "log": []})

    calls = {"confirm": 0, "modify": 0}
    store.get_session = lambda sid: {"id": sid, "project_id": "p1"}
    store.get_project = lambda pid: {"id": pid, "title": "T", "state": "awaiting_gate", "stage": 3}
    store.project_dir = lambda pid: d
    store.add_message = lambda *a, **k: None
    store.maybe_autoname_session = lambda sid, text: None

    async def fake_handle_action(sid, project, pstate, act):
        calls["confirm"] += 1
        yield chat._sse("done", {})

    async def fake_run_modify(sid, project, message, attachments):
        calls["modify"] += 1
        yield chat._sse("done", {})

    chat.handle_action = fake_handle_action
    chat.run_modify = fake_run_modify

    async def drive(msg):
        return [c async for c in chat.handle_chat("s1", msg)]

    asyncio.run(drive("这个方案不太好"))
    check("A2 「不太好」走修改流", calls["modify"] == 1 and calls["confirm"] == 0, str(calls))

    calls["confirm"] = calls["modify"] = 0
    asyncio.run(drive("确认"))
    check("A3 「确认」走 confirm_gate", calls["confirm"] == 1 and calls["modify"] == 0, str(calls))
    # 还原真实实现，避免桩泄漏到后续用例
    chat.handle_action, chat.run_modify = real_handle_action, real_run_modify
    shutil.rmtree(d, ignore_errors=True)


# ---------- B. tts 分支 ----------

TIMELINE_TTS = {
    "title": "tts 修复验收", "width": 1920, "height": 1080, "fps": 30,
    "scenes": [
        {"id": 1, "title": "镜头1", "narration": "甲", "tts": "第一镜原文本",
         "duration": 6.0, "audio_offset": 0.55, "audio_duration": 4.0},
        {"id": 2, "title": "镜头2", "narration": "乙", "tts": "第二镜原文本",
         "duration": 6.0, "audio_offset": 0.55, "audio_duration": 4.0},
    ],
}


def _load_tts_tool():
    spec = importlib.util.spec_from_file_location("va_minimax_tts", TOOLS / "minimax_tts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def case_B():
    # 指定镜头号：只改对应镜头
    d = tmp_project(TIMELINE_TTS)
    route = {"branch": "tts", "scene_ids": [2], "instruction": "第二镜改后的配音：把「负」读作「减」"}
    n, label = chat._apply_tts_instruction(d, route)
    tl = json.loads((d / "timeline.json").read_text(encoding="utf-8"))
    by_id = {sc["id"]: sc for sc in tl["scenes"]}
    check("B1 指定第2镜：tts 写回 instruction", by_id[2]["tts"] == route["instruction"])
    check("B2 指定第2镜：第1镜不变", by_id[1]["tts"] == "第一镜原文本")
    check("B3 返回改动数与描述", n == 1 and "第2镜" in label, f"{n} {label}")

    # 未指定镜头：逐镜追加
    d2 = tmp_project(TIMELINE_TTS)
    n2, label2 = chat._apply_tts_instruction(
        d2, {"branch": "tts", "instruction": "把 RAG 读作 R-A-G"})
    tl2 = json.loads((d2 / "timeline.json").read_text(encoding="utf-8"))
    check("B4 未指定镜头：逐镜追加覆盖全部镜头", n2 == 2 and label2 == "全部镜头", f"{n2} {label2}")
    check("B5 追加后每镜 tts 都发生变化",
          all("把 RAG 读作 R-A-G" in sc["tts"] for sc in tl2["scenes"]))

    # instruction 里的「第N镜」也能定位
    d3 = tmp_project(TIMELINE_TTS)
    chat._apply_tts_instruction(d3, {"branch": "tts", "instruction": "第1镜：改念作「加」"})
    tl3 = json.loads((d3 / "timeline.json").read_text(encoding="utf-8"))
    by3 = {sc["id"]: sc for sc in tl3["scenes"]}
    check("B6 instruction 文本中的镜头号生效",
          by3[1]["tts"] == "第1镜：改念作「加」" and by3[2]["tts"] == "第二镜原文本")

    # 缓存签名：tts 文本变化 → signature 变化（minimax_tts 用 tts 文本参与 sha256）
    tts_mod = _load_tts_tool()
    args = SimpleNamespace(voice_id="v", speed=0.925, model="m",
                           audio_offset=0.55, end_hold=1.2, no_process=False)
    old_sig = tts_mod.signature({"tts": "第二镜原文本"}, args)
    new_sig = tts_mod.signature({"tts": tl["scenes"][1]["tts"]}, args)
    check("B7 TTS 缓存签名随 tts 文本失效", old_sig != new_sig, f"{old_sig[:8]} vs {new_sig[:8]}")

    # run_modify 集成：tts 分支写回 + 启动 8→11
    d4 = tmp_project(TIMELINE_TTS, {"stage": 11, "status": "tier_done",
                                    "gate": None, "tier": "video", "log": []})
    started = []

    async def fake_complete(p, msgs, max_tokens=8192, reasoning_effort=None):
        return json.dumps({"branch": "tts", "scene_ids": [2],
                           "instruction": "第二镜改成「减号」"}, ensure_ascii=False)

    async def fake_stream(pid, sid, q=None):
        yield chat._sse("done", {})

    store.provider_for_key = lambda key: {"name": "mock", "model": "m", "api_key": "x"}
    store.project_dir = lambda pid: d4
    store.add_message = lambda *a, **k: None
    chat.complete = fake_complete
    chat.stream_project_events = fake_stream

    def fake_start_run(pid, coro):
        started.append(pid)
        coro.close()

    machine.start_run = fake_start_run

    async def drive_modify():
        return [c async for c in chat.run_modify("s1", {"id": "p1"}, "第二镜发音不对", None)]

    chunks = asyncio.run(drive_modify())
    tokens = "".join(x["text"] for e, x in parse_events(chunks) if e == "token")
    tl4 = json.loads((d4 / "timeline.json").read_text(encoding="utf-8"))
    check("B8 run_modify(tts) 修改后 tts 文本确实变化",
          tl4["scenes"][1]["tts"] == "第二镜改成「减号」")
    check("B9 run_modify(tts) 播报已更新配音文本", "已更新" in tokens and "重新配音" in tokens)
    check("B10 run_modify(tts) 启动流水线重跑", started == ["p1"])
    for p in (d, d2, d3, d4):
        shutil.rmtree(p, ignore_errors=True)


# ---------- C. finish 状态 ----------

def case_C():
    d = tmp_project(TIMELINE_TTS, {"stage": 11, "status": "awaiting_gate",
                                   "gate": {"stage": 11, "name": "成片审片"},
                                   "tier": "video", "log": []})
    saved = {}
    updated = {}

    store.project_dir = lambda pid: d
    store.get_project = lambda pid: {"id": pid, "title": "T", "state": "awaiting_gate", "stage": 11}
    store.update_project = lambda pid, **kw: updated.update(kw)
    machine.save_state = lambda pd, st: saved.update({"dir": pd, "state": dict(st)})

    async def drive_finish():
        return [c async for c in chat.handle_action(
            "s1", {"id": "p1"}, {"stage": 11, "status": "awaiting_gate"}, {"action": "finish"})]

    chunks = asyncio.run(drive_finish())
    check("C1 finish 把流水线状态落为 finished",
          saved.get("state", {}).get("status") == "finished", str(saved.get("state")))
    check("C2 finish 清空门禁", saved.get("state", {}).get("gate") is None)
    check("C3 finish 同步项目状态", updated.get("state") == "finished", str(updated))
    check("C4 finish 流正常结束", any(e == "done" for e, _ in parse_events(chunks)))
    # finished 状态下的服务端 chips 不回落到默认「生成创意方案」
    from app.agent.suggestions import chips_for
    chips = chips_for({"id": "p1"}, {"status": "finished", "stage": 11, "tier": "video"})
    check("C5 finished chips 指向视频/QC/新对话",
          any(c["action"] == "open_artifact" and c.get("artifact") == "video" for c in chips)
          and any(c["action"] == "new_chat" for c in chips), str(chips))
    shutil.rmtree(d, ignore_errors=True)


# ---------- D. 渲染进度流 ----------

PROGRESS_SCRIPT = '''
import sys
print("scene 01: 0/600", flush=True)
print("noise line without progress", flush=True)
print("scene 01: 300/600", flush=True)
print("scene 01: DONE 600 frames", flush=True)
'''


def case_D():
    d = tmp_project({"title": "渲染", "fps": 30,
                     "scenes": [{"id": 1, "duration": 20.0}]})
    script = d / "fake_render.py"
    script.write_text(PROGRESS_SCRIPT, encoding="utf-8")

    store.project_dir = lambda pid: d
    pl = machine.Pipeline("p1")
    pl.state["stage"] = 10

    async def run():
        q = machine.BUS.subscribe("p1")
        try:
            await pl._run_tool([sys.executable, "-u", str(script)],
                               {"PATH": ""}, "渲染合成")
        finally:
            machine.BUS.unsubscribe("p1", q)
        evs = []
        while not q.empty():
            evs.append(q.get_nowait())
        return evs

    evs = asyncio.run(run())
    prog = [e["data"] for e in evs
            if e["event"] == "stage_event" and e["data"].get("status") == "progress"]
    percents = [p.get("percent") for p in prog]
    check("D1 解析出进度事件（至少 3 条）", len(prog) >= 3, f"{len(prog)}")
    check("D2 帧数分母来自 timeline 总帧", any(p.get("total_frames") == 600 for p in prog),
          str([p.get("total_frames") for p in prog]))
    check("D3 中段进度百分比正确（300/600 → 50%）", 50 in percents, str(percents))
    check("D4 DONE 行被视为完成帧数（封顶 99%）", 99 in percents, str(percents))
    check("D5 进度文本含帧数", any("600 帧" in str(p.get("detail")) for p in prog))
    shutil.rmtree(d, ignore_errors=True)


# ---------- E. 标注快照落盘 ----------

def case_E():
    d = tmp_project({"fps": 30, "scenes": [
        {"id": 1, "start": 0, "duration": 10.0}]})
    store.project_dir = lambda pid: d

    jpeg = b"\xff\xd8\xff\xe0" + b"x" * 64 + b"\xff\xd9"
    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    ann = annotations.save_annotation("p1", {
        "type": "frame", "video_version": "video_v1", "t_start": 1.0, "t_end": 1.0,
        "shapes": [{"kind": "rect", "x": 1, "y": 2, "w": 3, "h": 4}],
        "snapshot": data_url, "comment": "减号显示为方框"})

    snap_file = d / ann["snapshot_path"]
    check("E1 快照存为文件 annotations/<ann_id>.jpg",
          ann["snapshot_path"] == f"annotations/{ann['id']}.jpg" and snap_file.exists(),
          ann["snapshot_path"])
    check("E2 落盘字节与原图一致", snap_file.read_bytes() == jpeg)
    check("E3 JSON 不再内联 base64", ann["snapshot"] == "")

    raw_json = (d / "annotations" / "annotations.json").read_text(encoding="utf-8")
    check("E4 annotations.json 里没有 base64 尾部", "base64," not in raw_json and len(raw_json) < 4000,
          f"{len(raw_json)} 字节")

    # 兼容：旧数据（base64 内联）仍原样读取
    legacy = {"id": "ann_old", "type": "frame", "t_start": 0.5, "t_end": 0.5,
              "shapes": [], "snapshot": data_url, "comment": "旧标注"}
    items = json.loads(raw_json)
    items.append(legacy)
    (d / "annotations" / "annotations.json").write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8")
    loaded = annotations.list_annotations("p1")
    old = [a for a in loaded if a["id"] == "ann_old"][0]
    check("E5 旧 base64 标注读取正常（向后兼容）", old["snapshot"].startswith("data:image/jpeg"))

    # 已经是相对路径的输入原样保留，不重复写文件
    ann2 = annotations.save_annotation("p1", {
        "type": "frame", "t_start": 2.0, "t_end": 2.0, "shapes": [],
        "snapshot": ann["snapshot_path"], "comment": "重存"})
    check("E6 相对路径输入原样复用", ann2["snapshot_path"] == ann["snapshot_path"])
    shutil.rmtree(d, ignore_errors=True)


def main():
    case_A()
    case_B()
    case_C()
    case_D()
    case_E()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n===== {len(results) - len(failed)}/{len(results)} 通过 =====")
    if failed:
        sys.exit(1)


main()
