#!/usr/bin/env python3
"""D1-D4 深挖项回归（纯本地 mock，不依赖外部服务）：
D1. 事件落表：seq 递增、游标增量回放、EventBus.publish 持久化、重启后 history 仍在
D2. SQLite：WAL/busy_timeout 生效、多线程并发读写不报 database is locked
D3. 状态原子写、损坏兜底、run_one_stage 失败回写 prev_stage、执行者单写消息
D4. 脱敏（sk-/Bearer/长 hex）、聊天记录落盘前脱敏、系统提示词含注入防护条款
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import store  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="va_d_test_"))
store.DATA_DIR = TMP / "data"
store.DATA_DIR.mkdir()
store.PROJECTS_DIR = TMP / "projects"
store.PROJECTS_DIR.mkdir()
store.SETTINGS_PATH = store.DATA_DIR / "settings.json"
store.DB_PATH = store.DATA_DIR / "app.db"
store.init_db()

from app import redact  # noqa: E402
from app.pipeline import machine, prompts  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


# ============ D1 事件落表 ============
pid = store.create_project("D1测试", "video")
s1 = store.append_project_event(pid, "stage_event", {"stage": 2, "status": "started"})
s2 = store.append_project_event(pid, "stage_event", {"stage": 2, "status": "completed"})
s3 = store.append_project_event(pid, "gate", {"stage": 3, "name": "创意确认"})
check("D1-1 seq 递增", (s1, s2, s3) == (1, 2, 3), f"{s1},{s2},{s3}")
check("D1-2 latest 游标", store.latest_event_seq(pid) == 3)
ev_all = store.list_project_events(pid)
check("D1-3 全量回放 3 条", len(ev_all) == 3)
check("D1-4 payload 还原", ev_all[1]["payload"]["status"] == "completed")
ev_inc = store.list_project_events(pid, after=2)
check("D1-5 增量回放只剩 seq>2", len(ev_inc) == 1 and ev_inc[0]["seq"] == 3)
check("D1-6 其他项目互不可见", store.list_project_events("deadbeef00") == [])

# EventBus.publish 落表 + seq 回填
bus = machine.EventBus()
bus.publish(pid, {"event": "artifact", "data": {"logical_name": "x"}})
check("D1-7 publish 后 latest=4", store.latest_event_seq(pid) == 4)
hist = bus.history(pid, after=3)
check("D1-8 history 从 DB 回放", len(hist) == 1 and hist[0]["event"] == "artifact")

# 会话绑定与单写者落盘
sid = store.create_session()
store.bind_project(sid, pid)
store.add_project_note(pid, "✔ 阶段「测试」完成（1.0s）")
msgs = store.session_messages(sid)
check("D1-9 项目事件写入绑定会话", any("阶段「测试」完成" in m["content"] for m in msgs))
check("D1-10 无绑定会话静默跳过", store.add_project_note("deadbeef00", "x") is None)

# Pipeline._persist_note：执行者单写（emit 触发一次，订阅侧不再写）
proj_dir = store.create_project_dir(pid)
pl = machine.Pipeline(pid)
pl.emit_stage(4, "completed", elapsed=2.5)
time.sleep(0.05)
notes = [m for m in store.session_messages(sid) if "阶段「讲稿分镜」完成" in m["content"]]
check("D1-11 执行者恰好写一条完成消息", len(notes) == 1, f"{len(notes)} 条")

# ============ D2 SQLite 并发 ============
with store._conn() as c:
    mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    bt = c.execute("PRAGMA busy_timeout").fetchone()[0]
check("D2-1 WAL 模式", str(mode).lower() == "wal", mode)
check("D2-2 busy_timeout=5000", int(bt) == 5000, bt)

errors = []


def hammer(n):
    try:
        for i in range(60):
            store.add_message(sid, "user", f"并发写入{n}-{i}")
            store.list_sessions()
    except Exception as e:  # noqa: BLE001
        errors.append(e)


threads = [threading.Thread(target=hammer, args=(t,)) for t in range(6)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("D2-3 6 线程×60 次读写无 locked", not errors,
      str(errors[0]) if errors else "360 次全部成功")

# ============ D3 失败恢复 ============
p2 = store.create_project("D3测试", "video")
d2 = store.create_project_dir(p2)
machine.save_state(d2, {"stage": 7, "status": "awaiting_gate", "gate": {"stage": 7}})
check("D3-1 原子写后可读回", machine.load_state(d2)["stage"] == 7)
check("D3-2 无 .tmp 残留", not (d2 / "pipeline_state.tmp").exists())

# 损坏兜底：坏 JSON → 备份 + failed，不抛异常
(d2 / "pipeline_state.json").write_text('{"stage": 7, "stat', encoding="utf-8")
st = machine.load_state(d2)
check("D3-3 损坏状态返回 failed", st["status"] == "failed" and "损坏" in st.get("error", ""))
check("D3-4 坏文件已备份", len(list(d2.glob("pipeline_state.corrupt-*.json"))) == 1)

# run_one_stage 失败回写 prev_stage
machine.save_state(d2, {"stage": 7, "status": "awaiting_gate", "gate": {"stage": 7},
                        "tier": "video", "log": []})
pl2 = machine.Pipeline(p2)


async def _boom(self, s):
    raise RuntimeError("模拟阶段失败")


orig_run_stage = machine.Pipeline._run_stage
machine.Pipeline._run_stage = _boom
try:
    asyncio.run(pl2.run_one_stage(4))
finally:
    machine.Pipeline._run_stage = orig_run_stage
st2 = machine.load_state(d2)
check("D3-5 失败后 stage 回写 7", st2["stage"] == 7, f"stage={st2['stage']}")
check("D3-6 记录 last_failed_stage=4", st2.get("last_failed_stage") == 4)
check("D3-7 状态为 failed 可重试", st2["status"] == "failed")

# ============ D4 注入与脱敏 ============
r = redact.redact("错误：401 Authorization: Bearer sk-Ftp35Ka8MjpCakUgIv0WYCPUpal0yQ7mZxOoixq8i220o1zv")
check("D4-1 sk- 掩码", "sk-Ftp35" not in r and "REDACTED" in r)
r2 = redact.redact("token=9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1908abcdef")
check("D4-2 长 hex 掩码", "9f8e7d6c5b4a" not in r2)
check("D4-3 普通文本原样", redact.redact("镜头 3 渲染完成") == "镜头 3 渲染完成")

store.add_message(sid, "assistant", "失败原因 sk-abcdefghijklmnop 鉴权错误")
last = store.session_messages(sid)[-1]["content"]
check("D4-4 聊天记录落盘前脱敏", "sk-abcdefgh" not in last and "REDACTED" in last)

# 原始响应文件脱敏
machine._write_raw(d2, "raw_test.txt", "upstream says Bearer sk-topsecretvalue123 ok")
raw_text = (d2 / "raw_responses" / "raw_test.txt").read_text(encoding="utf-8")
check("D4-5 raw_responses 脱敏", "sk-topsecret" not in raw_text)

# 系统提示词含注入防护条款
for name in ("CLARIFY_SYSTEM", "CHAT_SYSTEM", "MODIFY_SYSTEM"):
    check(f"D4-6 {name} 含边界条款", "user_input" in getattr(prompts, name))
check("D4-7 资料块带 untrusted 标记", "untrusted" in prompts.REFERENCES_BLOCK)

# 摄取记录标记不可信
rid = store.create_reference("t", "https://example.com", "web", "s", [], ["f"], [], 10)
check("D4-8 摄取记录 untrusted=1", store.get_reference(rid).get("untrusted") == 1)

# ============ 汇总 ============
failed = [n for n, ok in results if not ok]
print(f"\n===== {len(results) - len(failed)}/{len(results)} 通过 =====")
if failed:
    print("FAILED:")
    for n in failed:
        print("-", n)
    sys.exit(1)
