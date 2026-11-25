#!/usr/bin/env python3
"""修改流（run_modify visuals 分支）本地验收（mock LLM，不依赖外部服务）。
断言：
A. 成功路径：进度播报 token 齐全、visuals.py 更新、注册「修改版」产物、start_run(7→11)、done
B. 视觉模型 ProviderError：错误如实播报 + 落盘 assistant 消息 + suggestions + done，visuals.py 不被污染
C. 冒烟+自动修复耗尽 RuntimeError：visuals.py 恢复为修改前可用版本（不被失败代码覆盖）
"""
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import store  # noqa: E402
from app.agent import chat  # noqa: E402
from app.llm import ProviderError  # noqa: E402
from app.pipeline import machine  # noqa: E402

OLD_CODE = '''
from PIL import Image
WIDTH, HEIGHT = 1920, 1080
def render_visual(scene, local_t):
    return Image.new("RGB", (WIDTH, HEIGHT), (7, 17, 31))
'''

NEW_CODE = '''
from PIL import Image, ImageDraw
WIDTH, HEIGHT = 1920, 1080
def render_visual(scene, local_t):
    img = Image.new("RGB", (WIDTH, HEIGHT), (7, 17, 31))
    d = ImageDraw.Draw(img)
    d.text((100, 100), "4 - 2 = 2", fill=(93, 226, 231))
    return img
'''

BUGGY_CODE = '''
from PIL import Image, ImageDraw
WIDTH, HEIGHT = 1920, 1080
def render_visual(scene, local_t):
    img = Image.new("RGB", (WIDTH, HEIGHT))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([800, 100, 300, 300], radius=20)  # x1<x0 必炸
    return img
'''

TIMELINE = {
    "title": "修改流验收", "width": 1920, "height": 1080, "fps": 30,
    "scenes": [{"id": 1, "title": "镜头1", "narration": "旁白", "tts": "旁白",
                "duration": 6.0, "audio_offset": 0.5, "audio_duration": 4.0}],
}
ROUTE = '{"branch": "visuals", "instruction": "修复减号显示"}'

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


def make_env(script_chat, script_machine=None):
    """搭临时项目目录 + 打桩 store/machine/chat 的外部依赖。"""
    d = Path(tempfile.mkdtemp(prefix="va_modify_test_"))
    (d / "timeline.json").write_text(json.dumps(TIMELINE, ensure_ascii=False), encoding="utf-8")
    (d / "visuals.py").write_text(OLD_CODE, encoding="utf-8")
    (d / "pipeline_state.json").write_text(json.dumps(
        {"stage": 7, "status": "awaiting_gate",
         "gate": {"stage": 7, "name": "关键帧确认"}, "tier": "video", "log": []}), encoding="utf-8")

    provider = {"name": "mock", "model": "mock-1", "api_key": "x", "type": "openai"}
    calls = {"chat": 0, "machine": 0}
    messages = []

    async def fake_chat_complete(p, msgs, max_tokens=8192, reasoning_effort=None):
        item = script_chat[min(calls["chat"], len(script_chat) - 1)]
        calls["chat"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    async def fake_machine_complete(p, msgs, max_tokens=8192, reasoning_effort=None):
        script = script_machine or []
        item = script[min(calls["machine"], len(script) - 1)] if script else ""
        calls["machine"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    started = []

    def fake_start_run(pid, coro):
        started.append(pid)
        coro.close()
        return None

    async def fake_stream_events(pid, sid, q=None):
        yield chat._sse("token", {"text": "\n[mock] 阶段事件流\n"})
        yield chat._sse("done", {})

    store.provider_for_key = lambda key: provider
    store.project_dir = lambda pid: d
    store.add_message = lambda sid, role, content, kind="text", payload=None: \
        messages.append({"role": role, "content": content})
    store.get_project = lambda pid: {"id": pid, "title": "T", "state": "awaiting_gate", "stage": 7}
    machine.provider_for = lambda stage: provider
    machine.start_run = fake_start_run
    chat.complete = fake_chat_complete
    machine.complete = fake_machine_complete
    chat.stream_project_events = fake_stream_events
    return d, calls, messages, started


async def case_A():
    d, calls, messages, started = make_env([ROUTE, NEW_CODE])
    chunks = [c async for c in chat.run_modify("s1", {"id": "p1"}, "第5帧减号显示成方框，修复", None)]
    evs = parse_events(chunks)
    tokens = "".join(x["text"] for e, x in evs if e == "token")
    arts = json.loads((d / "artifacts.json").read_text(encoding="utf-8"))
    mod = [a for a in arts if "修改版" in a.get("title", "")]
    check("A1 进度播报齐全（重写/自检/重渲染）",
          all(k in tokens for k in ("重写 visuals.py", "冒烟自检", "重新渲染关键帧")))
    check("A2 visuals.py 已更新为新代码", "4 - 2 = 2" in (d / "visuals.py").read_text(encoding="utf-8"))
    check("A3 注册「修改版」产物且冒烟通过",
          len(mod) == 1 and mod[0]["extra"].get("smoke") == "passed")
    check("A4 修改成功后启动流水线重跑", started == ["p1"])
    check("A5 流正常结束（done）", any(e == "done" for e, _ in evs))
    check("A6 无报错 token", "❌" not in tokens)
    shutil.rmtree(d, ignore_errors=True)


async def case_B():
    d, calls, messages, started = make_env(
        [ROUTE, ProviderError("empty", "模型返回了空内容（stop=max_tokens，正文未产出）")])
    chunks = [c async for c in chat.run_modify("s1", {"id": "p1"}, "第5帧减号显示成方框，修复", None)]
    evs = parse_events(chunks)
    tokens = "".join(x["text"] for e, x in evs if e == "token")
    check("B1 错误如实播报（含 kind 与 detail）",
          "❌ 修改失败（empty）" in tokens and "空内容" in tokens)
    check("B2 错误落盘为 assistant 消息（刷新后仍可见）",
          any(m["role"] == "assistant" and "修改失败" in m["content"] for m in messages))
    check("B3 给出后续操作建议 chips", any(e == "suggestions" for e, _ in evs))
    check("B4 流正常结束（done），前端不卡死", any(e == "done" for e, _ in evs))
    check("B5 visuals.py 保持修改前内容", (d / "visuals.py").read_text(encoding="utf-8") == OLD_CODE)
    check("B6 未启动流水线", started == [])
    check("B7 未注册修改版产物", not (d / "artifacts.json").exists())
    shutil.rmtree(d, ignore_errors=True)


async def case_C():
    d, calls, messages, started = make_env([ROUTE, BUGGY_CODE], [BUGGY_CODE, BUGGY_CODE])
    chunks = [c async for c in chat.run_modify("s1", {"id": "p1"}, "改一下画面", None)]
    evs = parse_events(chunks)
    tokens = "".join(x["text"] for e, x in evs if e == "token")
    check("C1 修复耗尽如实报错", "修改失败" in tokens and "自动修复 2 轮后仍失败" in tokens,
          tokens[-120:].replace("\n", " "))
    check("C2 visuals.py 恢复为修改前可用版本", (d / "visuals.py").read_text(encoding="utf-8") == OLD_CODE)
    check("C3 修复轮 LLM 恰好调用 2 次", calls["machine"] == 2, f"machine_calls={calls['machine']}")
    check("C4 未启动流水线", started == [])
    shutil.rmtree(d, ignore_errors=True)


async def main():
    await case_A()
    await case_B()
    await case_C()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n===== {len(results) - len(failed)}/{len(results)} 通过 =====")
    if failed:
        sys.exit(1)

asyncio.run(main())
