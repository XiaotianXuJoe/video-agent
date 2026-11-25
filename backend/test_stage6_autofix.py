#!/usr/bin/env python3
"""阶段 6 冒烟渲染 + 自动带错修复 本地验收（mock LLM，不依赖外部服务）。
断言：
A. 首生成带 flip 坐标 bug → 冒烟拦截 → 自动修复 1 轮 → 通过，fix_rounds==1，产物标注含「自动修复1轮」
B. 生成与修复全都坏 → 跑满 2 轮后如实 RuntimeError，visuals.py 保留失败代码
C. 首生成即正确 → fix_rounds==0，产物标注不含修复
D. 冒烟脚本能抓到「导入期就崩」的代码（import 阶段失败）
"""
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.pipeline import machine  # noqa: E402

GOOD_CODE = '''
from PIL import Image, ImageDraw
WIDTH, HEIGHT = 1920, 1080
def render_visual(scene, local_t):
    img = Image.new("RGB", (WIDTH, HEIGHT), (7, 17, 31))
    d = ImageDraw.Draw(img)
    d.rectangle([100, 100, 500, 300], outline=(93, 226, 231))
    return img
'''

# flip=True 分支算出 x1<x0 的矩形坐标（复刻 e62cf 生产事故）
BUGGY_CODE = '''
from PIL import Image, ImageDraw
WIDTH, HEIGHT = 1920, 1080
def render_visual(scene, local_t):
    img = Image.new("RGB", (WIDTH, HEIGHT), (7, 17, 31))
    d = ImageDraw.Draw(img)
    flip = local_t > scene.get("duration", 5) / 2
    x0, x1 = (800, 300) if flip else (300, 800)
    d.rounded_rectangle([x0, 100, x1, 300], radius=20, outline=(93, 226, 231))
    return img
'''

IMPORT_CRASH_CODE = '''
from PIL import Image
import definitely_not_a_real_module_xyz
WIDTH, HEIGHT = 1920, 1080
def render_visual(scene, local_t):
    return Image.new("RGB", (WIDTH, HEIGHT))
'''

TIMELINE = {
    "title": "验收测试", "width": 1920, "height": 1080, "fps": 30,
    "scenes": [
        {"id": 1, "title": "镜头1", "narration": "旁白一", "tts": "旁白一",
         "duration": 6.0, "audio_offset": 0.55, "audio_duration": 4.0},
        {"id": 2, "title": "镜头2", "narration": "旁白二", "tts": "旁白二",
         "duration": 8.0, "audio_offset": 0.55, "audio_duration": 6.0},
    ],
}

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def make_project():
    d = Path(tempfile.mkdtemp(prefix="va_s6_test_"))
    (d / "timeline.json").write_text(json.dumps(TIMELINE, ensure_ascii=False), encoding="utf-8")
    return d


def make_pipeline(d):
    pl = machine.Pipeline.__new__(machine.Pipeline)
    pl.pid = "testproj"
    pl.dir = d
    pl.state = {"stage": 6, "status": "running", "gate": None, "log": []}
    return pl


def patch_llm(script):
    """script: complete 被调用时依次返回的代码列表。"""
    calls = {"n": 0}

    async def fake_complete(provider, messages, max_tokens=8192, reasoning_effort=None):
        idx = min(calls["n"], len(script) - 1)
        calls["n"] += 1
        return script[idx]

    machine.complete = fake_complete
    machine.provider_for = lambda stage: {"name": "mock", "model": "mock-1", "api_key": "x"}
    return calls


async def case_A():
    d = make_project()
    patch_llm([BUGGY_CODE, GOOD_CODE])
    pl = make_pipeline(d)
    await pl._stage_6()
    arts = json.loads((d / "artifacts.json").read_text(encoding="utf-8"))
    art = [a for a in arts if a["logical_name"] == "visuals"][-1]
    code = (d / "visuals.py").read_text(encoding="utf-8")
    check("A1 首生成坏代码被冒烟拦截并自动修复通过", art["extra"].get("fix_rounds") == 1,
          f"fix_rounds={art['extra'].get('fix_rounds')}")
    check("A2 产物来源标注含「自动修复1轮」", "自动修复1轮" in art.get("source", ""), art.get("source"))
    check("A3 visuals.py 最终为修复后代码", "800, 300" not in code and "render_visual" in code)
    check("A4 smoke 标记 passed", art["extra"].get("smoke") == "passed")
    shutil.rmtree(d, ignore_errors=True)


async def case_B():
    d = make_project()
    calls = patch_llm([BUGGY_CODE, BUGGY_CODE, BUGGY_CODE])
    pl = make_pipeline(d)
    err = None
    try:
        await pl._stage_6()
    except RuntimeError as e:
        err = str(e)
    check("B1 修复 2 轮耗尽后如实报错", err is not None and "自动修复 2 轮后仍失败" in err,
          (err or "")[:80])
    check("B2 LLM 恰好调用 3 次（1 生成 + 2 修复）", calls["n"] == 3, f"calls={calls['n']}")
    check("B3 失败代码保留在 visuals.py 供排查", "800, 300" in (d / "visuals.py").read_text(encoding="utf-8"))
    shutil.rmtree(d, ignore_errors=True)


async def case_C():
    d = make_project()
    calls = patch_llm([GOOD_CODE])
    pl = make_pipeline(d)
    await pl._stage_6()
    arts = json.loads((d / "artifacts.json").read_text(encoding="utf-8"))
    art = [a for a in arts if a["logical_name"] == "visuals"][-1]
    check("C1 首生成即通过时 fix_rounds==0", art["extra"].get("fix_rounds") == 0)
    check("C2 来源标注不带修复标记", "自动修复" not in art.get("source", ""), art.get("source"))
    check("C3 LLM 只调用 1 次", calls["n"] == 1, f"calls={calls['n']}")
    shutil.rmtree(d, ignore_errors=True)


async def case_D():
    d = make_project()
    patch_llm([IMPORT_CRASH_CODE, GOOD_CODE])
    pl = make_pipeline(d)
    await pl._stage_6()
    arts = json.loads((d / "artifacts.json").read_text(encoding="utf-8"))
    art = [a for a in arts if a["logical_name"] == "visuals"][-1]
    check("D1 导入期崩溃也能被冒烟拦截并修复", art["extra"].get("fix_rounds") == 1)
    shutil.rmtree(d, ignore_errors=True)


async def main():
    await case_A()
    await case_B()
    await case_C()
    await case_D()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n===== {len(results) - len(failed)}/{len(results)} 通过 =====")
    if failed:
        sys.exit(1)

asyncio.run(main())
