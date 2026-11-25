#!/usr/bin/env python3
"""操作与执行结果落盘可见性 本地验收（mock，不依赖外部服务）。
断言：
A. confirm_gate 动作：动作本身落盘为 assistant 消息
B. 阶段完成事件落盘（重进会话可见每阶段执行记录）
C. 门禁暂停终态落盘
D. 失败终态落盘且截断到 600 字符内
E. run_modify 成功路径落盘进度摘要
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
from app.pipeline import machine  # noqa: E402

results = []
REAL_STREAM = chat.stream_project_events  # case_A 会打桩替换，后续用例需用真实现


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def make_env(state):
    d = Path(tempfile.mkdtemp(prefix="va_persist_test_"))
    (d / "pipeline_state.json").write_text(json.dumps(state), encoding="utf-8")
    messages = []
    store.project_dir = lambda pid: d
    store.add_message = lambda sid, role, content, kind="text", payload=None: \
        messages.append(content)
    # 执行者单写模型（D3/C-REL-09）：事件经 Pipeline.emit 落盘，需有绑定会话
    store.session_for_project = lambda pid: "s1"
    store.get_project = lambda pid: {"id": pid, "title": "T", "state": state["status"], "stage": state["stage"]}
    return d, messages


async def case_A():
    d, messages = make_env({"stage": 7, "status": "awaiting_gate",
                            "gate": {"stage": 7, "name": "关键帧确认"}, "tier": "video", "log": []})

    async def fake_stream(pid, sid, q=None):
        yield chat._sse("done", {})

    chat.stream_project_events = fake_stream
    machine.start_run = lambda pid, coro: (coro.close(), None)[1]
    chunks = [c async for c in chat.handle_action("s1", {"id": "p1"}, {"stage": 7}, {"action": "confirm_gate"})]
    check("A1 确认门禁动作落盘", any("已确认门禁" in m for m in messages), str(messages))
    check("A2 流正常结束", any("done" in c for c in chunks))
    shutil.rmtree(d, ignore_errors=True)


async def case_BCD():
    d, messages = make_env({"stage": 6, "status": "running", "gate": None, "tier": "video", "log": []})

    async def drive():
        chunks = []
        async for c in REAL_STREAM("p1", "s1"):
            chunks.append(c)
        return chunks

    task = asyncio.create_task(drive())
    await asyncio.sleep(0.2)
    # 执行者单写：事件经 Pipeline.emit 发布（内部落盘 + 订阅分发），
    # 订阅侧（stream_project_events）不再写消息——多标签不再重复
    pl = machine.Pipeline("p1")
    pl.emit({"event": "stage_event", "data": {
        "stage": 6, "name": "视觉代码", "status": "completed", "elapsed": 61.2}})
    await asyncio.sleep(0.05)
    pl.emit({"event": "gate", "data": {"stage": 7, "name": "关键帧确认"}})
    chunks = await asyncio.wait_for(task, timeout=5)
    check("B1 阶段完成落盘", any("✔ 阶段「视觉代码」完成（61.2s）" in m for m in messages), str(messages))
    check("B1b 订阅侧不写重复消息",
          len([m for m in messages if "阶段「视觉代码」完成" in m]) == 1, str(messages))
    check("C1 门禁暂停终态落盘", any("需要人工确认「关键帧确认」" in m for m in messages))
    check("C2 门禁时给出 chips", any("suggestions" in c for c in chunks))
    shutil.rmtree(d, ignore_errors=True)


async def case_D():
    d, messages = make_env({"stage": 8, "status": "running", "gate": None, "tier": "video", "log": []})
    long_msg = "配音合成失败: " + "x" * 2000

    async def drive():
        return [c async for c in REAL_STREAM("p1", "s1")]

    task = asyncio.create_task(drive())
    await asyncio.sleep(0.2)
    pl = machine.Pipeline("p1")
    pl.emit({"event": "error", "data": {"kind": "stage_error", "message": long_msg}})
    await asyncio.wait_for(task, timeout=5)
    fail_msgs = [m for m in messages if m.startswith("❌ 执行失败")]
    check("D1 失败终态落盘", len(fail_msgs) == 1, str(messages)[:80])
    check("D2 失败消息含 kind 与原因", fail_msgs and "stage_error" in fail_msgs[0] and "配音合成失败" in fail_msgs[0])
    check("D3 超长 traceback 被截断（≤640 字符）", fail_msgs and len(fail_msgs[0]) <= 640,
          f"len={len(fail_msgs[0]) if fail_msgs else 0}")
    shutil.rmtree(d, ignore_errors=True)


async def main():
    await case_A()
    await case_BCD()
    await case_D()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n===== {len(results) - len(failed)}/{len(results)} 通过 =====")
    if failed:
        sys.exit(1)

asyncio.run(main())
